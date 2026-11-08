"""Artefact storage: write/read/delete event and reference images.

Every sub-directory is created unconditionally at startup (pitfall 4).  Deletes
are idempotent and never raise on a missing file (pitfall 3: a double unlink
must not fail a request whose record was already removed).
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np

from ..core.logging_setup import get_logger

log = get_logger("media")

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SUBDIRS = ("frames", "faces", "persons", "temp", "refs", "avatars")


class MediaStore:
    def __init__(self, media_root: Path, zones_dir: Path) -> None:
        self.root = Path(media_root)
        self.zones_dir = Path(zones_dir)
        self._lock = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        # unconditional creation of every directory we write into
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.zones_dir.mkdir(parents=True, exist_ok=True)

    # -- path safety ---------------------------------------------------
    def resolve(self, subdir: str, name: str) -> Optional[Path]:
        """Resolve a stored relative name safely (no traversal)."""
        if not name or not SAFE_NAME.match(os.path.basename(name)):
            return None
        if subdir not in SUBDIRS:
            return None
        path = (self.root / subdir / os.path.basename(name)).resolve()
        try:
            path.relative_to((self.root / subdir).resolve())
        except ValueError:
            return None
        return path

    def path_of(self, relative: str) -> Optional[Path]:
        """``"frames/abc.jpg"`` -> absolute path (used by the API for serving)."""
        if not relative:
            return None
        parts = relative.replace("\\", "/").split("/")
        if len(parts) != 2:
            return None
        return self.resolve(parts[0], parts[1])

    # -- writes --------------------------------------------------------
    def _write_image(self, subdir: str, img: np.ndarray, quality: int, prefix: str) -> str:
        if img is None or getattr(img, "size", 0) == 0:
            return ""
        safe_q = max(20, min(100, int(quality)))
        name = f"{prefix}_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}.jpg"
        dest = self.root / subdir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = cv2.imwrite(str(dest), img, [int(cv2.IMWRITE_JPEG_QUALITY), safe_q])
        if not ok:
            log.error("artefact write failed", extra={"path": str(dest)})
            return ""
        return f"{subdir}/{name}"

    def save_event_artefacts(
        self, frame: np.ndarray, face_crop: np.ndarray, person_crop: np.ndarray, quality: int
    ) -> Dict[str, str]:
        stamp = f"ev{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"
        # Deliberately no temp-file dance: the buffer is written once, and any
        # failure returns "" without touching an already stored record.
        return {
            "frame": self._write_image("frames", frame, quality, stamp),
            "face": self._write_image("faces", face_crop, quality, stamp),
            "person": self._write_image("persons", person_crop, quality, stamp),
        }

    def save_reference(self, img: np.ndarray, quality: int = 92) -> str:
        return self._write_image("refs", img, quality, "ref")

    def save_avatar(self, img: np.ndarray, quality: int = 92) -> str:
        return self._write_image("avatars", img, quality, "av")

    def save_upload(self, data: bytes, suffix: str = ".jpg") -> Optional[Path]:
        """Store an untrusted upload in temp/ for processing."""
        suffix = suffix.lower() if suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp") else ".jpg"
        dest = self.root / "temp" / f"up_{uuid.uuid4().hex}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    # -- deletes -------------------------------------------------------
    def delete(self, relative: str) -> bool:
        path = self.path_of(relative)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False  # idempotent: already gone is a success-ish no-op
        except OSError as exc:
            log.warning("artefact delete failed", extra={"path": str(path), "error": str(exc)})
            return False

    def delete_event_artefacts(self, frame: str, face: str, person: str) -> None:
        for rel in (frame, face, person):
            if rel:
                self.delete(rel)

    def read_image(self, relative: str) -> Optional[np.ndarray]:
        path = self.path_of(relative)
        if path is None or not path.exists():
            return None
        img = cv2.imread(str(path))
        return img


def image_bytes(frame: np.ndarray, quality: int = 78, max_width: int = 0) -> bytes:
    """Encode a frame as JPEG for the live view.

    ``max_width`` downscales wide camera frames before encoding: a 2560px
    mosaic tile is invisible at display size and costs 4x the bandwidth.  Used
    by the live-frame endpoint and the MJPEG streamer.
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return b""
    img = frame
    if max_width and img.shape[1] > max_width:
        scale = max_width / float(img.shape[1])
        img = cv2.resize(img, (max_width, max(1, int(img.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""
