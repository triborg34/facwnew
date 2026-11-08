# FACW — work log and backlog

_Last updated: 2026-11-08. Author: Particle-1 (AI assistant)._

This file records what was changed, why, and what is still open. It is meant to
be read before starting new work so the same bugs are not fixed twice.

---

## 1. Where the project stands

FACW is a face-recognition access-control system:

```
facw/
  core/       config, app wiring, device (CUDA/ORT) probing, security
  pipeline/   capture, engine (stages), tracking, matching, enrolment, onvif,
              discovery, relay, notifications, zones
  vision/     face (yunet + sface embeddings), person detector, fingerprint
  storage/    db, repositories, media
  web/        FastAPI: api_routes, api_more, video (MJPEG + snapshot), auth,
              forms (multipart limits), reports
web/console/  plain ES-module console (no build step):
              app.js, dashboard.js, people.js, people_enrol.js, reports.js,
              cameras.js, settings.js, ui.js, api.js, store.js, imgw.js
```

Key runtime facts:

- The match index (`pipeline/matching.py`) is **rebuilt on every enrolment**
  and the pipeline reads `ctx.index` live, so **enrolling never needs a restart**.
- Live video is **one `multipart/x-mixed-replace` stream per tile**
  (`GET /video/{id}`), paced to 15 fps.
- Detection list polls `/api/live/detections` at ~4 Hz and patches rows in place.
- `data/` is **not tracked** in git (database, face photos, media).

---

## 2. What was fixed (chronological)

| Commit | What it fixed |
|---|---|
| `e41417f` | GPU/CUDA path + ONVIF discovery + camera-still enrolment + multipart 1 MB limit |
| `63bc3b7` | Black flash between frames — double-buffered `<img>` |
| `281037f` | Split `refreshMosaic()` from `refreshDetections()`; `Cache-Control: no-cache` on console assets (stale JS was hiding fixes) |
| `2b30779` | Detection list blinking: empty placeholder was removed+re-added on every poll |
| `27546b8` | Stopped tracking 401 files in `data/` (DB + 398 face photos were public on GitHub) |
| `d080afd` | Live tiles use **one MJPEG stream** instead of snapshot swapping; added the missing 15 fps throttle in `video.py` |
| `fce3ea6` | "face index N out of range" — `chosen[]` was built in display order but the server rebuilds images as *files then stills* |
| `8fcdf5c` | Same error, real cause: faces were detected on the **full-size** frame but saved from a **1280px** copy (7 faces vs 2). Also the picker offered 9–19px false positives |
| `29a96cd` | Dashboard stuck on "connecting": `unmount()` didn't clear `S.mosaicSig` |
| `354836e` | Enrolment dialog split into tabs (file / camera / handed-over still); register action added to the report screen; fixed `loadGrid()` null crash |
| `b21fde1` | New people appear immediately (people page now listens to `facw:change`); report detail shows face + frame |

### Root causes worth remembering

1. **Snapshot swapping was the black flash.** Setting `.src` makes the browser
   drop the current bitmap. Only a streaming `<img>` avoids it entirely.
2. **Stale console JS hides fixes.** `/js/*` had an ETag but no `Cache-Control`,
   so a fixed `dashboard.js` never reached the browser. Always Ctrl+F5 after a
   restart, and check `Cache-Control` if a fix "doesn't work".
3. **Old server processes hold port 8770.** `pkill` is unreliable here; use
   `netstat -ano | grep :8770` then `taskkill //PID <pid> //F`. This caused a
   false "already fixed" report once.
4. **Detect on the image you will save.** Any downscale between detection and
   save invalidates face indices.
5. **`window.store` is not a global** — it is module-scoped. Browser tests must
   not read it; use the DOM or the API instead.

---

## 3. Verification

Run these before declaring work done:

```bash
python -m pytest tests/test_algorithms.py -q     # 33 unit tests
python tests/acceptance.py                       # 24 end-to-end (needs a running server)
python tests/browser_smoke.py                    # 0 JS errors across all pages
python scripts/browser_flows.py                  # 17 UI checks (Playwright)
python scripts/smoke_boot.py                     # boot + enrolment round trip
```

Notes:
- `browser_flows.py` needs a **live camera** for the tile checks. A demo clip
  works: `python scripts/make_demo_clip.py --seconds 120 --out data/temp/livecam.avi`
  then register it as a `file` camera. Without one, tile checks fail for
  environmental reasons, not code reasons.
- The face in the demo clip is ~44–50px, below the default `min_face_width=52`,
  so it produces **no detections**. Lower the gate temporarily if you need
  detection rows, and restore it afterwards (52 / 0.62).

---

## 4. TODO — open items

### High priority

- [ ] **Make the repository private.** `data/facw.db` and 398 face photos are
      still in **past git history** on a public repo. `27546b8` stopped tracking
      them going forward but did not remove the history. This is the only
      pending item with a real privacy consequence.
- [ ] **Reproduce the dashboard "select a face → error" report.** It could not
      be reproduced on this machine (always returned 200). `fce3ea6` and
      `8fcdf5c` remove the two known mechanisms, but if it recurs the new error
      text distinguishes index mismatch from quality gate.
- [ ] **Test on a real ONVIF camera.** Discovery/WS-Security/stream URI were
      only exercised against mocks and dead endpoints. Run
      **دوربینها → کشف دوربین در شبکه** with real credentials and report the
      profile list.
- [ ] **Test with a crowd scene.** The 4K-frame mismatch was reproduced by
      upscaling; a real crowded 4K camera should confirm the fix in situ.

### Medium priority

- [ ] **Objects detected as faces.** The live pipeline only looks for faces
      inside person boxes, but the enrolment picker scans the whole image.
      `_pick_candidates` now applies the quality gate, which removes most false
      positives. A stronger fix is to wire a person detector into
      `pipeline/enrolment.py` and only consider faces inside person boxes.
- [ ] **Verify CUDA actually accelerates.** No GPU on the dev machine; the
      accelerator card reports an honest CPU fallback. Needs a CUDA host.
- [ ] **Several cameras at once.** MJPEG uses one connection per tile; the
      browser allows ~6 per host. Four is fine, more needs checking.
- [ ] **`data/autostart_test.db`** was untracked with the rest; confirm nothing
      depends on it existing.

### Low priority / polish

- [ ] Screenshots in `docs/screens/` are stale after the UI changes; regenerate.
- [ ] `logs/facw.log` is tracked and modified constantly; consider untracking.
- [ ] `__pycache__` is still tracked in the index (ignored for new files only);
      `git rm -r --cached` would clean it up.
- [ ] Enrolment dialog: the "قاب ارسالی" tab shows nothing when opened directly
      from the people page. Consider hiding the tab when it is empty.
- [ ] Report export PDF was tested at 5–6 MB; check size with a large date range.

---

## 5. Environment notes

- Windows + Git Bash. `/tmp` maps to WSL and is unusable — write files inside
  the project instead.
- Git Bash mangles `:` in git pathspecs (`git show rev:path`). Use
  `git show <rev> -- <path>`.
- Playwright 1.64.1 + chromium are installed. Persian text is escaped in the
  DOM, so prefer class/position selectors over text selectors.
- A pre-login `401` on `/api/auth/me` is **by design** (`api.js` `allow401`).
- `httpx` TestClient sends urlencoded bodies unless files are supplied; use
  `files=[(k, (None, v))]` to force multipart in tests.
