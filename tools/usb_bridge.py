"""Mac-side USB serial bridge between an ESP32 and the Pyron backend.

This is the USB fallback path: with the ESP32 plugged into the Mac over USB
(Wi-Fi is no longer part of the fallback), the device speaks newline-delimited
JSON (NDJSON) at 115200 baud instead of making HTTP requests itself. This
bridge sits in between and does the HTTP talking on the device's behalf:

    ESP32 (USB serial, NDJSON) <---> usb_bridge.py <---> FastAPI backend (HTTP)

Direction 1, ESP32 -> Mac, one JSON object per line::

    {"type": "telemetry", "device": "...", "uptime_ms": ..., "mq2_raw": ...,
     "state": "...", "damper": "...", "damper_deg": ..., "config_version": ...}

The bridge strips the ``"type"`` field and POSTs the rest to ``/telemetry``
unchanged. It never fabricates telemetry, never fills in missing fields, and
never reclassifies ``state``/``damper`` -- messages that are not valid
telemetry are logged and dropped.

Direction 2, Mac -> ESP32, one JSON object per line::

    {"type": "config", "config": {...exact response from GET /config...}}

The bridge fetches ``GET /config`` immediately at startup and then on a fixed
interval (default every 2 seconds) and writes the config verbatim; it never
mutates it.

This module only moves bytes between a serial port and the backend's existing
HTTP endpoints. It does not change endpoint contracts, does not talk to an
LLM, and does not touch ESP32 firmware.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from typing import Any, Callable, Protocol

import httpx

try:
    import serial
    from serial import SerialException
except ImportError:  # pragma: no cover - pyserial is a hard runtime dependency
    serial = None  # type: ignore[assignment]

    class SerialException(Exception):  # type: ignore[no-redef]
        """Stand-in so the module still imports when pyserial is missing."""


logger = logging.getLogger("pyron.usb_bridge")

DEFAULT_BAUD = 115200
DEFAULT_CONFIG_INTERVAL_S = 2.0
DEFAULT_SERIAL_TIMEOUT_S = 1.0
DEFAULT_BACKEND_TIMEOUT_S = 5.0
DEFAULT_RETRY_DELAY_S = 1.0

# Fields the firmware is expected to send in every telemetry message, matching
# the backend's Telemetry model. The bridge checks only that they are present
# (not their types/ranges) -- deeper validation is the backend's job via
# POST /telemetry, and its response status is logged as-is.
REQUIRED_TELEMETRY_FIELDS = (
    "device",
    "uptime_ms",
    "mq2_raw",
    "state",
    "damper",
    "damper_deg",
    "config_version",
)


class SerialLike(Protocol):
    """Minimal subset of ``serial.Serial`` the bridge depends on.

    Keeping this narrow lets tests use a plain fake instead of a real port.
    """

    def readline(self) -> bytes: ...

    def write(self, data: bytes) -> int: ...


def parse_incoming_line(raw: bytes | str) -> dict[str, Any] | None:
    """Parse one line received from the ESP32 over serial.

    Returns the telemetry payload (with the ``"type"`` field removed) if, and
    only if, the line is well-formed JSON, is an object, has
    ``"type": "telemetry"``, and has every field in
    ``REQUIRED_TELEMETRY_FIELDS``. Everything else -- blank lines, malformed
    JSON, non-object JSON, unknown message types, and telemetry missing
    required fields -- is logged and returns ``None``. This function never
    raises on bad input and never invents a field that was not present.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("dropped non-UTF-8 serial line: %r", raw)
            return None
    else:
        text = raw

    text = text.strip()
    if not text:
        return None  # blank line (or bare newline) -- nothing to log

    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("dropped malformed JSON line %r: %s", text, exc)
        return None

    if not isinstance(message, dict):
        logger.warning("dropped non-object JSON line: %r", text)
        return None

    msg_type = message.get("type")
    if msg_type != "telemetry":
        logger.warning("dropped message with unknown type %r: %r", msg_type, text)
        return None

    payload = {k: v for k, v in message.items() if k != "type"}
    missing = [field for field in REQUIRED_TELEMETRY_FIELDS if field not in payload]
    if missing:
        logger.warning("dropped telemetry missing fields %s: %r", missing, text)
        return None

    return payload


def build_config_message(config: dict[str, Any]) -> str:
    """Return the exact NDJSON line to send to the ESP32 for this config.

    ``config`` is forwarded verbatim (deep-copied via JSON re-serialization);
    nothing is added, removed, or renamed.
    """
    return json.dumps({"type": "config", "config": config}, separators=(",", ":"))


def fetch_config(client: httpx.Client, backend_url: str) -> dict[str, Any] | None:
    """GET /config from the backend. Returns None (and logs) on any failure."""
    url = f"{backend_url}/config"
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("GET /config failed: %s", exc)
        return None
    try:
        return resp.json()
    except ValueError as exc:
        logger.error("GET /config returned invalid JSON: %s", exc)
        return None


def post_telemetry(client: httpx.Client, backend_url: str, payload: dict[str, Any]) -> int | None:
    """POST payload to /telemetry. Logs the HTTP status; returns it, or None on
    a transport-level failure (backend unreachable, timeout, etc.)."""
    url = f"{backend_url}/telemetry"
    try:
        resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.error("POST /telemetry failed (backend unreachable): %s", exc)
        return None
    logger.info("POST /telemetry -> %s", resp.status_code)
    if resp.status_code >= 400:
        logger.warning("backend rejected telemetry (%s): %s", resp.status_code, resp.text)
    return resp.status_code


class UsbBridge:
    """Pumps config to the ESP32 and telemetry from the ESP32, over one serial
    port and one HTTP backend, until stopped.

    Two loops run concurrently: ``config_loop`` (periodic GET /config -> serial
    write, meant to run on a background thread) and ``read_loop`` (continuous
    serial read -> POST /telemetry, meant to run on the calling/main thread so
    Ctrl+C interrupts it directly).
    """

    def __init__(
        self,
        ser: SerialLike,
        client: httpx.Client,
        backend_url: str,
        config_interval_s: float = DEFAULT_CONFIG_INTERVAL_S,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ser = ser
        self.client = client
        self.backend_url = backend_url.rstrip("/")
        self.config_interval_s = config_interval_s
        self.retry_delay_s = retry_delay_s
        self.sleep = sleep
        self._write_lock = threading.Lock()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _write_line(self, line: str) -> bool:
        try:
            with self._write_lock:
                self.ser.write((line + "\n").encode("utf-8"))
        except SerialException as exc:
            logger.error("serial write failed: %s", exc)
            return False
        return True

    def push_config_once(self) -> bool:
        """Fetch GET /config and write it to serial as one NDJSON line.

        Returns True only if both the fetch and the write succeeded.
        """
        config = fetch_config(self.client, self.backend_url)
        if config is None:
            return False
        if not self._write_line(build_config_message(config)):
            return False
        logger.info("wrote config (version=%s) to serial", config.get("version"))
        return True

    def config_loop(self) -> None:
        """Push config at startup, then every ``config_interval_s`` seconds,
        until ``stop()`` is called. Intended to run on its own thread."""
        self.push_config_once()
        while not self._stop.wait(self.config_interval_s):
            self.push_config_once()

    def handle_line(self, raw: bytes | str) -> int | None:
        """Parse one serial line and forward it if it is valid telemetry.

        Returns the backend's HTTP status code if a POST was made, else None
        (malformed/blank/unknown-type/invalid input, or a backend failure).
        """
        payload = parse_incoming_line(raw)
        if payload is None:
            return None
        return post_telemetry(self.client, self.backend_url, payload)

    def read_loop(self) -> None:
        """Continuously read serial lines and forward telemetry, until
        ``stop()`` is called. Intended to run on the main thread so a
        KeyboardInterrupt (Ctrl+C) lands here and unwinds cleanly."""
        while not self._stop.is_set():
            try:
                raw = self.ser.readline()
            except SerialException as exc:
                logger.error("serial read failed, retrying: %s", exc)
                self.sleep(self.retry_delay_s)
                continue
            if not raw:
                # Read timed out with no data (or, in tests, the line source is
                # exhausted) -- loop back around and re-check the stop flag.
                continue
            self.handle_line(raw)

    def run(self) -> None:
        """Start the config thread, run the read loop on this thread, and make
        sure the config thread is stopped before returning (including when
        this is interrupted, e.g. by Ctrl+C)."""
        config_thread = threading.Thread(
            target=self.config_loop, name="usb-bridge-config", daemon=True
        )
        config_thread.start()
        try:
            self.read_loop()
        finally:
            self.stop()
            config_thread.join(timeout=self.config_interval_s + 1.0)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.usb_bridge",
        description=(
            "USB serial bridge between an ESP32 (NDJSON over USB serial) and "
            "the Pyron FastAPI backend. Fetches GET /config and forwards it "
            "to the device; reads device telemetry and POSTs it to /telemetry."
        ),
    )
    parser.add_argument(
        "--port", required=True, help="Serial device, e.g. /dev/cu.usbmodem1101"
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default {DEFAULT_BAUD})"
    )
    parser.add_argument(
        "--backend",
        required=True,
        help="Backend base URL, e.g. http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--config-interval",
        type=float,
        default=DEFAULT_CONFIG_INTERVAL_S,
        help=f"Seconds between GET /config polls (default {DEFAULT_CONFIG_INTERVAL_S})",
    )
    parser.add_argument(
        "--serial-timeout",
        type=float,
        default=DEFAULT_SERIAL_TIMEOUT_S,
        help=f"Serial readline timeout in seconds (default {DEFAULT_SERIAL_TIMEOUT_S})",
    )
    parser.add_argument(
        "--backend-timeout",
        type=float,
        default=DEFAULT_BACKEND_TIMEOUT_S,
        help=f"HTTP request timeout in seconds (default {DEFAULT_BACKEND_TIMEOUT_S})",
    )
    return parser


def open_serial_with_retry(
    port: str,
    baud: int,
    timeout: float,
    stop: threading.Event,
    retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
) -> "serial.Serial | None":
    """Open the serial port, retrying on temporary failures (e.g. the ESP32
    enumerating slowly after plug-in) until it opens or ``stop`` is set."""
    while not stop.is_set():
        try:
            return serial.Serial(port=port, baudrate=baud, timeout=timeout)
        except SerialException as exc:
            logger.error(
                "could not open serial port %s: %s -- retrying in %.1fs",
                port,
                exc,
                retry_delay_s,
            )
            time.sleep(retry_delay_s)
    return None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)

    if serial is None:
        logger.error(
            "pyserial is not installed. Run: "
            ".venv/bin/python -m pip install -r backend/requirements.txt"
        )
        return 1

    stop_event = threading.Event()
    ser = None
    try:
        ser = open_serial_with_retry(
            args.port, args.baud, args.serial_timeout, stop_event
        )
        if ser is None:  # stopped (Ctrl+C) while waiting for the port
            return 1

        logger.info(
            "connected to %s at %d baud; backend=%s", args.port, args.baud, args.backend
        )

        with httpx.Client(timeout=args.backend_timeout) as client:
            bridge = UsbBridge(
                ser, client, args.backend, config_interval_s=args.config_interval
            )
            bridge.run()
    except KeyboardInterrupt:
        logger.info("stopping (Ctrl+C)")
        stop_event.set()
        return 0
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup on the way out
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
