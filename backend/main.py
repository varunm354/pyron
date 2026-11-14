"""Pyron Phase 1 backend: config + telemetry endpoints for the ESP32 habitat node.

The ESP32 is the HTTP client: it polls GET /config and posts POST /telemetry.
It classifies ok/warn/alarm itself; this server stores what it reports.

Telemetry is held in memory, so run a single server worker.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

HISTORY_LIMIT = 120
ONLINE_WINDOW_S = 5.0
ADC_MAX = 4095
MAX_CONFIG_BYTES = 2048
CORS_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

STATES = ("ok", "warn", "alarm")
DAMPERS = ("open", "half", "closed")
MODE_RESPONSES: dict[str, dict[str, str]] = {
    "nasa": {"ok": "open", "warn": "half", "alarm": "closed"},
    "earth": {"ok": "open", "warn": "open", "alarm": "open"},
}
CONFIG_KEYS = (
    "version",
    "mode",
    "simulate",
    "warn_adc",
    "alarm_adc",
    "clear_adc",
    "damper_deg",
    "response",
    "airflow_cm_s",
)


class ConfigError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_keys(name: str, mapping: Any, expected: tuple[str, ...]) -> None:
    if not isinstance(mapping, dict):
        raise ConfigError(f"{name} must be an object")
    if set(mapping) != set(expected):
        raise ConfigError(f"{name} must have exactly the keys {list(expected)}")


def validate_config(raw: Any) -> dict[str, Any]:
    """Return a deep copy of ``raw`` if it is a valid Pyron config, else raise ConfigError."""
    _check_keys("config", raw, CONFIG_KEYS)

    if not _is_int(raw["version"]) or raw["version"] < 0:
        raise ConfigError("version must be a non-negative integer")
    if raw["mode"] not in MODE_RESPONSES:
        raise ConfigError(f"mode must be one of {list(MODE_RESPONSES)}")
    if not isinstance(raw["simulate"], bool):
        raise ConfigError("simulate must be true or false")

    for key in ("clear_adc", "warn_adc", "alarm_adc"):
        if not _is_int(raw[key]):
            raise ConfigError(f"{key} must be an integer")
    if not 0 <= raw["clear_adc"] < raw["warn_adc"] < raw["alarm_adc"] <= ADC_MAX:
        raise ConfigError(
            f"thresholds must satisfy 0 <= clear_adc < warn_adc < alarm_adc <= {ADC_MAX}"
        )

    _check_keys("damper_deg", raw["damper_deg"], DAMPERS)
    for damper, deg in raw["damper_deg"].items():
        if not _is_int(deg) or not 0 <= deg <= 180:
            raise ConfigError(f"damper_deg.{damper} must be an integer 0-180")

    _check_keys("response", raw["response"], STATES)
    for state, damper in raw["response"].items():
        if damper not in DAMPERS:
            raise ConfigError(f"response.{state} must be one of {list(DAMPERS)}")

    _check_keys("airflow_cm_s", raw["airflow_cm_s"], DAMPERS)
    for damper, flow in raw["airflow_cm_s"].items():
        if not _is_number(flow) or flow < 0:
            raise ConfigError(f"airflow_cm_s.{damper} must be a non-negative number")

    if len(json.dumps(raw, separators=(",", ":")).encode()) > MAX_CONFIG_BYTES:
        raise ConfigError(f"config must serialize to at most {MAX_CONFIG_BYTES} bytes")

    return copy.deepcopy(raw)


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return validate_config(raw)


def write_config_atomic(path: Path, config: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class Telemetry(BaseModel):
    # Unknown firmware fields are kept so records preserve everything the device sent.
    model_config = ConfigDict(extra="allow")

    device: str = Field(min_length=1, max_length=64)
    uptime_ms: int = Field(ge=0)
    mq2_raw: int = Field(ge=0, le=ADC_MAX, strict=True)
    state: Literal["ok", "warn", "alarm"]
    damper: Literal["open", "half", "closed"]
    damper_deg: int = Field(ge=0, le=180)
    config_version: int = Field(ge=0)


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["nasa", "earth"]


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: StrictBool


class PyronState:
    def __init__(self, config_path: Path, clock: Callable[[], float] = time.monotonic):
        self.config_path = config_path
        self.clock = clock
        self.lock = threading.Lock()
        self.config = load_config(config_path)
        self.snapshots: dict[int, dict[str, Any]] = {
            self.config["version"]: copy.deepcopy(self.config)
        }
        self.history: deque[tuple[float, dict[str, Any]]] = deque(maxlen=HISTORY_LIMIT)

    def get_config(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.config)

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            candidate = copy.deepcopy(self.config)
            candidate.update(updates)
            if candidate == self.config:
                return copy.deepcopy(self.config)
            candidate["version"] = self.config["version"] + 1
            candidate = validate_config(candidate)
            write_config_atomic(self.config_path, candidate)
            self.config = candidate
            self.snapshots[candidate["version"]] = copy.deepcopy(candidate)
            return copy.deepcopy(candidate)

    def add_telemetry(self, payload: Telemetry) -> None:
        record = payload.model_dump()
        with self.lock:
            snapshot = self.snapshots.get(payload.config_version)
            airflow = snapshot["airflow_cm_s"][payload.damper] if snapshot else None
            record["received_at"] = datetime.now(timezone.utc).isoformat()
            record["airflow_cm_s"] = airflow
            record["airflow_estimated"] = True
            self.history.append((self.clock(), record))

    def latest(self) -> dict[str, Any]:
        with self.lock:
            if not self.history:
                return {"online": False, "age_seconds": None, "telemetry": None}
            received_mono, record = self.history[-1]
            age = max(0.0, self.clock() - received_mono)
            return {
                "online": age < ONLINE_WINDOW_S,
                "age_seconds": round(age, 3),
                "telemetry": copy.deepcopy(record),
            }

    def history_items(self) -> list[dict[str, Any]]:
        with self.lock:
            return [copy.deepcopy(record) for _, record in self.history]


def create_app(
    config_path: Path | str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    state = PyronState(Path(config_path) if config_path else DEFAULT_CONFIG_PATH, clock)

    app = FastAPI(title="Pyron backend", redirect_slashes=False)
    app.state.pyron = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return state.get_config()

    @app.post("/config/mode")
    def set_mode(body: ModeRequest) -> dict[str, Any]:
        return state.update_config(
            {"mode": body.mode, "response": dict(MODE_RESPONSES[body.mode])}
        )

    @app.post("/config/simulate")
    def set_simulate(body: SimulateRequest) -> dict[str, Any]:
        return state.update_config({"simulate": body.on})

    @app.post("/telemetry")
    def post_telemetry(body: Telemetry) -> dict[str, bool]:
        state.add_telemetry(body)
        return {"ok": True}

    @app.get("/telemetry/latest")
    def telemetry_latest() -> dict[str, Any]:
        return state.latest()

    @app.get("/telemetry/history")
    def telemetry_history() -> dict[str, Any]:
        return {"items": state.history_items()}

    return app


app = create_app()
