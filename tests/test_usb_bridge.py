"""Tests for tools/usb_bridge.py.

No real serial port and no real backend are used: serial is a fake in-memory
object and HTTP goes through httpx.MockTransport. These tests never touch
hardware and never start the bridge's CLI automatically.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from tools.usb_bridge import (
    REQUIRED_TELEMETRY_FIELDS,
    UsbBridge,
    build_arg_parser,
    build_config_message,
    fetch_config,
    parse_incoming_line,
    post_telemetry,
)

SAMPLE_CONFIG = {
    "version": 5,
    "mode": "nasa",
    "simulate": False,
    "warn_adc": 1200,
    "alarm_adc": 2000,
    "clear_adc": 700,
    "damper_deg": {"open": 90, "half": 45, "closed": 0},
    "response": {"ok": "open", "warn": "half", "alarm": "closed"},
    "airflow_cm_s": {"open": 15, "half": 7, "closed": 0},
}

SAMPLE_TELEMETRY_LINE = {
    "type": "telemetry",
    "device": "pyron-node-1",
    "uptime_ms": 123,
    "mq2_raw": 0,
    "state": "ok",
    "damper": "open",
    "damper_deg": 90,
    "config_version": 5,
}


class FakeSerial:
    """Duck-typed stand-in for serial.Serial: queued lines in, captured writes out.

    Once the queue of lines is exhausted, readline() behaves like a real
    pyserial timeout: it returns b"" instead of blocking. An optional
    stop_event lets tests make the read loop exit on its own once the fake
    device has nothing left to say.
    """

    def __init__(self, lines: list[bytes | str] | None = None, stop_event: threading.Event | None = None):
        self._lines = [line.encode() if isinstance(line, str) else line for line in (lines or [])]
        self._index = 0
        self.written: list[bytes] = []
        self.stop_event = stop_event
        self.raise_on_write: Exception | None = None

    def readline(self) -> bytes:
        if self._index >= len(self._lines):
            if self.stop_event is not None:
                self.stop_event.set()
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    def write(self, data: bytes) -> int:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.written.append(data)
        return len(data)


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def config_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/config"
    return httpx.Response(200, json=SAMPLE_CONFIG)


def make_bridge(ser: FakeSerial, client: httpx.Client, **kwargs) -> UsbBridge:
    return UsbBridge(ser, client, "http://127.0.0.1:8000", **kwargs)


# --- parse_incoming_line -------------------------------------------------------


def test_parse_valid_telemetry_line_strips_type():
    payload = parse_incoming_line(json.dumps(SAMPLE_TELEMETRY_LINE))
    assert payload is not None
    assert "type" not in payload
    for field in REQUIRED_TELEMETRY_FIELDS:
        assert payload[field] == SAMPLE_TELEMETRY_LINE[field]


def test_parse_accepts_bytes_with_trailing_newline():
    raw = (json.dumps(SAMPLE_TELEMETRY_LINE) + "\n").encode("utf-8")
    payload = parse_incoming_line(raw)
    assert payload is not None
    assert payload["device"] == "pyron-node-1"


@pytest.mark.parametrize("raw", [b"", b"\n", b"   \n", "", "\n", "   "])
def test_parse_blank_lines_ignored(raw):
    assert parse_incoming_line(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "{",
        '{"type": "telemetry", "device": }',
        "null",
        "true",
        "42",
        '"just a string"',
        "[1, 2, 3]",
    ],
)
def test_parse_malformed_or_non_object_json_ignored(raw):
    assert parse_incoming_line(raw) is None


def test_parse_unknown_message_type_ignored():
    line = json.dumps({"type": "status", "ok": True})
    assert parse_incoming_line(line) is None


def test_parse_missing_type_field_ignored():
    line = json.dumps({k: v for k, v in SAMPLE_TELEMETRY_LINE.items() if k != "type"})
    assert parse_incoming_line(line) is None


@pytest.mark.parametrize("missing", list(REQUIRED_TELEMETRY_FIELDS))
def test_parse_telemetry_missing_required_field_ignored(missing):
    line = json.dumps({k: v for k, v in SAMPLE_TELEMETRY_LINE.items() if k != missing})
    assert parse_incoming_line(line) is None


def test_parse_non_utf8_bytes_ignored_without_crashing():
    assert parse_incoming_line(b"\xff\xfe\x00broken") is None


def test_parse_never_invents_fields():
    # Extra firmware fields pass through untouched; nothing is added or removed
    # beyond stripping "type".
    line = json.dumps({**SAMPLE_TELEMETRY_LINE, "rssi": -61})
    payload = parse_incoming_line(line)
    assert payload["rssi"] == -61
    assert set(payload) == set(SAMPLE_TELEMETRY_LINE) - {"type"} | {"rssi"}


# --- build_config_message ------------------------------------------------------


def test_build_config_message_exact_shape():
    line = build_config_message(SAMPLE_CONFIG)
    decoded = json.loads(line)
    assert decoded == {"type": "config", "config": SAMPLE_CONFIG}


def test_build_config_message_is_one_line_json():
    line = build_config_message(SAMPLE_CONFIG)
    assert "\n" not in line


# --- fetch_config / post_telemetry --------------------------------------------


def test_fetch_config_returns_backend_response_verbatim():
    client = make_client(config_handler)
    config = fetch_config(client, "http://127.0.0.1:8000")
    assert config == SAMPLE_CONFIG


def test_fetch_config_returns_none_on_backend_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = make_client(handler)
    assert fetch_config(client, "http://127.0.0.1:8000") is None


def test_fetch_config_returns_none_when_backend_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    assert fetch_config(client, "http://127.0.0.1:8000") is None


def test_post_telemetry_sends_exact_payload_and_returns_status():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/telemetry"
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    payload = {k: v for k, v in SAMPLE_TELEMETRY_LINE.items() if k != "type"}
    status = post_telemetry(client, "http://127.0.0.1:8000", payload)
    assert status == 200
    assert captured["body"] == payload  # no fields added or removed


def test_post_telemetry_returns_status_even_on_422():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "invalid"})

    client = make_client(handler)
    status = post_telemetry(client, "http://127.0.0.1:8000", {"bad": "payload"})
    assert status == 422


def test_post_telemetry_returns_none_when_backend_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    payload = {k: v for k, v in SAMPLE_TELEMETRY_LINE.items() if k != "type"}
    assert post_telemetry(client, "http://127.0.0.1:8000", payload) is None


# --- UsbBridge.push_config_once (config forwarding) ----------------------------


def test_push_config_once_writes_ndjson_line_to_serial():
    ser = FakeSerial()
    client = make_client(config_handler)
    bridge = make_bridge(ser, client)

    assert bridge.push_config_once() is True
    assert len(ser.written) == 1
    line = ser.written[0].decode("utf-8")
    assert line.endswith("\n")
    assert json.loads(line) == {"type": "config", "config": SAMPLE_CONFIG}


def test_push_config_once_survives_backend_error_without_writing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down")

    ser = FakeSerial()
    client = make_client(handler)
    bridge = make_bridge(ser, client)

    assert bridge.push_config_once() is False
    assert ser.written == []


def test_push_config_once_survives_serial_write_error():
    from serial import SerialException

    ser = FakeSerial()
    ser.raise_on_write = SerialException("device disconnected")
    client = make_client(config_handler)
    bridge = make_bridge(ser, client)

    assert bridge.push_config_once() is False


# --- UsbBridge.handle_line / read_loop (telemetry forwarding) ------------------


def test_handle_line_valid_telemetry_forwards_exact_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    ser = FakeSerial()
    client = make_client(handler)
    bridge = make_bridge(ser, client)

    status = bridge.handle_line(json.dumps(SAMPLE_TELEMETRY_LINE))
    assert status == 200
    assert "type" not in captured["body"]
    assert captured["body"]["device"] == "pyron-node-1"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\n",
        "not json",
        json.dumps({"type": "status"}),
        json.dumps({k: v for k, v in SAMPLE_TELEMETRY_LINE.items() if k != "mq2_raw"}),
    ],
)
def test_handle_line_invalid_input_never_posts(raw):
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={"ok": True})

    ser = FakeSerial()
    client = make_client(handler)
    bridge = make_bridge(ser, client)

    assert bridge.handle_line(raw) is None
    assert called["count"] == 0


def test_read_loop_processes_mixed_valid_and_invalid_lines():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    lines = [
        json.dumps(SAMPLE_TELEMETRY_LINE),
        "",  # blank
        "not json",  # malformed
        json.dumps({"type": "status", "ok": True}),  # unknown type
        json.dumps({**SAMPLE_TELEMETRY_LINE, "uptime_ms": 456}),
    ]
    client = make_client(handler)
    ser = FakeSerial(lines=lines)
    bridge = make_bridge(ser, client)
    ser.stop_event = bridge._stop  # let the fake device "hang up" end the loop

    bridge.read_loop()

    assert len(posted) == 2
    assert posted[0]["uptime_ms"] == 123
    assert posted[1]["uptime_ms"] == 456
    # Nothing invented: every posted field traces back to a real input line.
    for body in posted:
        assert set(body) == set(SAMPLE_TELEMETRY_LINE) - {"type"}


def test_read_loop_survives_serial_read_errors_without_crashing():
    from serial import SerialException

    class FlakyThenDoneSerial(FakeSerial):
        def __init__(self, stop_event):
            super().__init__(lines=[json.dumps(SAMPLE_TELEMETRY_LINE)], stop_event=stop_event)
            self._raised = False

        def readline(self) -> bytes:
            if not self._raised:
                self._raised = True
                raise SerialException("transient USB glitch")
            return super().readline()

    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    bridge = UsbBridge(None, client, "http://127.0.0.1:8000", retry_delay_s=0.0, sleep=lambda _s: None)
    bridge.ser = FlakyThenDoneSerial(bridge._stop)

    bridge.read_loop()  # must not raise despite the injected SerialException

    assert len(posted) == 1


def test_read_loop_survives_backend_outage_and_keeps_running():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down")

    lines = [json.dumps(SAMPLE_TELEMETRY_LINE), json.dumps(SAMPLE_TELEMETRY_LINE)]
    client = make_client(handler)
    ser = FakeSerial(lines=lines)
    bridge = make_bridge(ser, client)
    ser.stop_event = bridge._stop

    bridge.read_loop()  # must not raise even though every POST fails


# --- UsbBridge.run (end-to-end: config thread + read loop together) -----------


def test_run_forwards_config_periodically_and_telemetry_on_arrival():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/config":
            return httpx.Response(200, json=SAMPLE_CONFIG)
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    lines = [json.dumps(SAMPLE_TELEMETRY_LINE), "garbage", json.dumps(SAMPLE_TELEMETRY_LINE)]
    client = make_client(handler)
    ser = FakeSerial(lines=lines)
    bridge = make_bridge(ser, client, config_interval_s=0.05)
    ser.stop_event = bridge._stop

    run_thread = threading.Thread(target=bridge.run)
    run_thread.start()
    run_thread.join(timeout=2.0)
    assert not run_thread.is_alive()

    assert len(posted) == 2  # the two valid telemetry lines, not the garbage one
    assert len(ser.written) >= 1  # at least the startup config push landed
    first_config_line = json.loads(ser.written[0].decode("utf-8"))
    assert first_config_line == {"type": "config", "config": SAMPLE_CONFIG}


def test_bridge_never_invents_telemetry_when_serial_is_silent():
    client = make_client(config_handler)
    ser = FakeSerial(lines=[])  # device sends nothing
    bridge = make_bridge(ser, client)
    ser.stop_event = bridge._stop

    bridge.read_loop()  # returns immediately once the (empty) line source ends

    # Nothing to post, and the bridge must not synthesize a telemetry POST.
    assert ser.written == []


# --- CLI argument parsing (no serial port, no network, no auto-start) ---------


def test_arg_parser_requires_port_and_backend():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_arg_parser_defaults():
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--port", "/dev/cu.usbmodem1101", "--backend", "http://127.0.0.1:8000"]
    )
    assert args.port == "/dev/cu.usbmodem1101"
    assert args.backend == "http://127.0.0.1:8000"
    assert args.baud == 115200
    assert args.config_interval == pytest.approx(2.0)


def test_arg_parser_accepts_overrides():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--port",
            "/dev/cu.usbmodem1101",
            "--baud",
            "9600",
            "--backend",
            "http://127.0.0.1:8000",
            "--config-interval",
            "5",
        ]
    )
    assert args.baud == 9600
    assert args.config_interval == pytest.approx(5.0)
