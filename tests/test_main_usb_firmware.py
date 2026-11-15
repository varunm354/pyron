"""Host-side contract checks for firmware/main_usb.py.

This firmware runs on MicroPython and imports `machine`, which doesn't exist
on CPython, so it cannot be imported or executed here. These tests only
parse the source with `ast` (syntax only, no execution) and do static,
textual contract checks against tools/usb_bridge.py's protocol. They prove
the source is well-formed, free of forbidden imports, and shaped the way the
bridge expects -- they do NOT exercise any MicroPython runtime behavior
(ADC sampling, PWM/servo timing, LED GPIO, USB CDC stdin polling, latch
timing under real sensor noise, etc.). See the chat summary for the list of
behavior that remains untested until it runs on the real board.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tools.usb_bridge import REQUIRED_TELEMETRY_FIELDS

FIRMWARE_PATH = Path(__file__).resolve().parent.parent / "firmware" / "main_usb.py"
WIFI_FIRMWARE_PATH = Path(__file__).resolve().parent.parent / "firmware" / "mainmg.py"


def _read_source() -> str:
    return FIRMWARE_PATH.read_text(encoding="utf-8")


def _parse() -> ast.Module:
    return ast.parse(_read_source(), filename=str(FIRMWARE_PATH))


def _imported_top_level_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _module_level_name_values(tree: ast.Module) -> dict:
    """Best-effort literal values of simple `NAME = <literal>` assignments at
    module level (skips anything that isn't a plain literal, e.g. ADC(...))."""
    values = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


# --- File exists, is valid Python, doesn't touch the Wi-Fi firmware -----------


def test_firmware_file_exists_and_is_syntactically_valid():
    assert FIRMWARE_PATH.exists()
    _parse()  # raises SyntaxError if malformed


def test_wifi_firmware_file_is_untouched_sibling():
    # We only assert it still exists and is a different file; this test does
    # not (and should not) pin mainmg.py's contents.
    assert WIFI_FIRMWARE_PATH.exists()
    assert WIFI_FIRMWARE_PATH != FIRMWARE_PATH


def test_header_identifies_usb_fallback_not_wifi_path():
    header = "\n".join(_read_source().splitlines()[:5]).lower()
    assert "usb" in header
    assert "wi-fi" in header or "wifi" in header


# --- No networking, no CPython-only / concurrency modules ---------------------


def test_no_networking_modules_imported():
    modules = _imported_top_level_modules(_parse())
    forbidden = {"network", "requests", "urequests", "socket", "urllib"}
    assert modules & forbidden == set()


def test_no_asyncio_threads_dataclasses_or_typing():
    modules = _imported_top_level_modules(_parse())
    forbidden = {"asyncio", "threading", "_thread", "dataclasses", "typing"}
    assert modules & forbidden == set()


def test_no_network_or_http_literal_strings():
    source = _read_source()
    assert "http://" not in source
    assert "https://" not in source
    assert "WIFI_SSID" not in source
    assert "WIFI_PASS" not in source


# --- Non-blocking serial reads --------------------------------------------------


def test_uses_poll_for_nonblocking_stdin_not_blocking_readline():
    source = _read_source()
    assert "select" in source  # imported as uselect-or-select
    assert ".poll(" in source
    assert "sys.stdin.readline" not in source


# --- Preserved hardware constants ----------------------------------------------


def test_pin_assignments_match_existing_wiring():
    constants = _module_level_name_values(_parse())
    assert constants["MQ2_PIN"] == 1
    assert constants["INLET_PIN"] == 42
    assert constants["OUTLET_PIN"] == 41
    assert constants["RED_PIN"] == 40
    assert constants["YELLOW_PIN"] == 39
    assert constants["GREEN_PIN"] == 21


def test_adc_11db_attenuation_preserved():
    assert "ATTN_11DB" in _read_source()


def test_median_of_five_sampling_preserved():
    assert "range(5)" in _read_source()
    assert "sorted(" in _read_source()


def test_default_config_matches_known_good_defaults():
    tree = _parse()
    default_config = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "config"
        ):
            default_config = ast.literal_eval(node.value)
            break
    assert default_config is not None, "no module-level `config = {...}` default found"

    # Ronny's bench measurements with the project's MQ-2/divider circuit,
    # not NASA-derived (NASA data does not supply MQ-2 thresholds).
    assert default_config["warn_adc"] == 80
    assert default_config["alarm_adc"] == 120
    assert default_config["clear_adc"] == 70
    assert default_config["damper_deg"] == {"open": 90, "half": 45, "closed": 0}
    assert default_config["response"] == {"ok": "open", "warn": "half", "alarm": "closed"}
    assert default_config["mode"] == "nasa"
    assert default_config["simulate"] is False


def test_alarm_latch_and_simulate_logic_preserved():
    source = _read_source()
    assert "latched" in source
    assert 'config["clear_adc"]' in source
    assert 'config["alarm_adc"]' in source
    assert 'config.get("simulate")' in source


# --- stdout is reserved for NDJSON telemetry only ------------------------------


def test_no_print_builtin_used_for_debug_logging():
    # This firmware must never write anything to stdout except one telemetry
    # NDJSON line per cycle (via sys.stdout.write), because the bridge parses
    # every line it reads from the board as JSON.
    tree = _parse()
    print_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert print_calls == []


def test_telemetry_is_written_via_stdout_write_with_trailing_newline():
    source = _read_source()
    assert "sys.stdout.write" in source


# --- Protocol parity with tools/usb_bridge.py ----------------------------------


def test_telemetry_fields_match_bridge_required_fields():
    source = _read_source()
    assert '"type": "telemetry"' in source
    for field in REQUIRED_TELEMETRY_FIELDS:
        assert f'"{field}"' in source, f"telemetry field {field!r} missing from firmware source"


def test_device_name_matches_sample_used_elsewhere_in_the_repo():
    assert "pyron-node-1" in _read_source()


def test_accepts_config_message_type():
    source = _read_source()
    assert '"config"' in source
    assert 'message.get("type")' in source
    assert 'message.get("config")' in source


def test_config_validation_rejects_incomplete_or_malformed_input():
    source = _read_source()
    assert "is_valid_config" in source
    assert "isinstance(candidate, dict)" in source
    # Required config keys mirrored from the backend's /config contract
    # (airflow_cm_s is intentionally excluded -- this firmware never uses it,
    # matching mainmg.py's existing behavior).
    for key in (
        "version",
        "mode",
        "simulate",
        "warn_adc",
        "alarm_adc",
        "clear_adc",
        "damper_deg",
        "response",
    ):
        assert f'"{key}"' in source


# --- Timing contracts (static, not executed) ------------------------------------


@pytest.mark.parametrize("expected_ms", [100, 1000])
def test_loop_timing_constants_present(expected_ms):
    source = _read_source()
    assert f">= {expected_ms}" in source
