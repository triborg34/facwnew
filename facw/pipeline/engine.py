"""The recognition pipeline orchestrator: capture -> zones -> detect -> track
-> face -> gate -> embed -> match -> vote -> commit -> event -> relay.

Isolation design (spec 9.2 / 9.1):

* ``PipelineWorker`` - one thread per camera that pulls the newest frame,
  runs person detection + tracking + face work, and produces annotated frames.
  A crash inside the worker is caught and the worker restarts; it never takes
  the service down.
* ``EventWriter`` - one thread draining a **bounded** event queue.  When the
  queue is full an event is dropped with a visible warning instead of stalling
  the video path (spec 4.11).
* The heavy *detector* itself runs in a separate child process when
  ``FACW_ISOLATE_ANALYSIS=1``; otherwise in-process.  Either way, the interface
  thread never runs analysis.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from ..core.logging_setup import get_logger
from ..storage.repos_more import EventRepo
from ..vision.face import FaceEngine, FaceBox, best_face, estimate_attributes, pad_box, quality_gate
from ..vision.person import Detection, PersonDetector, nms
from .capture import CameraStream, StreamRegistry, redact
from .matching import MatchIndex, MatchResult
from .relay import RelayController
from .tracking import Track, TrackManager, Vote, resolve_votes
from .zones import Zone, apply_zone_mask, attribute_zone

log = get_logger("pipeline")

FONT = cv2.FONT_HERSHEY_SIMPLEX
COLOURS = {
    "identified": (60, 200, 90),
    "unknown": (60, 90, 230),
    "analysing": (40, 190, 235),
    "person": (200, 200, 200),
    "face": (240, 180, 60),
}


@dataclass
class PipelineMetrics:
    frames: int = 0
    analysed: int = 0
    events: int = 0
    dropped_events: int = 0
    errors: int = 0
    last_event_at: float = 0.0
    latency_ms: float = 0.0
    queue_depth: int = 0
    per_camera: Dict[str, Dict[str, float]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# IoU tracker (stage 3) - stable ids via greedy IoU association
# --------------------------------------------------------------------------
def iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _inside(inner: np.ndarray, outer: Detection) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer``."""
    x1, y1 = max(inner[0], outer.x1), max(inner[1], outer.y1)
    x2, y2 = min(inner[2], outer.x2), min(inner[3], outer.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = max(1e-6, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / area


class AppearanceTracker:
    """Assigns stable track ids by IoU association against live tracks.

    Detectors often emit a complete body box *plus* a tighter head/torso
    fragment for the same person.  Only bodies become tracks: a detection that
    is largely contained in a better one is dropped here, not only in NMS,
    because the tracker is where a duplicate would become a permanent extra
    track that consumes sampling budget forever.
    """

    CONTAINMENT = 0.8

    def __init__(self, tracks: TrackManager) -> None:
        self.tracks = tracks

    @classmethod
    def suppress_fragments(cls, detections: List[Detection]) -> List[Detection]:
        ordered = sorted(detections, key=lambda d: d.score, reverse=True)
        kept: List[Detection] = []
        for det in ordered:
            box = np.array([det.x1, det.y1, det.x2, det.y2], dtype=np.float32)
            duplicate = False
            for k in kept:
                other = np.array([k.x1, k.y1, k.x2, k.y2], dtype=np.float32)
                if iou(other, box) > 0.3 or _inside(box, k) > cls.CONTAINMENT:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(det)
        return kept

    def update(self, camera_key: str, detections: List[Detection], lo: float) -> List[Tuple[Track, Detection]]:
        detections = self.suppress_fragments(detections)
        existing = [t for t in self.tracks.for_camera(camera_key)]
        matched: List[Tuple[Track, Detection]] = []
        used: set = set()
        for det in sorted(detections, key=lambda d: d.score, reverse=True):
            box = np.array([det.x1, det.y1, det.x2, det.y2], dtype=np.float32)
            best_t, best_iou = None, lo
            for t in existing:
                if id(t) in used:
                    continue
                v = iou(np.array(t.bbox, dtype=np.float32), box)
                if v > best_iou:
                    best_t, best_iou = t, v
            if best_t is not None:
                used.add(id(best_t))
                best_t.bbox = tuple(box.tolist())
                best_t.score = det.score
                best_t.last_seen = time.time()
                best_t.updated_at = best_t.last_seen
                matched.append((best_t, det))
            else:
                tr = Track(self.tracks.next_id(), camera_key, tuple(box.tolist()), det.score)
                self.tracks.put(tr)
                used.add(id(tr))
                matched.append((tr, det))
        return matched


# --------------------------------------------------------------------------
# Overlay drawing (stage 14)
# --------------------------------------------------------------------------
def draw_overlay(
    frame: np.ndarray,
    tracks: List[Track],
    zones: List[Zone],
    fps: float,
    show_zones: bool = True,
) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]

    if show_zones and zones:
        for z in zones:
            pts = np.asarray(z.to_pixels(w, h), dtype=np.int32)
            try:
                colour = tuple(int(z.colour.lstrip("#")[i : i + 2], 16) for i in (4, 2, 0))
            except (ValueError, IndexError):
                colour = (200, 200, 200)
            if z.kind == "line" and len(pts) >= 2:
                cv2.polylines(out, [pts], False, colour, 2)
            elif len(pts) >= 3:
                cv2.polylines(out, [pts], True, colour, 2)
            if len(pts) >= 1:
                cv2.putText(out, f"{z.name}", (int(pts[0][0]) + 4, int(pts[0][1]) + 18),
                            FONT, 0.5, colour, 1, cv2.LINE_AA)

    for t in tracks:
        x1, y1, x2, y2 = [int(v) for v in t.bbox]
        state = t.state
        colour = COLOURS.get(state, COLOURS["person"])
        label = "analysing"
        if state == "identified":
            nm = (t.identity or {}).get("name", "?")
            sc = float((t.identity or {}).get("score", 0.0))
            label = f"{nm} {sc:.2f}"
        elif state == "unknown":
            label = "unknown"
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(out, f"#{t.track_id} {label}", (x1, max(14, y1 - 8)), FONT, 0.55, colour, 2, cv2.LINE_AA)
        fb = t.face_box
        if isinstance(fb, FaceBox):
            cv2.rectangle(
                out,
                (int(fb.x), int(fb.y)),
                (int(fb.x + fb.w), int(fb.y + fb.h)),
                COLOURS["face"],
                1,
            )

    # frame-rate readout (top-right)
    txt = f"{fps:5.1f} fps"
    (tw, th), _ = cv2.getTextSize(txt, FONT, 0.6, 2)
    cv2.rectangle(out, (w - tw - 16, 8), (w - 6, 8 + th + 12), (0, 0, 0), -1)
    cv2.putText(out, txt, (w - tw - 11, 8 + th + 6), FONT, 0.6, (120, 240, 160), 2, cv2.LINE_AA)
    return out


# --------------------------------------------------------------------------
# Event writer (stages 10-11)
# --------------------------------------------------------------------------
class EventWriter:
    """Bounded queue + single writer thread.  Never blocks the video path."""

    def __init__(self, repo: EventRepo, media, settings_mgr, bus, maxsize: int = 64) -> None:
        self.repo = repo
        self.media = media
        self.settings_mgr = settings_mgr
        self.bus = bus
        self.q: "queue.Queue[Dict]" = queue.Queue(maxsize=maxsize)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.metrics = {"written": 0, "dropped": 0, "deduped": 0, "errors": 0}
        self._last_by_scope: Dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="event-writer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def submit(self, item: Dict) -> bool:
        """Enqueue; returns False when the queue is full (event dropped)."""
        try:
            self.q.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self.metrics["dropped"] += 1
            log.warning(
                "event dropped - write queue full",
                extra={"identity": item.get("identity_name"), "camera": item.get("camera_source")},
            )
            return False

    @property
    def depth(self) -> int:
        return self.q.qsize()

    def _dedup_key(self, item: Dict) -> str:
        # Scope is explicit and documented: identity + camera.  The same person
        # on two cameras produces two events; the same person standing still on
        # one camera produces one (spec 4.12 / pitfall 11).
        return f"{item.get('identity_name')}::{item.get('camera_id')}"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._write(item)
            except Exception as exc:  # noqa: BLE001 - a bad event must not kill the writer
                with self._lock:
                    self.metrics["errors"] += 1
                log.error("event write failed", extra={"error": str(exc)})

    def _write(self, item: Dict) -> None:
        s = self.settings_mgr.get()
        now = time.time()
        key = self._dedup_key(item)
        with self._lock:
            last = self._last_by_scope.get(key)
        if item.get("identity_name") != "unknown" and last and (now - last) < s.dedup_window_seconds:
            with self._lock:
                self.metrics["deduped"] += 1
            return
        files = self.media.save_event_artefacts(
            item["frame"], item["face_crop"], item["person_crop"], s.snapshot_quality
        )
        event_id = self.repo.create(
            {
                "identity_name": item.get("identity_name", "unknown"),
                "identity_id": item.get("identity_id"),
                "score": item.get("score", 0.0),
                "age": item.get("age"),
                "gender": item.get("gender", "unknown"),
                "role": item.get("role", "unknown"),
                "national_number": item.get("national_number", ""),
                "camera_source": item.get("camera_source", ""),
                "camera_id": item.get("camera_id"),
                "zone_name": item.get("zone_name", ""),
                "track_id": item.get("track_id", 0),
                "frame_file": files["frame"],
                "face_file": files["face"],
                "person_file": files["person"],
                "created_ts": now,
            }
        )
        with self._lock:
            self._last_by_scope[key] = now
            self.metrics["written"] += 1
            if len(self._last_by_scope) > 4096:  # keep memory flat
                cutoff = now - max(60.0, s.dedup_window_seconds * 4)
                self._last_by_scope = {k: v for k, v in self._last_by_scope.items() if v > cutoff}
        log.info(
            "event stored",
            extra={"id": event_id, "identity": item.get("identity_name"),
                   "camera": item.get("camera_source"), "track": item.get("track_id")},
        )
        self.bus.publish(
            "events",
            {
                "action": "created",
                "id": event_id,
                "identity_name": item.get("identity_name"),
                "camera_source": item.get("camera_source"),
                "score": round(float(item.get("score", 0.0)), 3),
                "track_id": item.get("track_id"),
            },
        )


# --------------------------------------------------------------------------
# Per-camera worker
# --------------------------------------------------------------------------
class PipelineWorker:
    """Analyses one camera's frames on its own thread."""

    def __init__(self, ctx: "PipelineContext", camera: Dict, stream: CameraStream) -> None:
        self.ctx = ctx
        self.camera = camera
        self.camera_id = int(camera["id"])
        self.camera_key = str(camera.get("address") or camera.get("id"))
        self.stream = stream
        self.tracker = AppearanceTracker(ctx.tracks)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_seq = -1
        self.annotations: "LatestAnnotated" = LatestAnnotated()
        self.zones: List[Zone] = []
        self._zones_loaded_at = 0.0
        self._fps_marks: List[float] = []
        self._last_detect_at = 0.0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"pipe-{self.camera_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- zones ---------------------------------------------------------
    def reload_zones(self) -> None:
        try:
            rows = self.ctx.zone_repo.list_for_camera(self.camera_key)
            self.zones = [Zone.from_dict(r) for r in rows]
            self._zones_loaded_at = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("zone load failed", extra={"camera": self.camera_key, "error": str(exc)})

    # -- main loop -----------------------------------------------------
    def _run(self) -> None:
        self.reload_zones()
        while not self._stop.is_set():
            try:
                got = self.stream.frames.get(self._last_seq, timeout=0.5)
                if got is None:
                    continue
                frame, seq, _ts = got
                self._last_seq = seq
                if time.time() - self._zones_loaded_at > 5.0:
                    self.reload_zones()
                settings = self.ctx.settings.get()
                t0 = time.perf_counter()
                annotated = self.process_frame(frame, settings)
                latency = (time.perf_counter() - t0) * 1000.0
                self.annotations.put(annotated, latency)
                self.ctx.record_frame(self.camera_id, latency, self.stream.status.fps)
            except Exception as exc:  # noqa: BLE001 - one camera must not kill the service
                self.ctx.metrics.errors += 1
                log.error("pipeline worker error", extra={"camera": self.camera_id, "error": str(exc)})
                self._stop.wait(0.25)

    def _sample_due(self, track: Track, settings) -> bool:
        """Stage 16: fast cadence while undecided, slow after a decision.

        A committed track is *not* re-embedded at all (it is resolved for this
        appearance); the slow cadence only drives its face-box refresh.
        """
        now = time.time()
        cadence = settings.slow_sample_ms if track.committed else settings.fast_sample_ms
        if (now - track.last_sample_at) * 1000.0 >= cadence:
            track.last_sample_at = now
            return True
        return False

    def _tracks_due(self, settings) -> bool:
        """Cheap throttle: run detection+tracking at the fast cadence.

        Without this the person detector ran on every captured frame (25-30 fps
        per camera), which is what made the live view lag behind by seconds and
        starved the box positions.  The newest frame is still analysed as soon
        as it arrives at the configured cadence, so boxes stay fresh while the
        CPU/GPU has headroom for more cameras.
        """
        now = time.time()
        cadence = max(30, int(settings.fast_sample_ms)) / 1000.0
        if (now - self._last_detect_at) < cadence:
            return False
        self._last_detect_at = now
        return True

    def process_frame(self, frame: np.ndarray, settings) -> np.ndarray:
        h, w = frame.shape[:2]
        work = frame
        # Stage 2 - zone masking.  No zones -> whole frame (never a hard-coded
        # rectangle).
        if settings.zone_mode and self.zones:
            work = apply_zone_mask(frame, self.zones)

        detections = self.ctx.person.detect(work, settings.person_confidence, getattr(settings, "person_max_side", 0))
        # The detector family emits several same-size, diagonally offset slabs
        # around one person; IoU and containment both stay below their gates.
        # Suppress across the *accepted* set with a looser threshold so one
        # body yields one detection (and therefore one track).
        detections = nms(detections, 0.1)
        detection_update = self._tracks_due(settings)
        if not detection_update:
            # Between association passes the existing tracks keep their ids and
            # only their face boxes are refreshed, so annotate cheaply.
            if not self.ctx.tracks.for_camera(self.camera_key):
                detections = []
            else:
                detections = None
        if settings.zone_mode and self.zones:
            # Attribute to the zone containing the box centre; also drop boxes
            # that the block mask left empty (nothing to detect outside zones).
            kept = []
            for d in detections or []:
                cx, cy = (d.x1 + d.x2) / 2.0 / max(1, w), (d.y1 + d.y2) / 2.0 / max(1, h)
                z = attribute_zone(self.zones, cx, cy)
                if z is not None:
                    kept.append((d, z))
            pairs = self.tracker.update(self.camera_key, [d for d, _ in kept], settings.overlap_threshold) if detection_update else []
            zone_by_id = {id(d): z for d, z in kept}
        else:
            pairs = self.tracker.update(self.camera_key, detections, settings.overlap_threshold) if detection_update else []
            zone_by_id = {}

        for track, det in pairs:
            if settings.zone_mode and self.zones:
                z = zone_by_id.get(id(det))
                if z is not None:
                    track.zone_name = z.name
            if track.committed:
                # Stage 4: a committed track only refreshes its face box, and
                # only at the slow cadence (it is already resolved).
                if self._sample_due(track, settings):
                    self._refresh_face_box(track, frame, settings)
                continue
            if not self._sample_due(track, settings):
                continue
            self._analyse_track(track, frame, settings)

        annotated = draw_overlay(
            frame,
            self.ctx.tracks.for_camera(self.camera_key),
            self.zones,
            self.stream.status.fps,
            show_zones=True,
        )
        return annotated

    def _refresh_face_box(self, track: Track, frame: np.ndarray, settings) -> None:
        x1, y1, x2, y2 = [int(v) for v in track.bbox]
        crop = frame[max(0, y1) : max(0, y2), max(0, x1) : max(0, x2)]
        if crop.size == 0:
            return
        faces = self.ctx.face.detect(crop, settings.face_accept_threshold)
        bf = best_face(faces)
        if bf is not None:
            track.face_box = FaceBox(bf.x + x1, bf.y + y1, bf.w, bf.h, bf.score, None)

    def _analyse_track(self, track: Track, frame: np.ndarray, settings) -> None:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = [int(round(v)) for v in track.bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return
        person_crop = frame[y1:y2, x1:x2]
        track.person_crop = person_crop
        track.sample_count += 1

        # Stage 4 - faces inside the person crop
        faces = self.ctx.face.detect(person_crop, settings.face_accept_threshold)
        body = best_face(faces)
        if body is None:
            # no face yet -> keep analysing, keep sampling
            return
        track.face_box = FaceBox(body.x + x1, body.y + y1, body.w, body.h, body.score, None)

        # Stage 5 - quality gate.  A small/blurry face never votes; the track
        # stays "analysing" until the face is good enough (never "unknown").
        if not quality_gate(body, settings.min_face_width, settings.min_face_score):
            return

        # Stage 6 - identity vector
        vector = self.ctx.face.embed(person_crop, body)
        if vector is None:
            return
        track.vector = vector
        age, gender = estimate_attributes(body, max(1.0, x2 - x1), max(1.0, y2 - y1))

        # Stage 7 - match
        result: MatchResult = self.ctx.index.match(vector, settings.match_threshold, settings.ambiguity_margin)
        track.votes.append(
            Vote(
                identity=result.name,
                score=result.score,
                age=age if result.name != "unknown" else age,
                gender=result.gender if result.name != "unknown" else gender,
                role=result.role,
                national_number=result.national_number,
                identity_id=result.identity_id,
                description=result.description,
            )
        )

        # Stage 8 - vote resolution
        decision = resolve_votes(track.votes, settings.samples_required)
        if decision is None:
            return
        track.committed = True
        track.identity = decision
        track.votes = []  # drop the vote list once committed
        track.emitted = False
        track.updated_at = time.time()

        # Stage 10 - emit exactly one event for this appearance
        self._emit_event(track, frame, person_crop, body, settings, x1, y1)

    def _emit_event(self, track: Track, frame, person_crop, face: FaceBox, settings, ox, oy) -> None:
        if track.emitted:
            return
        track.emitted = True
        pad = int(settings.face_crop_padding)
        fx1, fy1, fx2, fy2 = pad_box(face.x, face.y, face.w, face.h, pad, person_crop.shape[1], person_crop.shape[0])
        face_crop = person_crop[fy1:fy2, fx1:fx2]
        if face_crop.size == 0:
            face_crop = person_crop
        ident = track.identity or {}
        submitted = self.ctx.writer.submit(
            {
                "identity_name": ident.get("name", "unknown"),
                "identity_id": ident.get("identity_id"),
                "score": ident.get("score", 0.0),
                "age": ident.get("age"),
                "gender": ident.get("gender", "unknown"),
                "role": ident.get("role", "unknown"),
                "national_number": ident.get("national_number", ""),
                "camera_source": self.camera.get("name", str(self.camera_id)),
                "camera_id": self.camera_id,
                "zone_name": track.zone_name,
                "track_id": track.track_id,
                "frame": frame.copy(),
                "face_crop": face_crop.copy(),
                "person_crop": person_crop.copy(),
            }
        )
        if not submitted:
            return
        self.ctx.metrics.events += 1
        self.ctx.metrics.last_event_at = time.time()

        # Stage 12 - access control
        if ident.get("name") != "unknown" and str(ident.get("role")) == "approve":
            zone_channel = ""
            if settings.zone_mode and track.zone_name:
                for z in self.zones:
                    if z.name == track.zone_name:
                        zone_channel = z.relay_channel
                        break
            results = self.ctx.relay.actuate(
                settings,
                zone_channel=zone_channel or None,
                reason=f"identity={ident.get('name')} camera={self.camera_id}",
            )
            if results and self.ctx.audit:
                self.ctx.audit.record(
                    "relay_actuation",
                    actor="pipeline",
                    target=str(ident.get("name")),
                    detail=str(results),
                    outcome="ok" if all(r.get("ok") for r in results) else "partial",
                )

        # Stage 13 - operator notification (never breaks recognition)
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note = self.ctx.notifier.send_detection(
            settings,
            identity=str(ident.get("name", "unknown")),
            when_text=now_text,
            camera=str(self.camera.get("name", "")),
            score=float(ident.get("score", 0.0)),
        )
        if note is not None:
            self.ctx.bus.publish(
                "alarm",
                {
                    "identity": ident.get("name", "unknown"),
                    "identity_id": ident.get("identity_id"),
                    "at": now_text,
                    "camera": self.camera.get("name", ""),
                    "score": round(float(ident.get("score", 0.0)), 3),
                    "notified": note.sent,
                    "notify_error": note.error,
                },
            )


class LatestAnnotated:
    """Single-slot buffer of annotated frames + latency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._latency = 0.0

    def put(self, frame: np.ndarray, latency_ms: float = 0.0) -> None:
        with self._lock:
            self._frame = frame
            self._seq += 1
            self._latency = latency_ms

    def get(self, since_seq: int = -1, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, int]]:
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self._seq != since_seq and self._frame is not None:
                    return self._frame, self._seq
            if time.time() >= deadline:
                return None
            time.sleep(0.01)

    @property
    def latency(self) -> float:
        return self._latency


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
@dataclass
class PipelineContext:
    settings: Any
    media: Any
    person: PersonDetector
    face: FaceEngine
    index: MatchIndex
    tracks: TrackManager
    writer: EventWriter
    relay: RelayController
    notifier: Any
    bus: Any
    zone_repo: Any
    audit: Any
    metrics: PipelineMetrics = field(default_factory=PipelineMetrics)

    def record_frame(self, camera_id: int, latency_ms: float, source_fps: float = 0.0) -> None:
        self.metrics.frames += 1
        self.metrics.analysed += 1
        self.metrics.latency_ms = round(latency_ms, 2)
        cam = self.metrics.per_camera.setdefault(
            str(camera_id), {"frames": 0, "latency_ms": 0.0, "fps": 0.0, "source_fps": 0.0}
        )
        cam["frames"] += 1
        cam["latency_ms"] = round(latency_ms, 2)
        cam["source_fps"] = round(float(source_fps or 0.0), 1)
        cam["fps"] = round(1000.0 / latency_ms, 1) if latency_ms > 0 else 0.0


class Pipeline:
    """Owns the stream registry, one worker per camera and the sweep thread."""

    def __init__(self, ctx: PipelineContext, streams: StreamRegistry, worker_count: int = 0) -> None:
        self.ctx = ctx
        self.streams = streams
        self.workers: Dict[int, PipelineWorker] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None

    def sync(self, cameras: List[Dict]) -> None:
        """Start/stop workers to match the registered camera set."""
        active = {int(c["id"]): c for c in cameras if c.get("analysis_enabled", True)}
        self.streams.sync(cameras)
        with self._lock:
            for cid in list(self.workers):
                if cid not in active:
                    self.workers.pop(cid).stop()
                    self.ctx.tracks.clear_camera(str(active.get(cid, {}).get("address", cid)))
            for cid, cam in active.items():
                stream = self.streams.get(cid)
                if stream is None:
                    continue
                w = self.workers.get(cid)
                if w is None or not w.alive or w.stream is not stream:
                    if w:
                        w.stop()
                    w = PipelineWorker(self.ctx, cam, stream)
                    self.workers[cid] = w
                    stream.acquire(owner="pipeline")
                    w.start()
                else:
                    w.camera = cam
                    w.camera_key = str(cam.get("address") or cid)
        # ensure the writer is running
        self.ctx.writer.start()

    def annotated(self, camera_id: int):
        w = self.workers.get(int(camera_id))
        return w.annotations if w else None

    def zones_for(self, camera_id: int) -> List[Zone]:
        w = self.workers.get(int(camera_id))
        return w.zones if w else []

    def reload_zones(self) -> None:
        for w in self.workers.values():
            w.reload_zones()

    def start_sweeper(self) -> None:
        if self._sweeper:
            return

        def loop() -> None:
            while not self._stop.wait(0.5):
                try:
                    self.ctx.tracks.ttl = self.ctx.settings.get().track_ttl_seconds
                    dropped = self.ctx.tracks.sweep()
                    if dropped:
                        log.debug("tracks swept", extra={"count": len(dropped)})
                except Exception as exc:  # noqa: BLE001
                    log.warning("sweeper error", extra={"error": str(exc)})

        self._sweeper = threading.Thread(target=loop, name="track-sweeper", daemon=True)
        self._sweeper.start()

    def metrics(self) -> Dict[str, Any]:
        m = self.ctx.metrics
        queues = {
            "event_write": self.ctx.writer.depth,
            "tracks": self.ctx.tracks.count,
        }
        with self._lock:
            workers = len(self.workers)
        return {
            "frames": m.frames,
            "analysed": m.analysed,
            "events": m.events,
            "dropped_events": m.dropped_events,
            "errors": m.errors,
            "latency_ms": m.latency_ms,
            "last_event_at": m.last_event_at,
            "queue_depth": queues["event_write"],
            "queues": queues,
            "workers": workers,
            "per_camera": m.per_camera,
        }

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for w in workers:
            w.stop()
            try:
                w.stream.release(owner="pipeline")
            except Exception:
                pass
        self.ctx.writer.stop()
        self.streams.stop_all()
