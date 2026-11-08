# FACW — سامانه شناسایی چهره و کنترل دسترسی

**FACW** (Face Access Control Watch) is a complete, self-contained, on-premise
face-recognition access-control system. It watches cameras, identifies people,
logs every event with a permanent audit trail, actuates physical door relays,
and exposes a Persian RTL operator console — all on one host with no cloud and
no internet at runtime.

```
Live cameras ──► person detection ──► face embedding ──► match index
                                                              │
        events (frames/faces/persons) ◄── vote resolution ◄───┘
              │                              │
     console / reports / PDF            access control (relay, 2 channels)
              │                              │
          audit log ─────────────────────────┘   (nothing is ever lost)
```

---

## 1. What is inside

| Layer | Components |
|---|---|
| Capture | RTSP / HTTP-MJPEG / local webcam / file sources; newest-frame single-slot buffer; fail-fast host probe; exponential reconnect; viewer reference counting |
| Vision | **YuNet** face detection, **SFace** 128-D embeddings, MediaPipe person detector (ONNX via `cv2.dnn`), dHash+HSV image fingerprints |
| Identity | vectorised match index (top-K mean per identity), ambiguity margin, threshold; **one row per reference photo**, sealed vectors |
| Tracking | per-person tracks, sample voting (strict majority), TTL sweep, dedup windows, zone masking/attribution |
| Access control | two relay channels, ordered pulse, one-failure isolation, **fail-closed** (only role `approve` unlocks) |
| Storage | SQLite (WAL); events with `frames/`, `faces/`, `persons/` JPEG artefacts; sealed biometrics; zone geometry |
| Console | FastAPI SPA: dashboard mosaic, reports (Jalali date filters, sort, pagination), cameras + discovery, people/enrolment with face picker, settings/users |
| Reporting | Persian RTL multi-page PDF (index + per-row detail with thumbnails), report lock |
| Audit | every sensitive action: sign-in, enrolment, settings change, zone save, relay pulse, backup, delete |
| Tools | `service_control`, `enrolment_helper` (Tk), `zone_editor` (Tk), `autostart` |

---

## 2. Prerequisites

- **Windows 10/11** (64-bit) or any recent Linux/macOS with Python 3.10+.
- Python 3.10+ and pip.
- One-time internet access to install packages (build time only; see §9 for a
  fully offline install from a pre-built wheelhouse).

No Docker, Node, or GPU is required. Everything runs on the CPU.

---

## 3. Installation

```bash
cd facw
python -m venv .venv
# Windows:
.venv\Scripts\activate
# POSIX:
# source .venv/bin/activate

pip install -r requirements.txt
```

Model files are included under `models/`:

| File | Purpose |
|---|---|
| `models/yunet.onnx` | face detector |
| `models/sface.onnx` | face recogniser (128-D) |
| `models/persondet.onnx` | person detector |

> If `models/` is empty, run `python scripts/fetch_models.py` once (needs
> internet) or copy the three files from the opencv_zoo release.

---

## 4. First run

```bash
# create the database, seed an admin + observer, start the service on :8770
python -m facw.tools.service_control start
```

The first start bootstraps `data/facw.db` and creates two default accounts:

| Login | Password | Role |
|---|---|---|
| `admin` | `admin12345` | administrator (full access) |
| `observer` | `observer12345` | observer (view-only, raw video) |

**Change these immediately** — see §7.4.

Open the console: <http://127.0.0.1:8770>

Other commands:

```bash
python -m facw.tools.service_control status
python -m facw.tools.service_control stop
python -m facw.tools.service_control restart
python -m facw.tools.service_control logs -n 60
```

A convenience wrapper exists too: `scripts/devrestart.sh [port]`.

### 4.1 Start automatically after a reboot

```bash
python -m facw.tools.autostart install     # logon task (Windows) / systemd user unit (Linux)
python -m facw.tools.autostart status
python -m facw.tools.autostart remove
```

The service opens the data store itself, so one launch brings the whole system
up.  (Nothing is registered unless you run `install`.)

---

## 5. Demo walk-through (5 minutes)

The project ships a synthetic clip and seed script so you can see the full
pipeline working before wiring real cameras.

```bash
python scripts/make_demo_clip.py --seconds 14 --fps 15   # data/temp/livecam.avi
python scripts/seed_demo.py                               # identities + demo camera
python -m facw.tools.service_control restart
```

Then:

1. Open the console, sign in as `admin`.
2. **مانیتورینگ** — the tile shows the clip live; a person is detected, tracked,
   identified and the box turns green with the name.
3. **گزارش‌ها** — the identified events appear; open one to see the stored frame,
   face crop and person crop; export the Persian PDF.
4. Enable access control in **تنظیمات** (relay in simulation) and watch the
   relay log fill with two channel pulses per approved appearance.

To watch an *unknown* visitor, enrol a person (اشخاص → «+ ثبت شخص جدید»), or
delete the demo identity.

---

## 6. Architecture

### 6.1 Data flow (stages from the specification)

```
capture thread (per camera) ──frames──► worker (per camera)
                                              │
   person detector → NMS → track update ──────┤
                                              ▼
   zone mask/attribution → face detect (YuNet) → quality gate
                                              │
                                       SFace embed
                                              ▼
   MatchIndex.match(embed) ──► votes ──► resolve_votes (strict majority)
                                              ▼
                                     commit → store event + artefacts
                                              ▼
                        access control (role==approve) → relay pulse
                                              ▼
                                  notifier (SMTP/log) — never blocks
```

### 6.2 Threads

- **Capture**: one daemon thread per camera (`capture.py::CameraStream`), a
  newest-frame single-slot buffer so slow analysis never backlogs.
- **Worker**: one per camera (`engine.py::PipelineWorker`), the full analysis
  above; a crash restarts the worker, never the service.
- **Event writer**: one bounded-queue thread persisting events and artefacts;
  the video path never blocks on disk.
- **Sweeper**: track TTL/reconnect housekeeping.
- **Streaming**: `/video/{id}` MJPEG and `/video/{id}/snapshot` are served
  per-request; the console **polls snapshots** instead of holding one long-lived
  multipart socket per tile (browsers cap ~6 connections per host — six live
  streams would starve every other API call).

### 6.3 Storage

- SQLite in **WAL** mode, one connection per thread.
- `face_refs.vector_sealed` — embeddings are AES-256-GCM sealed with a key in
  `data/.sealkey`; reading vectors for the index is the *only* unseal path and
  is itself audited (`biometric_access`).
- Event artefacts are written under `data/media/{frames,faces,persons}/` and
  served through `/media/{kind}/{name}` with a strict path allow-list.
- Backups: `POST /api/backup` writes a consistent SQLite snapshot + artefacts
  into `data/backups/`; `integrity_check` runs on boot.

### 6.4 Matching

- The index stores every reference row as a normalised vector.
- A query is one matrix-vector product; per-identity score = mean of its top-K
  rows; identities are grouped by **identity id** (not display name, so two
  people can share a name and duplicate rows can't make a match ambiguous).
- Decision: below `match_threshold` → `unknown`; within `ambiguity_margin` of
  the runner-up → `unknown` (refuse to choose); otherwise the winner.
- A track commits only after `samples_required` votes with a **strict majority**
  of the same identity.

### 6.5 Zones

- Zone geometry is normalised `0..1`, resolution-independent; kinds: polygon,
  rectangle, line.
- When `zone_mode` is on, only detections whose **box centre** falls inside a
  zone are tracked; the zone is bound to a relay channel for per-zone access.

---

## 7. Operations

### 7.1 Cameras

- **کشف (discovery)**: WS-Discovery + a rate-limited, cancellable TCP sweep of
  the local /24; each found device can be added directly, and its RTSP URL can
  be fetched via ONVIF with WS-Security credentials.
- Each camera has a stream type (`rtsp` / `http` / `webcam` / `file`), a
  gate label, an optional `stream_address` (auto-built when blank) and a
  per-camera analysis toggle.
- `POST /api/cameras/{id}/restart` reopens a source (replays a finished file,
  retries a corrected address).

### 7.2 Reports

- Combined filters: date range (Jalali picker), name/national number, gender,
  role, age range, camera, zone; sort by any column; pagination.
- **Image search**: upload a photo → nearest events by perceptual fingerprint.
- **PDF export**: Persian RTL, an index page and one detail page per row with
  the stored frame and face crop; honours the report lock.

### 7.3 Audit & retention

- Every sensitive action writes an `audit` row with actor/target/detail/outcome.
- Retention: `retention_days` auto-purges events and artefacts (0 = keep
  forever); the purge itself is audited.
- Nothing that affects an access decision is ever silently dropped — if the
  audit write fails the operation still completes (fail-open on *logging*), but
  the access gate itself is strictly fail-closed.

### 7.4 Users & roles

| Role | Can |
|---|---|
| `admin` | everything: cameras, enrolment, settings, users, zones, backups |
| `observer` | view dashboard (raw video only), reports, people — no edits |

- Sessions are server-side; passwords are PBKDF2-SHA256 (210k iterations).
- Login throttle: 8 attempts / 300 s per account.
- The last remaining admin cannot be deleted or demoted.

### 7.5 Security posture

- No runtime internet: everything is on the LAN; the console is served by the
  same process.
- Restricted CORS, security headers, HttpOnly session cookie.
- Credentials never appear in listings (stream URLs are redacted).
- Biometrics are sealed at rest; the access log notes every unseal.

---

## 8. Configuration

Live-tunable in the console (تنظیمات) and persisted to `data/settings.json`;
changes apply without a restart. Key knobs:

| Setting | Default | Meaning |
|---|---|---|
| `match_threshold` | 0.36 | minimum top identity score |
| `ambiguity_margin` | 0.06 | refuse if runner-up is within this |
| `samples_required` | 5 | votes needed to commit a track |
| `min_face_width` | 52 px | quality gate for voting |
| `dedup_window_seconds` | 120 | suppress duplicate events per identity+camera |
| `track_ttl_seconds` | 6 | abandon a track after silence |
| `retention_days` | 0 | event retention (0 = forever) |
| `access_control` | off | enable relay actuation |
| `zone_mode` | off | require the person inside a zone |
| `relay_*` | — | host/port/channels/pulse width |

Environment overrides: `FACW_PORT`, `FACW_DATA_DIR`, `FACW_LOG_DIR`,
`FACW_URL` (tools), `FACW_OPEN_BROWSER` (`0` disables the auto-opened console).

### 8.1 Hardware acceleration (CUDA)

`processing_device` is `auto` by default and is resolved **once at start-up**:

| Setting | Behaviour |
|---|---|
| `auto` | uses CUDA if a device is genuinely available, otherwise CPU |
| `cpu` | always CPU (a GPU is ignored) |
| `gpu` | asks for CUDA; **falls back to CPU** if unavailable |

The service never refuses to start because a GPU is missing, and a build that
accepts the CUDA target but silently falls back per-layer cannot break anything.

The decision, and the evidence behind it, are visible at `GET /api/health/full`
under `device`:

```json
{
  "device": "cpu", "requested": "auto",
  "cuda_devices": 0, "opencv_cuda": false, "ort_cuda": false,
  "ort_providers": ["CPUExecutionProvider"],
  "reason": "auto: no CUDA available, using CPU"
}
```

**What actually benefits:** the person detector is a plain `cv2.dnn` graph, so it
is the stage that takes the CUDA target. `cv2.FaceDetectorYN` and
`cv2.FaceRecognizerSF` expose no backend switch, so detection and embedding run
on the CPU regardless; `device` is reported for diagnostics.

**Enabling CUDA** requires an OpenCV build compiled with CUDA (the standard
`opencv-python` wheels are not):

```bash
pip install onnxruntime-gpu          # provides CUDAExecutionProvider
export OPENCV_CUDA=1                 # or rebuild opencv-contrib with -DWITH_CUDA=ON
```

Verify with `python -c "import cv2; print(cv2.cuda.getCudaEnabledDeviceCount())"`
and then check `GET /api/health/full` reports `"device": "cuda"`.

---

## 9. Fully offline deployment

At build time (with internet):

```bash
pip download -r requirements.txt -d wheelhouse/ --platform win_amd64 \
    --python-version 312 --only-binary=:all:
python scripts/fetch_models.py
```

Copy the project (including `wheelhouse/` and `models/`) to the target host.
On the target:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install --no-index --find-links wheelhouse -r requirements.txt
python -m facw.tools.service_control start
```

No further internet access is needed.

---

## 10. Tests

```bash
python -m pytest tests/ -q
```

- `tests/test_algorithms.py` — 27 unit tests: top-K matching, threshold/
  ambiguity, atomic rebuild, vote resolution, TTL sweep, zones, access-control
  policy, relay channel isolation.
- `tests/browser_smoke.py` — optional end-to-end console walk in headless
  Chromium (needs the service running and Playwright): signs in, visits every
  screen, exercises enrolment face picking, checks dark theme and no horizontal
  overflow, and fails on any console/page/HTTP error.
- `tests/fixtures/` — reference photos used by the seed and the tests.

---

## 11. Layout

```
facw/
  facw/
    core/        config, logging, security, app container
    storage/     db (schema), repositories, media store
    vision/      face (YuNet/SFace), person, fingerprint
    pipeline/    matching, tracking, zones, capture, discovery,
                 relay, notifications, engine, enrolment
    web/         auth, video, api_routes, api_more, reports, server
    tools/       service_control, enrolment_helper, zone_editor, autostart
  web/console/   SPA (vanilla JS, RTL, Jalali)
  models/        yunet.onnx, sface.onnx, persondet.onnx
  data/          facw.db, media/, backups/, settings.json, .sealkey
  logs/          boot.log, facw.log (JSON lines)
  scripts/       devrestart.sh, make_demo_clip.py, seed_demo.py
  tests/         unit tests + browser smoke test + fixtures
```

---

## 12. Licence / notes

- Model files are from the OpenCV Model Zoo (Apache-2.0) and Google MediaPipe
  (Apache-2.0).
- The demo clip is synthetic; the bundled fixture photos are public-domain
  test images (Lena, Messi, Largest Selfie).
- FACW is designed as a **security-critical** system: keep the console on a
  private VLAN, rotate the default passwords, and treat the audit log as the
  source of truth.
