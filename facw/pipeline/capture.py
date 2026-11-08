"""Capture layer (stage 1 / spec 3.3).

Design:

* One ``CameraStream`` per camera.  It owns a capture thread that keeps only
  the **newest** frame in a single-slot buffer (older frames are dropped, never
  queued), so a slow reader can never stall the camera and never sees a
  half-written frame.
* Viewers are reference-counted.  The stream starts its capture thread lazily
  and stops it when the last viewer leaves, releasing the device.
* On read failure: release, wait with exponential backoff, reopen.  After
  repeated failures the camera is reported offline - other cameras are
  unaffected.
* A pre-open TCP probe fails fast with a clear message when the host is down
  instead of hanging for the RTSP timeout (spec 3.4).
"""
from __future__ import annotations

import socket
from pathlib import Path
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

import cv2
import numpy as np

from ..core.logging_setup import get_logger

log = get_logger("capture")

# project root = .../facw (this file lives at facw/pipeline/capture.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------
# Stream address helpers
# --------------------------------------------------------------------------
def redact(url: str) -> str:
    """Never log camera credentials (spec 9.3)."""
    try:
        p = urlparse(url)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            return p._replace(netloc=netloc).geturl()
    except ValueError:
        pass
    return url


def build_stream_address(
    stream_type: str,
    address: str,
    port: int = 554,
    profile: str = "main",
    username: str = "",
    password: str = "",
) -> str:
    """Compose a stream URL for common camera types.

    ``rtsp``  -> rtsp://user:pass@host:port/<profile>
    ``http``  -> http://user:pass@host:port/<profile>
    ``webcam``-> integer device index (``address`` is used as the index)
    ``file``  -> the address is a local path, returned unchanged
    """
    st = (stream_type or "rtsp").lower()
    if st in ("rtsp", "http", "https"):
        cred = ""
        if username:
            cred = quote(str(username), safe="")
            if password:
                cred += ":" + quote(str(password), safe="")
            cred += "@"
        host = str(address).strip()
        # if an operator pastes a full URL, honour it
        if "://" in host:
            return host
        default_port = 554 if st == "rtsp" else 80
        port_part = f":{int(port)}" if int(port or 0) and int(port) != default_port else ""
        prof = (profile or "").lstrip("/")
        suffix = f"/{prof}" if prof else ""
        return f"{st}://{cred}{host}{port_part}{suffix}"
    if st == "webcam":
        return str(address).strip().replace("webcam", "").strip(":/ ") or "0"
    # file (and anything else): the address is used verbatim
    return str(address).strip()


def probe_host(host: str, port: int, timeout: float = 1.5) -> bool:
    """Fail fast when the camera host does not answer (spec 3.4)."""
    if not host:
        return True
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def open_capture(address: str, stream_type: str = "rtsp") -> Optional[cv2.VideoCapture]:
    """Open a capture with a small buffer so readers never block."""
    st = (stream_type or "rtsp").lower()
    try:
        if st == "webcam":
            cap = cv2.VideoCapture(int(address) if str(address).lstrip("-").isdigit() else 0)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            target = address
            if st == "file":
                # Resolve relative paths against the project root, not the process
                # CWD: a service started by a scheduler has an unrelated CWD and
                # would otherwise silently fail to open the same file.
                p = Path(target)
                if not p.is_absolute():
                    p = PROJECT_ROOT / p
                target = str(p)
            cap = cv2.VideoCapture(target, cv2.CAP_FFMPEG if hasattr(cv2, "CAP_FFMPEG") else 0)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            return None
        return cap
    except cv2.error as exc:
        log.warning("capture open failed", extra={"address": redact(address), "error": str(exc)})
        return None


# --------------------------------------------------------------------------
# Newest-frame buffer
# --------------------------------------------------------------------------
class LatestFrame:
    """Single-slot buffer: writers overwrite, readers take the newest."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._ts = 0.0

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame           # whole-array swap: never half written
            self._seq += 1
            self._ts = time.time()

    def get(self, since_seq: int = -1, timeout: float = 1.0) -> Optional[tuple]:
        """Return ``(frame, seq, ts)`` newer than ``since_seq`` or None."""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self._seq != since_seq and self._frame is not None:
                    return self._frame, self._seq, self._ts
            if time.time() >= deadline:
                return None
            time.sleep(0.008)


# --------------------------------------------------------------------------
# Camera stream worker
# --------------------------------------------------------------------------
@dataclass
class CameraStatus:
    camera_id: int = 0
    name: str = ""
    address: str = ""
    state: str = "idle"          # idle | connecting | streaming | reconnecting | offline
    fps: float = 0.0
    viewers: int = 0
    last_error: str = ""
    opened_at: float = 0.0
    frames: int = 0
    detail: str = ""
    #: analysis cadence actually used (set by the pipeline)
    analysed_fps: float = 0.0


class CameraStream:
    """One capture worker per camera with reference-counted viewers."""

    MAX_FAILURES = 8

    def __init__(self, camera: Dict, on_status=None) -> None:
        self.camera_id = int(camera.get("id") or 0)
        self.name = str(camera.get("name") or f"camera-{self.camera_id}")
        self.address = str(camera.get("stream_address") or "")
        self.raw_address = str(camera.get("address") or "")
        self.stream_type = str(camera.get("stream_type") or "rtsp")
        self.port = int(camera.get("port") or 554)
        self.frames = LatestFrame()
        self.status = CameraStatus(
            camera_id=self.camera_id,
            name=self.name,
            address=self.raw_address or self.address,
        )
        self._on_status = on_status
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # set while the analysis pipeline (not just a console viewer) needs frames
        self.pipeline_owned = False
        self._viewers = 0
        self._fps_marks: List[float] = []
        self._fails = 0
        # file-source pacing (see _run)
        self._file_fps = 0.0
        self._next_file_ts = 0.0

    # -- viewer reference counting -------------------------------------
    def acquire(self, owner: str = "viewer") -> None:
        """Register a consumer; the reader thread starts on the first one."""
        with self._lock:
            if owner == "pipeline":
                self.pipeline_owned = True
            self._viewers += 1
            self.status.viewers = self._viewers
            if self._thread is None or not self._thread.is_alive():
                self._thread = None
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, name=f"cap-{self.camera_id}", daemon=True
                )
                self._thread.start()

    def release(self, owner: str = "viewer") -> None:
        """Drop a consumer; the camera is released only when nobody needs it."""
        with self._lock:
            if owner == "pipeline":
                self.pipeline_owned = False
            self._viewers = max(0, self._viewers - 1)
            self.status.viewers = self._viewers
            if self._viewers == 0 and not self.pipeline_owned:
                # Nobody left: release the camera (spec 3.3).
                self._stop.set()
                self.status.state = "idle"

    @property
    def viewers(self) -> int:
        return self._viewers

    def snapshot(self) -> Optional[np.ndarray]:
        got = self.frames.get(-1, timeout=0.0)
        return got[0] if got else None

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self.status.state = "offline"

    def restart(self) -> None:
        """Stop and reopen the source.

        Used to replay a finished file source or to retry a camera whose address
        or credentials were just corrected.  The reader thread is created lazily
        by acquire(), so this only needs to stop it and clear the flags.
        """
        self.stop()          # joins the old thread, so it cannot re-set the flag
        self._fails = 0
        with self._lock:
            self._stop = threading.Event()   # a fresh event, never a cleared one
            if self._viewers > 0 or self.pipeline_owned:
                self._thread = threading.Thread(
                    target=self._run, name=f"cap-{self.camera_id}", daemon=True
                )
                self._thread.start()

    # -- worker --------------------------------------------------------
    def _set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.status, k, v)
        if self._on_status:
            try:
                self._on_status(self.status)
            except Exception:
                pass

    def _run(self) -> None:
        cap: Optional[cv2.VideoCapture] = None
        backoff = 0.5
        while not self._stop.is_set():
            if cap is None:
                self._set(state="connecting", detail="")
                if self.stream_type in ("rtsp", "http", "https") and self.raw_address:
                    parsed = urlparse(self.address)
                    host = parsed.hostname or self.raw_address
                    if not probe_host(host, self.port, timeout=1.5):
                        self._set(
                            state="offline",
                            last_error=f"camera host {host}:{self.port} not reachable",
                        )
                        log.warning(
                            "camera host unreachable",
                            extra={"camera": self.name, "host": host, "port": self.port},
                        )
                        if self._stop.wait(backoff):
                            break
                        backoff = min(backoff * 2, 15.0)
                        continue
                cap = open_capture(self.address, self.stream_type)
                if cap is not None and self.stream_type == "file":
                    native = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    # clamp absurd metadata; fall back to a sane 12 fps
                    self._file_fps = native if 1.0 <= native <= 60.0 else 12.0
                    self._next_file_ts = time.time()
                if cap is None:
                    self._fails += 1
                    self._set(state="offline", last_error="cannot open stream")
                    if self._fails >= self.MAX_FAILURES:
                        log.error("camera offline after repeated failures", extra={"camera": self.name})
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 15.0)
                    continue

            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                self._fails += 1
                with self._lock:
                    cap.release()
                    cap = None
                if self.stream_type == "file":
                    # A file source that runs out of frames is *finished*, not
                    # broken.  Retrying it spins the decode loop and floods the
                    # log; stop cleanly until the stream is restarted.
                    self._set(state="idle", fps=0.0, last_error="end of file")
                    log.info("file source finished", extra={"camera": self.name})
                    break
                # Log the first failure only; a persistent fault must not write a
                # line per loop iteration (that once starved the event loop).
                if self._fails == 1:
                    log.warning(
                        "camera read failed",
                        extra={"camera": self.name, "failures": self._fails, "address": redact(self.address)},
                    )
                self._set(state="reconnecting", last_error="read failed")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 15.0)
                continue

            # healthy read resets the failure counter and backoff
            self._fails = 0
            backoff = 0.5
            if self.stream_type == "file" and self._file_fps > 0:
                # Play a file at its native rate.  Without pacing a 12-second
                # clip is decoded in well under a second, so the pipeline sees
                # the whole scene between two analysis passes and nothing is
                # ever tracked or matched.
                target = self._next_file_ts
                now = time.time()
                if target > now:
                    if self._stop.wait(min(target - now, 0.5)):
                        break
                self._next_file_ts = max(target, now) + (1.0 / self._file_fps)
            self._push_fps()
            self.frames.put(frame)
            if self.status.state != "streaming":
                self._set(state="streaming", last_error="", opened_at=time.time())

        if cap is not None:
            with self._lock:
                cap.release()
        self._set(state="idle", fps=0.0)

    def _push_fps(self) -> None:
        now = time.time()
        self._fps_marks.append(now)
        self.status.frames += 1
        if len(self._fps_marks) > 60:
            del self._fps_marks[:-60]
        if len(self._fps_marks) >= 2:
            span = self._fps_marks[-1] - self._fps_marks[0]
            if span > 0.2:
                self.status.fps = round((len(self._fps_marks) - 1) / span, 1)


class StreamRegistry:
    """Owns the per-camera streams; creates/replaces/destroys as cameras change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: Dict[int, CameraStream] = {}
        self._status_listeners: List = []

    def on_status(self, cb) -> None:
        self._status_listeners.append(cb)

    def _notify(self, status: CameraStatus) -> None:
        for cb in self._status_listeners:
            try:
                cb(status)
            except Exception:
                pass

    def sync(self, cameras: List[Dict]) -> None:
        """Ensure one stream per registered camera with analysis enabled."""
        wanted = {int(c["id"]): c for c in cameras}
        with self._lock:
            for cid in list(self._streams):
                if cid not in wanted:
                    self._streams.pop(cid).stop()
            for cid, cam in wanted.items():
                existing = self._streams.get(cid)
                addr = str(cam.get("stream_address") or "")
                if existing and (existing.address != addr or existing.name != cam.get("name")):
                    existing.stop()
                    existing = None
                if existing is None:
                    self._streams[cid] = CameraStream(cam, on_status=self._notify)
                else:
                    existing.name = str(cam.get("name") or existing.name)

    def get(self, camera_id: int) -> Optional[CameraStream]:
        with self._lock:
            return self._streams.get(int(camera_id))

    def all_status(self) -> List[Dict]:
        with self._lock:
            streams = list(self._streams.values())
        out = []
        for s in streams:
            st = s.status
            out.append(
                {
                    "camera_id": st.camera_id,
                    "name": st.name,
                    "address": st.address,
                    "state": st.state,
                    "fps": st.fps,
                    "analysed_fps": st.analysed_fps,
                    "viewers": st.viewers,
                    "last_error": st.last_error,
                    "frames": st.frames,
                }
            )
        return out

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for s in streams:
            s.stop()
