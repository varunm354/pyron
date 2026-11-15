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

`pypdf` is used only by the ingestion script. The server reads the processed corpus.

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

### USB serial bridge (Mac-side, Wi-Fi fallback)

While ESP32 firmware is migrated off Wi-Fi, the ESP32 stays USB-connected to
the Mac and speaks newline-delimited JSON (NDJSON) over serial instead of
making HTTP calls itself. `tools/usb_bridge.py` is a small Mac-side process
that sits between the two and does the HTTP talking on the device's behalf.
It does not change `/config` or `/telemetry`, does not reclassify telemetry,
and never fabricates a reading.

```
┌──────────┐   USB serial (NDJSON, 115200 baud)   ┌──────────────────┐   HTTP   ┌──────────────────┐
│  ESP32   │ ───────────────────────────────────► │  tools/usb_bridge │ ───────► │  FastAPI backend  │
│ (plugged │ {"type":"telemetry", ...}             │       .py          │ POST    │  /telemetry       │
│  into    │ ◄─────────────────────────────────── │  (this Mac)        │ ◄─────── │  GET /config       │
│  the Mac)│ {"type":"config","config":{...}}      │                     │ GET     │                    │
└──────────┘                                        └──────────────────┘          └──────────────────┘
```

**Setup** (adds `pyserial` to the existing backend requirements):

```sh
.venv/bin/python -m pip install -r backend/requirements.txt
```

**Run** (with the backend already running separately, and the ESP32 plugged
into USB):

```sh
.venv/bin/python -m tools.usb_bridge --port /dev/cu.usbmodem1101 --baud 115200 --backend http://127.0.0.1:8000
```

Find the ESP32's serial device name with `ls /dev/cu.*` while it is plugged
in. Stop the bridge with Ctrl+C at any time; it closes the serial port and
exits.

**Serial protocol** (one JSON object per line in each direction):

- ESP32 → Mac (telemetry): `{"type":"telemetry","device":"...","uptime_ms":...,"mq2_raw":...,"state":"...","damper":"...","damper_deg":...,"config_version":...}`
- Mac → ESP32 (config): `{"type":"config","config":{...exact response from GET /config...}}`

**Behavior:**

- At startup, and every 2 seconds after that, the bridge fetches `GET /config`
  and writes it to the ESP32 as a `{"type":"config",...}` line, unchanged.
- It reads serial continuously. Each well-formed `{"type":"telemetry",...}`
  line is forwarded as-is to `POST /telemetry`, and the HTTP status is logged.
- Blank lines, malformed JSON, unknown message types, and telemetry missing
  required fields are logged and dropped, not forwarded, and never crash the
  bridge. A backend or serial error is logged and retried; it does not stop
  the bridge or invent data to fill the gap.
- The bridge never calls an LLM, never mutates the config it forwards, and
  never fabricates a telemetry reading.

**Limitation:** this is a USB fallback. The ESP32 must remain physically
connected to the Mac over USB for the whole demo — if it is unplugged, the
bridge has no transport to read telemetry from or write config to until it is
reconnected (`--port` must still resolve to a real device).

**Firmware (device side).** `firmware/main_usb.py` is the USB-only sibling of
`firmware/mainmg.py` (the Wi-Fi firmware, left unchanged): same pins, same
ok/warn/alarm state machine, same alarm latch, same defaults, but it talks
NDJSON over USB serial instead of Wi-Fi/HTTP. It accepts
`{"type":"config","config":{...}}` lines, applying only complete/well-shaped
ones and otherwise keeping the last known-good config; it emits
`{"type":"telemetry",...}` once a second as the *only* thing it writes to
stdout, because the bridge parses every line it reads from the board as JSON.
It is covered on the host side by `tests/test_main_usb_firmware.py` (static
source/contract checks — see that file's docstring for exactly what is and
isn't exercised without real hardware).

Do **not** upload or run it yet. Only once the board is physically back, a
USB-only bench test (no Wi-Fi, `main_usb.py` driving the real GPIO pins) has
passed, and Ronny has confirmed the GPIO wiring matches the pins above, flash
it with:

```sh
.venv/bin/mpremote connect /dev/cu.usbmodemXXXX cp firmware/main_usb.py :main.py
```

(replace `/dev/cu.usbmodemXXXX` with the board's actual serial device, e.g.
from `ls /dev/cu.*` while it's plugged in).

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

**Ask NASA research** searches three NASA documents and shows short excerpts with
PDF page links (see [NASA document research](#nasa-document-research-phase-4)). It
works in both views and never uses telemetry.

**How to read NASA evidence here** separates NASA observations, project
interpretation, and prototype calibration, and lists the scientific limits: NASA data
does not supply MQ-2 thresholds, closing dampers is not shown to guarantee
extinction, the cardboard habitat does not reproduce microgravity, and microgravity
results do not directly validate lunar or Martian partial-gravity performance.

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

### NASA document research (Phase 4)

Sources (official NASA Technical Reports Server PDFs):

| ID | NTRS | Title |
| --- | --- | --- |
| `bass2-summary` | [20210011385](https://ntrs.nasa.gov/citations/20210011385) | BASS-II Summary Report |
| `bass2-results` | [20160000593](https://ntrs.nasa.gov/citations/20160000593) | Combustion of Solids in Microgravity: Results from the BASS-II Experiment |
| `saffire-results` | [20170008805](https://ntrs.nasa.gov/citations/20170008805) | Results of Large-Scale Spacecraft Flammability Tests |

**Ingestion** (needs internet only the first time):

```sh
.venv/bin/python -m backend.ingest            # download missing PDFs, then build the corpus
.venv/bin/python -m backend.ingest --offline  # rebuild from PDFs already in data/papers/
```

- PDFs are saved to `data/papers/` (gitignored; the BASS-II summary is about 49 MB).
  Existing files are reused. Downloads use timeouts, and each file must start with
  `%PDF-`. A non-PDF file already in place is reported, not overwritten.
- If a download fails, the script names the failed source, records it as `missing`
  in the corpus, and exits with status 1. To add a PDF by hand, save it in `data/papers/`
  under the filename the script prints, then rerun. No other document is substituted.
- Text is extracted page by page with `pypdf`, and whole sentences are packed into
  chunks of about 110 words (170 maximum) **within a single page**. Each chunk keeps
  `chunk_id` (`<doc>-p<page>-c<n>`), document ID, title, NTRS record URL, PDF URL, and
  the **1-based PDF page index** (not the printed page label). The corpus is written
  to `data/processed/nasa_corpus.json`.
- Pages are classified and reported as empty (no extractable text), poorly extracted
  (mostly non-letters: tables of contents, dense tables), or sparse (under 40 words).
  Empty pages produce no chunks. Chunks from poorly extracted pages stay in the corpus
  but are excluded from search. Current result: 540 chunks, of which 488 are
  searchable. Pages with figures or scanned content are not OCR'd.
- Symbol-font bullets (Unicode private-use characters) are written as `•`. Apart from
  that and whitespace normalization, chunk text is the extracted text.

**Retrieval** is local TF-IDF with cosine similarity, built in memory at startup. It
uses no network, embeddings, or vector database. It returns at most 5 chunks
(2 per page). A chunk must reach a minimum score and match enough of the question's
distinctive words, counting words that never appear in the documents. Off-topic
questions (for example "What is the capital of France?" or "What is the MQ-2 threshold
for alarm?") therefore return `insufficient_evidence` instead of weak matches.
Distinctive terms not covered by the top results get their best chunk added, so
comparisons such as Saffire vs. BASS-II show both experiments. Displayed excerpts are
at most about 300 characters around the best-matching sentence.

Tokenization splits hyphens and known compounds (`airflow` → `air` + `flow`) in both
the index and the question. Soft query-side expansions bridge ordinary wording to NASA
terminology (`persist`/`survive`, `quench`/`extinction`, and extra low-flow + flame
terms) without hardcoding answers. Expansion weights stay below the user's own words,
and out-of-vocabulary question terms still enforce the off-topic coverage floor.

**Without API credentials** (the default), `/ask` returns `mode: "retrieval"`,
`answer: null`, and the ranked excerpts. The dashboard labels this "EVIDENCE SEARCH ·
not an AI answer". No canned answer is generated.

**Optional AI answers (OpenAI).** Copy `.env.example` to `.env` (gitignored) and set
both values, or export them in the shell (environment variables win), then restart
the server:

```sh
OPENAI_API_KEY=...   # your own OpenAI API key
OPENAI_MODEL=...     # a model on your account that supports structured outputs
```

You must supply your own OpenAI API key; a Cursor subscription does not provide one
for this application. No model is chosen by default. When both are set:

- Only the retrieved excerpts and the question are sent. Telemetry, configuration, and
  the key itself are never included in the prompt or returned by any endpoint.
- The system prompt treats excerpts as untrusted evidence, never as instructions, and
  states the scientific limits above. The model must return strict JSON with at most
  6 short statements, each listing supporting `chunk_ids`.
- The server rejects the whole answer if any statement has no citation or cites a
  chunk that was not retrieved for this question. It then falls back to evidence
  search with `status: "invalid_citations"`.
- Requests run in FastAPI's worker threads (not the event loop), with a 45 s timeout
  and a 2500-token output cap (reasoning models spend hidden reasoning tokens from it;
requests use `reasoning_effort: "low"`). On timeout, HTTP error, refusal, truncation, or invalid
  JSON, `/ask` returns the evidence with `status: "provider_error"`. Model output is
  display-only and cannot change configuration or hardware.
- A valid citation ID shows that the cited chunk exists and was retrieved, not that it
  supports the statement. Manually check generated answers against the cited pages.
- **Not yet verified against the real API:** no key/model was configured during
  development. The OpenAI request/response handling is tested only with mock
  transports.

`/ask` status values: `evidence_only`, `rag_answer`, `insufficient_evidence`,
`model_insufficient_evidence`, `invalid_citations`, `provider_error`, and
`corpus_unavailable` (HTTP 503). If the corpus file is missing or invalid, `/sources`
reports `available: false`, `/ask` returns 503, and all other endpoints keep working.

### Test

```sh
.venv/bin/python -m pytest
```

Tests use temporary config files and never modify `backend/config.json`. Research
tests use the committed corpus, fake generators, and mock HTTP transports; they make
no network calls. The test that checks chunk text against its PDF page is skipped if
the PDFs are not in `data/papers/`.

`tools/usb_bridge.py` is covered by `tests/test_usb_bridge.py`, using a fake
in-memory serial port and a mocked HTTP transport (`httpx.MockTransport`). These
tests never open a real serial port, never require the ESP32 to be attached, and
never call the live backend.

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
| GET | `/experiments` | All 129 imported BASS-II records with provenance |
| GET | `/experiments/summary` | Record, PI, flag, missing-value, and CO₂-plot counts |
| GET | `/sources` | NASA documents, ingestion status, page/chunk counts, AI status |
| POST | `/ask` | `{"question": "..."}` (1–500 chars): cited excerpts, plus an AI answer if configured |

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
