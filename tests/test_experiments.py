import csv
import json
import math

import pytest
from fastapi.testclient import TestClient

from backend import experiments
from backend.experiments import (
    COMPLETENESS_NOTE,
    DEFAULT_DATASET_PATH,
    GAS_COLUMNS,
    ORIGINAL_COLUMNS,
    SCATTER_X,
    SCATTER_Y,
    SOURCE_URL,
    DatasetError,
    load_dataset,
    parse_numeric,
)
from backend.main import create_app

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


def raw_rows():
    with DEFAULT_DATASET_PATH.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(INITIAL_CONFIG))
    return path


@pytest.fixture
def client(config_path):
    with TestClient(create_app(config_path), follow_redirects=False) as c:
        yield c


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DEFAULT_DATASET_PATH)


def by_id(dataset, record_id):
    return next(i for i in dataset.items if i["record_id"] == record_id)


def write_variant(tmp_path, mutate):
    header, rows = raw_rows()
    header, rows = mutate(list(header), [dict(r) for r in rows])
    path = tmp_path / "variant.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


# --- Loading ------------------------------------------------------------------


def test_dataset_path_is_project_relative():
    assert DEFAULT_DATASET_PATH == experiments.PROJECT_ROOT / "data" / "processed" / "bass_ii_combined.csv"
    assert DEFAULT_DATASET_PATH.is_absolute()


def test_loads_129_records_with_unique_ids(dataset):
    assert len(dataset.items) == 129
    ids = [i["record_id"] for i in dataset.items]
    assert len(set(ids)) == 129
    assert all(ids)


def test_original_strings_and_provenance_preserved(dataset):
    header, rows = raw_rows()
    for raw, item in zip(rows, dataset.items):
        for column in header:
            if column.startswith("numeric__"):
                continue
            assert item[column] == raw[column], (raw["record_id"], column)
    m5 = by_id(dataset, "Miller:M5")
    assert m5["Final CO (ppm)"] == ">500"
    assert m5["Total Frames Shot"] == "300+ +102"
    assert by_id(dataset, "Miller:M3")["Radiometer gain (1000 250 50 10)"] == "250>50>250"


def test_mixed_fan_and_air_display_text_kept_verbatim(dataset):
    header, rows = raw_rows()
    raw = {r["record_id"]: r for r in rows}
    for item in dataset.items:
        assert item["Fan display"] == raw[item["record_id"]]["Fan display"]
        assert item["Air display"] == raw[item["record_id"]]["Air display"]
    assert "numeric__Fan display" not in dataset.items[0]
    assert "numeric__Air display" not in dataset.items[0]


def test_review_flags_preserved(dataset):
    _, rows = raw_rows()
    for raw, item in zip(rows, dataset.items):
        assert item["quality_flags"] == raw["quality_flags"]
        assert item["review_flags"] == ([raw["quality_flags"]] if raw["quality_flags"] else [])
    assert by_id(dataset, "T'ien:T9")["review_flags"] == ["review_date_year_2018"]
    assert by_id(dataset, "Fernandez-Pello:P22b")["review_flags"] == [
        "review_oxygen_above_30_percent:Calibrated final O2 % by vol"
    ]


def test_blank_numeric_fields_become_null_not_zero(dataset):
    _, rows = raw_rows()
    for raw, item in zip(rows, dataset.items):
        for column in dataset.numeric_columns:
            if raw[column].strip() == "":
                assert item[column] is None, (raw["record_id"], column)
    b14 = by_id(dataset, "Bhattacharjee:B14")
    assert b14["Final CO2 % by vol"] == "none"
    assert b14[SCATTER_Y] is None
    assert b14[SCATTER_X] == 0.0


def test_negative_numeric_values_unchanged(dataset):
    assert by_id(dataset, "Olson:O20")["numeric__Initial CO (ppm)"] == -3.0
    assert by_id(dataset, "Olson:O5a")["numeric__Initial CO (ppm)"] == -2.0
    _, rows = raw_rows()
    raw = {r["record_id"]: r for r in rows}
    negatives = 0
    for item in dataset.items:
        for column in dataset.numeric_columns:
            text = raw[item["record_id"]][column].strip()
            if text.startswith("-"):
                negatives += 1
                assert item[column] == float(text) < 0
    assert negatives == 13


def test_numeric_values_are_finite_json_numbers(dataset):
    for item in dataset.items:
        for column in dataset.numeric_columns:
            value = item[column]
            assert value is None or (isinstance(value, float) and math.isfinite(value))
    json.dumps(dataset.items, allow_nan=False)


@pytest.mark.parametrize("text", ["", "  ", "nan", "NaN", "inf", "-inf", "Infinity", "none"])
def test_parse_numeric_rejects_blank_and_nonfinite(text):
    assert parse_numeric(text) is None


def test_parse_numeric_keeps_sign():
    assert parse_numeric("-1.0") == -1.0
    assert parse_numeric("0.0") == 0.0


# --- Summary ------------------------------------------------------------------


def test_summary_counts(dataset):
    _, rows = raw_rows()
    s = dataset.summary
    assert s["record_count"] == 129
    assert s["distinct_pi_count"] == len({r["PI"] for r in rows}) == 6
    assert s["review_flagged_count"] == sum(1 for r in rows if r["quality_flags"]) == 17
    for column in GAS_COLUMNS:
        expected = sum(1 for r in rows if r["numeric__" + column].strip() == "")
        assert s["missing_numeric_counts"][column] == expected


def test_scatter_usable_and_excluded_counts(dataset):
    _, rows = raw_rows()
    usable = sum(1 for r in rows if r[SCATTER_X].strip() and r[SCATTER_Y].strip())
    scatter = dataset.summary["co2_scatter"]
    assert scatter["usable_count"] == usable == 121
    assert scatter["excluded_count"] == 129 - usable == 8


# --- Endpoints ----------------------------------------------------------------


def test_experiments_endpoint_shape(client):
    resp = client.get("/experiments")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "count", "source_url", "completeness_note"}
    assert body["count"] == len(body["items"]) == 129
    assert body["source_url"] == SOURCE_URL
    assert body["completeness_note"] == COMPLETENESS_NOTE
    first = body["items"][0]
    for column in ORIGINAL_COLUMNS:
        assert column in first
    for column in ("record_id", "source_file", "source_row", "source_url", "quality_flags", "review_flags"):
        assert column in first


def test_experiments_endpoint_serializes_nulls_and_negatives(client):
    items = {i["record_id"]: i for i in client.get("/experiments").json()["items"]}
    assert items["Bhattacharjee:B14"][SCATTER_Y] is None
    assert items["Olson:O20"]["numeric__Initial CO (ppm)"] == -3.0


def test_summary_endpoint(client):
    resp = client.get("/experiments/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["record_count"] == 129
    assert body["co2_scatter"]["usable_count"] == 121
    assert body["co2_scatter"]["excluded_count"] == 8
    assert body["source_url"] == SOURCE_URL
    assert body["completeness_note"] == COMPLETENESS_NOTE


# --- Unavailable data ---------------------------------------------------------


def _invalid_variants(tmp_path):
    return {
        "missing_file": tmp_path / "does-not-exist.csv",
        "missing_column": write_variant(
            tmp_path, lambda h, rows: ([c for c in h if c != "Final CO2 % by vol"], rows)
        ),
        "duplicate_id": write_variant(
            tmp_path, lambda h, rows: (h, rows[:-1] + [{**rows[-1], "record_id": rows[0]["record_id"]}])
        ),
        "wrong_count": write_variant(tmp_path, lambda h, rows: (h, rows[:-1])),
    }


@pytest.mark.parametrize("variant", ["missing_file", "missing_column", "duplicate_id", "wrong_count"])
def test_invalid_dataset_raises(tmp_path, variant):
    with pytest.raises(DatasetError):
        load_dataset(_invalid_variants(tmp_path)[variant])


@pytest.mark.parametrize("variant", ["missing_file", "missing_column", "duplicate_id", "wrong_count"])
def test_unavailable_dataset_keeps_hardware_api_working(tmp_path, config_path, variant):
    app = create_app(config_path, dataset_path=_invalid_variants(tmp_path)[variant])
    with TestClient(app, follow_redirects=False) as client:
        for path in ("/experiments", "/experiments/summary"):
            resp = client.get(path)
            assert resp.status_code == 503
            body = resp.json()
            assert body["available"] is False
            assert body["error"].startswith("NASA dataset unavailable:")
            assert body["source_url"] == SOURCE_URL

        assert client.get("/health").json() == {"ok": True}
        assert client.get("/config").json() == INITIAL_CONFIG
        assert client.get("/").status_code == 200
        sample = {
            "device": "pyron-node-1", "uptime_ms": 1, "mq2_raw": 100, "state": "ok",
            "damper": "open", "damper_deg": 90, "config_version": 3,
        }
        assert client.post("/telemetry", json=sample).json() == {"ok": True}
        assert client.get("/telemetry/latest").json()["telemetry"]["mq2_raw"] == 100


def test_dataset_file_is_not_modified(client):
    before = DEFAULT_DATASET_PATH.read_bytes()
    client.get("/experiments")
    client.get("/experiments/summary")
    assert DEFAULT_DATASET_PATH.read_bytes() == before
