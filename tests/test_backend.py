import copy
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.main import (
    DEFAULT_CONFIG_PATH,
    HISTORY_LIMIT,
    ConfigError,
    create_app,
    load_config,
    validate_config,
)

INITIAL_CONFIG = {
    "version": 3,
    "mode": "nasa",
    "simulate": False,
    "warn_adc": 1200,
    "alarm_adc": 2000,
    "clear_adc": 700,
    "damper_deg": {"open": 90, "half": 45, "closed": 0},
    "response": {"ok": "open", "warn": "half", "alarm": "closed"},
    "airflow_cm_s": {"open": 15, "half": 7, "closed": 0},
}
NASA_RESPONSE = {"ok": "open", "warn": "half", "alarm": "closed"}
EARTH_RESPONSE = {"ok": "open", "warn": "open", "alarm": "open"}

SAMPLE = {
    "device": "pyron-node-1",
    "uptime_ms": 123456,
    "mq2_raw": 1450,
    "state": "warn",
    "damper": "half",
    "damper_deg": 45,
    "config_version": 3,
}


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(INITIAL_CONFIG, indent=2))
    return path


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def client(config_path, clock):
    with TestClient(create_app(config_path, clock=clock), follow_redirects=False) as c:
        yield c


def post(client, **overrides):
    return client.post("/telemetry", json={**SAMPLE, **overrides})


# --- Exact paths, no redirects -------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_exact_paths_respond_without_redirects(client):
    responses = [
        client.get("/config"),
        post(client),
        client.get("/telemetry/latest"),
        client.get("/telemetry/history"),
        client.post("/config/mode", json={"mode": "nasa"}),
        client.post("/config/simulate", json={"on": False}),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.request.url
        assert not resp.is_redirect
        assert "location" not in resp.headers


def test_trailing_slash_is_not_redirected(client):
    assert client.get("/config/").status_code == 404
    assert client.post("/telemetry/", json=SAMPLE).status_code == 404


def test_initial_config_is_exact_and_small(client):
    resp = client.get("/config")
    assert resp.json() == INITIAL_CONFIG
    assert len(resp.content) < 2048


def test_repo_config_file_matches_initial_config():
    assert load_config(DEFAULT_CONFIG_PATH) == INITIAL_CONFIG


def test_cors_allows_only_local_dashboard_origins(client):
    for origin in ("http://localhost:8000", "http://127.0.0.1:8000"):
        resp = client.get("/config", headers={"Origin": origin})
        assert resp.headers.get("access-control-allow-origin") == origin
    resp = client.get("/config", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


# --- Dashboard ----------------------------------------------------------------


def test_root_serves_dashboard_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "PYRON" in resp.text
    assert "/static/app.js" in resp.text


@pytest.mark.parametrize(
    "path,content_type",
    [("/static/app.js", "javascript"), ("/static/styles.css", "text/css")],
)
def test_static_assets_served(client, path, content_type):
    resp = client.get(path)
    assert resp.status_code == 200
    assert content_type in resp.headers["content-type"]


def test_dashboard_does_not_break_api_json(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == INITIAL_CONFIG


def test_dashboard_never_posts_telemetry():
    js = (DEFAULT_CONFIG_PATH.parent.parent / "dashboard" / "static" / "app.js").read_text()
    assert '"/telemetry"' not in js
    assert "https://" not in js and "http://" not in js.replace('"http://www.w3.org/2000/svg"', "")


# --- Telemetry ----------------------------------------------------------------


def test_telemetry_record_preserves_fields_and_adds_server_fields(client):
    resp = post(client)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    record = client.get("/telemetry/latest").json()["telemetry"]
    for key, value in SAMPLE.items():
        assert record[key] == value
    received = datetime.fromisoformat(record["received_at"])
    assert received.utcoffset() == timezone.utc.utcoffset(None)
    assert abs((datetime.now(timezone.utc) - received).total_seconds()) < 5
    assert record["airflow_cm_s"] == 7
    assert record["airflow_estimated"] is True


def test_unknown_firmware_fields_are_preserved(client):
    post(client, rssi=-61)
    assert client.get("/telemetry/latest").json()["telemetry"]["rssi"] == -61


def test_reported_state_is_stored_not_reclassified(client):
    post(client, mq2_raw=3500, state="ok", damper="open", damper_deg=90)
    record = client.get("/telemetry/latest").json()["telemetry"]
    assert record["state"] == "ok"
    assert record["damper"] == "open"


def test_airflow_is_null_for_unknown_config_version(client):
    post(client, config_version=99)
    record = client.get("/telemetry/latest").json()["telemetry"]
    assert record["config_version"] == 99
    assert record["airflow_cm_s"] is None
    assert record["airflow_estimated"] is True


def test_airflow_uses_snapshot_of_reported_version(config_path, clock):
    custom = copy.deepcopy(INITIAL_CONFIG)
    custom["airflow_cm_s"] = {"open": 20, "half": 9, "closed": 0}
    config_path.write_text(json.dumps(custom))
    with TestClient(create_app(config_path, clock=clock)) as client:
        client.post("/config/mode", json={"mode": "earth"})
        post(client, config_version=3, damper="half")
        post(client, config_version=4, damper="half")
        post(client, config_version=2, damper="half")
        items = client.get("/telemetry/history").json()["items"]
    assert [i["airflow_cm_s"] for i in items] == [9, 9, None]


@pytest.mark.parametrize("value", [0, 4095])
def test_adc_boundaries_accepted(client, value):
    assert post(client, mq2_raw=value).status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"mq2_raw": -1},
        {"mq2_raw": 4096},
        {"mq2_raw": 1450.5},
        {"mq2_raw": "1450"},
        {"mq2_raw": True},
        {"state": "danger"},
        {"state": "OK"},
        {"damper": "ajar"},
        {"damper": None},
    ],
)
def test_invalid_telemetry_rejected(client, overrides):
    assert post(client, **overrides).status_code == 422
    assert client.get("/telemetry/history").json() == {"items": []}


@pytest.mark.parametrize("missing", list(SAMPLE))
def test_missing_telemetry_field_rejected(client, missing):
    payload = {k: v for k, v in SAMPLE.items() if k != missing}
    assert client.post("/telemetry", json=payload).status_code == 422


def test_history_keeps_last_120_in_receipt_order(client):
    total = HISTORY_LIMIT + 5
    for i in range(total):
        assert post(client, uptime_ms=i).status_code == 200
    items = client.get("/telemetry/history").json()["items"]
    assert len(items) == HISTORY_LIMIT
    assert [i["uptime_ms"] for i in items] == list(range(5, total))
    assert client.get("/telemetry/latest").json()["telemetry"]["uptime_ms"] == total - 1


def test_history_is_receipt_order_not_device_order(client):
    post(client, uptime_ms=2000)
    post(client, uptime_ms=1000)
    items = client.get("/telemetry/history").json()["items"]
    assert [i["uptime_ms"] for i in items] == [2000, 1000]


# --- Online / offline ---------------------------------------------------------


def test_latest_before_any_data(client):
    assert client.get("/telemetry/latest").json() == {
        "online": False,
        "age_seconds": None,
        "telemetry": None,
    }
    assert client.get("/telemetry/history").json() == {"items": []}


def test_online_window_uses_monotonic_age(client, clock):
    post(client)
    latest = client.get("/telemetry/latest").json()
    assert latest["online"] is True
    assert latest["age_seconds"] == 0

    clock.advance(4.9)
    latest = client.get("/telemetry/latest").json()
    assert latest["online"] is True
    assert latest["age_seconds"] == pytest.approx(4.9)

    clock.advance(0.1)
    latest = client.get("/telemetry/latest").json()
    assert latest["online"] is False
    assert latest["age_seconds"] == pytest.approx(5.0)
    assert latest["telemetry"]["uptime_ms"] == SAMPLE["uptime_ms"]


# --- Mode ---------------------------------------------------------------------


def test_mode_transitions_and_versions(client, config_path):
    resp = client.post("/config/mode", json={"mode": "earth"})
    assert resp.status_code == 200
    cfg = resp.json()
    expected = {**INITIAL_CONFIG, "version": 4, "mode": "earth", "response": EARTH_RESPONSE}
    assert cfg == expected
    assert json.loads(config_path.read_text()) == expected
    assert client.get("/config").json() == expected

    assert client.post("/config/mode", json={"mode": "earth"}).json()["version"] == 4

    cfg = client.post("/config/mode", json={"mode": "nasa"}).json()
    assert cfg == {**INITIAL_CONFIG, "version": 5}
    assert json.loads(config_path.read_text()) == cfg


def test_mode_persistence_survives_restart(client, config_path, clock):
    client.post("/config/mode", json={"mode": "earth"})
    with TestClient(create_app(config_path, clock=clock)) as fresh:
        cfg = fresh.get("/config").json()
    assert cfg["mode"] == "earth"
    assert cfg["version"] == 4


def test_persist_leaves_no_temp_files(client, config_path):
    client.post("/config/mode", json={"mode": "earth"})
    client.post("/config/simulate", json={"on": True})
    assert sorted(p.name for p in config_path.parent.iterdir()) == ["config.json"]


@pytest.mark.parametrize("body", [{"mode": "mars"}, {"mode": None}, {}, {"mode": "nasa", "x": 1}])
def test_invalid_mode_rejected(client, config_path, body):
    before = config_path.read_text()
    assert client.post("/config/mode", json=body).status_code == 422
    assert client.get("/config").json()["version"] == 3
    assert config_path.read_text() == before


# --- Simulation ---------------------------------------------------------------


def test_simulation_toggle_and_versions(client, config_path):
    cfg = client.post("/config/simulate", json={"on": True}).json()
    assert cfg == {**INITIAL_CONFIG, "version": 4, "simulate": True}
    assert json.loads(config_path.read_text()) == cfg

    assert client.post("/config/simulate", json={"on": True}).json()["version"] == 4

    cfg = client.post("/config/simulate", json={"on": False}).json()
    assert cfg == {**INITIAL_CONFIG, "version": 5}


def test_simulation_leaves_mode_response_mappings_intact(client):
    cfg = client.post("/config/simulate", json={"on": True}).json()
    assert cfg["mode"] == "nasa"
    assert cfg["response"] == NASA_RESPONSE
    assert cfg["response"]["alarm"] == "closed"

    cfg = client.post("/config/mode", json={"mode": "earth"}).json()
    assert cfg["simulate"] is True
    assert cfg["response"] == EARTH_RESPONSE
    assert cfg["response"]["alarm"] == "open"

    cfg = client.post("/config/simulate", json={"on": False}).json()
    assert cfg["mode"] == "earth"
    assert cfg["response"] == EARTH_RESPONSE


@pytest.mark.parametrize("body", [{"on": "true"}, {"on": 1}, {"on": None}, {}])
def test_invalid_simulate_rejected(client, body):
    assert client.post("/config/simulate", json=body).status_code == 422
    assert client.get("/config").json()["simulate"] is False


# --- No fabricated telemetry --------------------------------------------------


def test_config_changes_do_not_fabricate_telemetry(client):
    client.post("/config/mode", json={"mode": "earth"})
    client.post("/config/simulate", json={"on": True})
    assert client.get("/telemetry/latest").json() == {
        "online": False,
        "age_seconds": None,
        "telemetry": None,
    }
    assert client.get("/telemetry/history").json() == {"items": []}


def test_hardware_unconfirmed_until_it_reports_new_version(client):
    post(client, state="ok", damper="open", damper_deg=90, mq2_raw=500)
    new_version = client.post("/config/simulate", json={"on": True}).json()["version"]

    latest = client.get("/telemetry/latest").json()["telemetry"]
    assert latest["config_version"] == 3
    assert latest["state"] == "ok"
    assert len(client.get("/telemetry/history").json()["items"]) == 1

    post(client, state="alarm", damper="closed", damper_deg=0, config_version=new_version)
    latest = client.get("/telemetry/latest").json()["telemetry"]
    assert latest["config_version"] == new_version
    assert latest["state"] == "alarm"
    assert latest["airflow_cm_s"] == 0


# --- Config validation --------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"clear_adc": -1},
        {"clear_adc": 1200},
        {"warn_adc": 2000},
        {"alarm_adc": 4096},
        {"warn_adc": 1200.5},
        {"mode": "mars"},
        {"simulate": "no"},
        {"response": {"ok": "open", "warn": "half", "alarm": "shut"}},
        {"damper_deg": {"open": 90, "half": 45}},
        {"airflow_cm_s": {"open": 15, "half": -1, "closed": 0}},
        {"source": "rationale text"},
    ],
)
def test_invalid_config_rejected(overrides):
    with pytest.raises(ConfigError):
        validate_config({**INITIAL_CONFIG, **overrides})


def test_invalid_config_file_fails_startup(config_path):
    config_path.write_text(json.dumps({**INITIAL_CONFIG, "warn_adc": 600}))
    with pytest.raises(ConfigError):
        create_app(config_path)
