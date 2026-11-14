# Pyron
NASA Space Apps prototype combining a combustion research dashboard with an ESP32 habitat demonstration.

## Backend (Phase 1)

A small FastAPI server for the ESP32 habitat node. The ESP32 is the HTTP client: it
fetches `GET /config` every 2 seconds and posts `POST /telemetry` every second. The
ESP32 decides ok/warn/alarm itself; the backend stores the state it reports and
does not reclassify it.

### Setup

Requires Python 3.10+ (tested with 3.12). From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
```

### Run

From the repository root:

```sh
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Telemetry is kept in memory, so run a single worker (the default; do not pass
`--workers`). `--host 0.0.0.0` makes the server reachable from the ESP32 on the
same phone hotspot. Point the firmware at `http://<your-mac-ip>:8000`; find the IP
with `ipconfig getifaddr en0`. macOS may ask you to allow incoming connections for
Python the first time.

### Dashboard

With the server running, open [http://localhost:8000/](http://localhost:8000/) on the
Mac, or `http://<your-mac-ip>:8000/` from another device on the hotspot. The
dashboard is plain HTML/CSS/JS in `dashboard/`, served by the same FastAPI app
(`GET /` and `/static/`). There is no second frontend server, it uses no external
assets, and it works offline.

**Hardware view** (default) shows what the ESP32 reports:

- It polls `/telemetry/latest` and `/config` about once per second, one cycle at a
  time. It refreshes the chart from `/telemetry/history` every 5 seconds, keeping at
  most 120 points.
- Backend and ESP32 connection are shown separately. With no telemetry it shows
  "Waiting for ESP32". When the device goes offline, the last reading stays visible
  and is marked STALE. If the backend is unreachable, a banner says so and ESP32
  status is shown as unknown.
- State and damper position are exactly what the ESP32 reports; nothing is inferred
  from gas readings or configuration. The MQ-2 reading is raw ADC counts (0–4095),
  not ppm. The damper angle is the commanded servo angle, not a measured position.
  Airflow is a provisional estimate and shows "Unavailable" when the backend has no
  estimate for the device's reported config version.
- Controls change the backend configuration for connected hardware:
  - **NASA-inspired response** / **Ventilation-on comparison** call
    `POST /config/mode`. The ventilation-on comparison is a demo baseline, not a
    recommended Earth fire procedure.
  - **Force alarm on ESP32** calls `POST /config/simulate`. The ESP32 picks it up on
    its next config fetch, reports ALARM, and applies the current mode's alarm
    response (NASA-inspired closes the dampers; ventilation-on leaves them open).
  - After a change, the dashboard shows "Pending device confirmation" until telemetry
    reports the new `config_version`. A successful update does not mean the dampers
    have moved.

**Demo preview** is a browser-only scenario labeled "DEMO — synthetic readings".
Start scenario runs a repeatable 24-second OK → WARNING → ALARM sequence with a gas
chart and animated dampers for both response modes side by side. Reset clears it.
The demo never calls the backend. It does not post telemetry, change configuration,
or show the ESP32 as connected, and hardware polling pauses while it is open.
Leaving the preview stops its timer and discards its synthetic data. You can open
the preview directly at `http://localhost:8000/#demo`.

**BASS-II experiment explorer** appears in both views and shows NASA experiment
records (see [NASA BASS-II dataset](#nasa-bass-ii-dataset)). It has search across PI,
test number, and fuel material, a PI filter, a paginated table, and per-record
details. It is fetched once per page load, never in the hardware polling loop.

**NASA evidence** notes that NASA experimental records are now available, while
paper-based interpretation is still pending. No findings, citations, flame-spread
curves, or extinction thresholds are stated or plotted.

### NASA BASS-II dataset

- **File:** `data/processed/bass_ii_combined.csv` (UTF-8 with BOM). It is read once at
  server startup, from a path resolved relative to the project source, not the shell
  directory. The server never modifies, regenerates, or repairs it.
- **Source:** [NASA PSI-25 (BASS-II)](https://psi.nasa.gov/physci/repo/data/investigations/PSI-25).
- **Provenance:** each record keeps the 17 original NASA table columns as their original
  cell text, plus `record_id` (stable identifier), `source_file`, `source_row`,
  `source_url`, `quality_flags`, and `dataset_note`.
- **Numeric fields:** `numeric__` columns are used for calculations and the chart. Blank
  or nonnumeric values are returned as `null`, never 0. Negative values are kept as
  they are. Values are always finite JSON numbers (never NaN or Infinity). `Fan display`
  and `Air display` are mixed free text; no airflow value or unit is extracted from them.
- **Completeness:** 129 records were imported, but the source website displayed 130.
  This is an imported subset with completeness pending reconciliation; no record has
  been invented to fill the gap.
- **Review flags:** 17 records carry source-review flags: negative initial CO values,
  final O₂ above 30%, and dates in 2018. These mark values to check against the
  source; they are not proven errors, and flagged records stay in the table and chart.
- **Validation:** if the file is missing or invalid (wrong row count, missing required
  columns, or duplicate/blank `record_id`), `/experiments` and `/experiments/summary`
  return HTTP 503 with `"available": false` and an `error` message. The dashboard
  shows that message in the NASA section, and the hardware endpoints keep working.

API:

- `GET /experiments` returns `{"items", "count", "source_url", "completeness_note"}`.
- `GET /experiments/summary` returns:
  - the record count, distinct PI count, and flagged-record count;
  - missing-value counts for the six gas columns;
  - the CO₂ scatter's usable/excluded counts (currently 121 / 8);
  - the source URL and completeness note.

**Chart: "BASS-II initial vs final CO₂".** Each point is one record, plotting
`numeric__Initial CO2 % by vol` (x) against `numeric__Final CO2 % by vol` (y) on the same
scale for both axes. The dashed y = x line marks no change. Records missing either value
are excluded, and the plotted/excluded counts are shown for the current search and PI
filter. Records with identical values share an enlarged point labeled ×n. Hovering shows
the records at a point, clicking pins them with links to full details, and arrow keys move
between points. The chart compares before/after measurements across experiments with
different materials and test conditions. It does not establish a causal airflow
relationship. This imported table contains no explicit flame-spread measurements. Live
MQ-2 readings are never overlaid on NASA data or calibrated against NASA CO
measurements.

### Test

```sh
.venv/bin/python -m pytest
```

Tests use temporary config files and never modify `backend/config.json`.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | `{"ok": true}` |
| GET | `/config` | Current configuration for the ESP32 |
| POST | `/config/mode` | `{"mode": "nasa"}` or `{"mode": "earth"}` |
| POST | `/config/simulate` | `{"on": true}` or `{"on": false}` |
| POST | `/telemetry` | Telemetry record from the ESP32 |
| GET | `/telemetry/latest` | Latest record, its age, and online status |
| GET | `/telemetry/history` | Last 120 records in server receipt order |

Notes:

- `online` is true only if the latest telemetry arrived less than 5 seconds ago
  (measured with a monotonic clock).
- Each stored record keeps every field the device sent and adds `received_at`
  (server UTC time), `airflow_cm_s`, and `airflow_estimated: true`. `received_at`
  reflects arrival order, not necessarily device sampling order.
- Airflow is looked up using the `config_version` the device reports. If that
  version was not used during the current server session, `airflow_cm_s` is `null`.
  Airflow values are estimates, not measurements.
- `simulate: true` tells the ESP32 to report alarm. The ESP32 still applies
  `response["alarm"]` for the current mode: NASA mode closes the dampers, Earth
  mode leaves them open. The backend never fabricates telemetry; until telemetry
  reports the new `config_version`, the hardware has not confirmed the change.
- Mode and simulation changes increment `version` only when something actually
  changes, and are written atomically to `backend/config.json`.

### Editing calibration values

The thresholds, damper angles, and airflow values in `backend/config.json` are
provisional. To change them:

1. Stop the server.
2. Edit `backend/config.json` (`clear_adc`, `warn_adc`, `alarm_adc`, `damper_deg`,
   `airflow_cm_s`).
3. Increment `version` so the ESP32 and stored telemetry can tell the new
   calibration apart from the old one.
4. Restart the server with the launch command above.

The server refuses to start if the file is invalid. Rules:

- `0 <= clear_adc < warn_adc < alarm_adc <= 4095` (integers, raw 12-bit ADC).
- `damper_deg` has integer `open`, `half`, `closed` angles from 0 to 180.
- `response` maps `ok`, `warn`, `alarm` to `open`, `half`, or `closed`.
- `airflow_cm_s` has non-negative numbers for `open`, `half`, `closed`.
- `mode` is `nasa` or `earth`; `simulate` is `true` or `false`.
- No other keys are allowed, and the file must stay under 2 KB. Keep sources and
  rationale out of this file.
