# pyron node firmware -- USB-serial fallback (NOT the Wi-Fi path)
#
# This is firmware/main_usb.py, the USB-only sibling of firmware/mainmg.py.
# mainmg.py (the Wi-Fi firmware) is left completely untouched; this file
# exists for the demo configuration where the ESP32-S3 stays plugged into
# the Mac over USB instead of joining Wi-Fi.
#
# Target: ESP32-S3 N16R8, MicroPython v1.29.0.
# No `network`, `requests`, `urequests`, sockets, or any other networking --
# this firmware makes no network calls at all. Config and telemetry travel
# over the USB serial link as newline-delimited JSON (NDJSON), read and
# written on the other end by tools/usb_bridge.py running on the Mac:
#
#   Mac -> board (config):    {"type": "config", "config": {...GET /config...}}
#   board -> Mac (telemetry): {"type": "telemetry", "device": ..., ...}
#
# stdout carries ONLY telemetry NDJSON lines. The bridge treats every line
# this board prints as a JSON message, so nothing else (no debug prints, no
# banners) may go to stdout here.
#
# Do not upload or run this on hardware until: the board is physically back,
# a USB-only bench test has passed, and Ronny has confirmed the GPIO wiring.
# See the README for the exact upload command and that checklist.

import sys
import time
import json
from machine import Pin, PWM, ADC

try:
    import uselect as select
except ImportError:
    import select

DEVICE = "pyron-node-1"

# pins, all on the right-hand header of the board (unchanged from mainmg.py)
MQ2_PIN = 1        # adc1, through 10k/20k divider
INLET_PIN = 42
OUTLET_PIN = 41
RED_PIN = 40
YELLOW_PIN = 39
GREEN_PIN = 21

# defaults used until a valid config arrives over USB (all placeholders).
# These are the same numbers mainmg.py starts with, so behavior before the
# first config line arrives is identical between the two firmwares.
config = {
    "version": 0,
    "mode": "nasa",
    "simulate": False,
    "warn_adc": 80,
    "alarm_adc": 120,
    "clear_adc": 70,
    "damper_deg": {"open": 90, "half": 45, "closed": 0},
    "response": {"ok": "open", "warn": "half", "alarm": "closed"},
}
# Only these fields are read from an incoming config message and copied into
# `config`; anything else in the message (e.g. airflow_cm_s) is ignored, same
# as mainmg.py's "only keep the fields the esp32 uses" rule.
REQUIRED_CONFIG_KEYS = (
    "version",
    "mode",
    "simulate",
    "warn_adc",
    "alarm_adc",
    "clear_adc",
    "damper_deg",
    "response",
)
DAMPER_NAMES = ("open", "half", "closed")

# hardware setup
adc = ADC(Pin(MQ2_PIN))
adc.atten(ADC.ATTN_11DB)

inlet = PWM(Pin(INLET_PIN), freq=50)
outlet = PWM(Pin(OUTLET_PIN), freq=50)

red = Pin(RED_PIN, Pin.OUT)
yellow = Pin(YELLOW_PIN, Pin.OUT)
green = Pin(GREEN_PIN, Pin.OUT)

# Non-blocking USB serial input: poll stdin so reading never pauses the
# 100 ms sensor/control loop. Complete lines are assembled a character at a
# time into _rx_buf and only handed off once a newline is seen.
_poll = select.poll()
_poll.register(sys.stdin, select.POLLIN)
_rx_buf = ""
MAX_LINE_LEN = 4096  # generous margin over the backend's 2048-byte config cap


# turn an angle into a servo pulse
def set_angle(servo, angle):
    us = 500 + (angle / 180) * 1900
    servo.duty_ns(int(us * 1000))


# median of 5 reads, scaled to 0-4095
def read_mq2():
    vals = sorted(adc.read_u16() >> 4 for _ in range(5))
    return vals[2]


# alarm latches until the reading drops below clear_adc
latched = False


def classify(raw):
    global latched
    if latched and raw < config["clear_adc"]:
        latched = False
    if raw >= config["alarm_adc"]:
        latched = True
    # simulate forces alarm without changing the latch, so the real reading
    # keeps driving the latch underneath the forced state
    if config.get("simulate"):
        return "alarm"
    if latched:
        return "alarm"
    if raw >= config["warn_adc"]:
        return "warn"
    return "ok"


def show_leds(state):
    red.value(1 if state == "alarm" else 0)
    yellow.value(1 if state == "warn" else 0)
    green.value(1 if state == "ok" else 0)


def poll_serial_lines():
    """Drain whatever is currently buffered on USB stdin and return a list of
    complete lines (newline stripped). Never blocks: this only acts on bytes
    that have already arrived, so a quiet or disconnected bridge never stalls
    the sensor/control loop. A trailing partial line is kept in _rx_buf for
    the next call."""
    global _rx_buf
    lines = []
    while _poll.poll(0):
        ch = sys.stdin.read(1)
        if not ch:
            break
        if ch == "\n":
            lines.append(_rx_buf)
            _rx_buf = ""
        elif ch != "\r":
            _rx_buf += ch
            if len(_rx_buf) > MAX_LINE_LEN:
                # Runaway line with no newline in sight -- drop it instead of
                # growing _rx_buf without bound.
                _rx_buf = ""
    return lines


def is_valid_config(candidate):
    """True only if `candidate` has every field this firmware needs, with the
    right shapes. Anything else (wrong type, missing keys, partial damper_deg)
    is rejected so a bad message can never corrupt the known-good config."""
    if not isinstance(candidate, dict):
        return False
    for key in REQUIRED_CONFIG_KEYS:
        if key not in candidate:
            return False
    if not isinstance(candidate["damper_deg"], dict):
        return False
    if not isinstance(candidate["response"], dict):
        return False
    for name in DAMPER_NAMES:
        if name not in candidate["damper_deg"]:
            return False
    return True


def apply_config(candidate):
    # Only a complete, well-shaped config replaces the in-memory config.
    # Everything else leaves the last known good config -- the defaults
    # above, until the first valid one arrives -- in control.
    if not is_valid_config(candidate):
        return
    for key in REQUIRED_CONFIG_KEYS:
        config[key] = candidate[key]


def handle_serial_line(line):
    """Parse one line from the Mac bridge. Ignores -- without raising --
    blank lines, malformed JSON, non-object JSON, unknown message types, and
    config messages that aren't valid. Only a well-formed
    {"type": "config", "config": {...}} message ever changes `config`."""
    line = line.strip()
    if not line:
        return
    try:
        message = json.loads(line)
    except ValueError:
        return  # malformed JSON
    if not isinstance(message, dict):
        return  # e.g. a bare number, string, list, null
    if message.get("type") != "config":
        return  # this firmware only acts on "config" messages over USB
    apply_config(message.get("config"))


def send_telemetry(raw, state, damper, deg):
    # The only thing this firmware ever writes to stdout: one NDJSON
    # telemetry line, matching tools/usb_bridge.py's expected shape exactly.
    sys.stdout.write(
        json.dumps(
            {
                "type": "telemetry",
                "device": DEVICE,
                "uptime_ms": time.ticks_ms(),
                "mq2_raw": raw,
                "state": state,
                "damper": damper,
                "damper_deg": deg,
                "config_version": config["version"],
            }
        )
    )
    sys.stdout.write("\n")


# start with dampers open
damper = "open"
deg = config["damper_deg"]["open"]
set_angle(inlet, deg)
set_angle(outlet, deg)
state = "ok"
show_leds(state)
raw = read_mq2()

last_read = last_telemetry = time.ticks_ms()

while True:
    now = time.ticks_ms()

    # USB serial in: drains whatever has arrived this tick, never blocks.
    for line in poll_serial_lines():
        handle_serial_line(line)

    # sensor + local control every 100 ms -- identical whether or not a
    # bridge is currently listening on the other end of the USB cable
    if time.ticks_diff(now, last_read) >= 100:
        last_read = now
        raw = read_mq2()
        new_state = classify(raw)
        target = config["response"].get(new_state, "open")
        target_deg = config["damper_deg"].get(target, 90)
        if new_state != state:
            state = new_state
            show_leds(state)
        if target != damper or target_deg != deg:
            damper = target
            deg = target_deg
            set_angle(inlet, deg)
            set_angle(outlet, deg)

    # telemetry out every 1 s, regardless of whether a bridge is listening
    if time.ticks_diff(now, last_telemetry) >= 1000:
        last_telemetry = now
        send_telemetry(raw, state, damper, deg)

    time.sleep_ms(10)
