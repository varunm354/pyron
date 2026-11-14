"""Read-only loader for the imported NASA BASS-II (PSI-25) experiment table.

The CSV in data/processed/ is used as-is: original cell text, provenance columns, and
review flags are preserved. Only ``numeric__`` columns are converted, to finite floats
or None. This module is independent of the ESP32 config/telemetry handling.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "processed" / "bass_ii_combined.csv"

SOURCE_URL = "https://psi.nasa.gov/physci/repo/data/investigations/PSI-25"
COMPLETENESS_NOTE = "129 imported records; source interface displayed 130; completeness unverified"
EXPECTED_RECORD_COUNT = 129

ORIGINAL_COLUMNS = (
    "PI",
    "Test #",
    "Date",
    "GMT",
    "Sample #",
    "Fuel Sample Material",
    "Flow restrictor",
    "Fan display",
    "Air display",
    "Radiometer gain (1000 250 50 10)",
    "Total Frames Shot",
    "Calibrated initial O2 % by vol",
    "Calibrated final O2 % by vol",
    "Initial CO2 % by vol",
    "Final CO2 % by vol",
    "Initial CO (ppm)",
    "Final CO (ppm)",
)
PROVENANCE_COLUMNS = ("record_id", "source_file", "source_row", "source_url")
GAS_COLUMNS = (
    "Calibrated initial O2 % by vol",
    "Calibrated final O2 % by vol",
    "Initial CO2 % by vol",
    "Final CO2 % by vol",
    "Initial CO (ppm)",
    "Final CO (ppm)",
)
NUMERIC_PREFIX = "numeric__"
SCATTER_X = NUMERIC_PREFIX + "Initial CO2 % by vol"
SCATTER_Y = NUMERIC_PREFIX + "Final CO2 % by vol"

REQUIRED_COLUMNS = (
    *ORIGINAL_COLUMNS,
    *PROVENANCE_COLUMNS,
    *(NUMERIC_PREFIX + c for c in GAS_COLUMNS),
    "quality_flags",
    "dataset_note",
)


class DatasetError(Exception):
    pass


def parse_numeric(text: str) -> float | None:
    """Blank or non-finite/unparseable text becomes None; never 0, NaN, or Infinity."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = float(stripped)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_flags(text: str) -> list[str]:
    return [part.strip() for part in text.split(";") if part.strip()]


@dataclass
class Dataset:
    items: list[dict[str, Any]]
    numeric_columns: tuple[str, ...]
    unparsed_numeric_cells: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


def load_dataset(path: Path, expected_count: int = EXPECTED_RECORD_COUNT) -> Dataset:
    if not path.is_file():
        raise DatasetError(f"dataset file not found: {path.name}")
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise DatasetError(f"dataset file could not be read: {exc}") from exc

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise DatasetError(f"dataset is missing required columns: {missing}")
    if len(rows) != expected_count:
        raise DatasetError(f"dataset has {len(rows)} records; expected {expected_count}")

    numeric_columns = tuple(c for c in header if c.startswith(NUMERIC_PREFIX))
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    unparsed = 0
    for index, row in enumerate(rows, start=1):
        if None in row or any(v is None for v in row.values()):
            raise DatasetError(f"dataset row {index} does not match the header column count")
        record_id = row["record_id"].strip()
        if not record_id:
            raise DatasetError(f"dataset row {index} has a blank record_id")
        if record_id in seen:
            raise DatasetError(f"duplicate record_id: {record_id}")
        seen.add(record_id)

        record: dict[str, Any] = {}
        for column in header:
            text = row[column]
            if column in numeric_columns:
                value = parse_numeric(text)
                if value is None and text.strip():
                    unparsed += 1
                record[column] = value
            else:
                record[column] = text
        record["review_flags"] = parse_flags(row["quality_flags"])
        items.append(record)

    dataset = Dataset(items=items, numeric_columns=numeric_columns, unparsed_numeric_cells=unparsed)
    dataset.summary = build_summary(dataset)
    return dataset


def build_summary(dataset: Dataset) -> dict[str, Any]:
    items = dataset.items
    flag_counts: dict[str, int] = {}
    for item in items:
        for flag in item["review_flags"]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    usable = sum(1 for i in items if i[SCATTER_X] is not None and i[SCATTER_Y] is not None)
    return {
        "available": True,
        "record_count": len(items),
        "distinct_pi_count": len({i["PI"].strip() for i in items}),
        "review_flagged_count": sum(1 for i in items if i["review_flags"]),
        "review_flag_counts": flag_counts,
        "missing_numeric_counts": {
            column: sum(1 for i in items if i[NUMERIC_PREFIX + column] is None) for column in GAS_COLUMNS
        },
        "co2_scatter": {
            "x": SCATTER_X,
            "y": SCATTER_Y,
            "usable_count": usable,
            "excluded_count": len(items) - usable,
        },
        "unparsed_numeric_cells": dataset.unparsed_numeric_cells,
        "source_url": SOURCE_URL,
        "completeness_note": COMPLETENESS_NOTE,
    }
