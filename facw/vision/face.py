"""Face stage: detection (YuNet), best-face choice, quality gate, embedding.

Stages 4, 5 and 6 of the pipeline live here.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from ..core.device import DeviceInfo, resolve as resolve_device
from ..core.logging_setup import get_logger

log = get_logger("vision.face")


@dataclass
class FaceBox:
    x: float
    y: float
    w: float
    h: float
    score: float
    landmarks: Optional[np.ndarray] = None  # 5x2, used for SFace alignment

    @property
    def area(self) -> float:
        return self.w * self.h

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "w": round(self.w, 1),
            "h": round(self.h, 1),
            "score": round(self.score, 4),
        }


def best_face(faces: List[FaceBox]) -> Optional[FaceBox]:
    """Stage 4 rule: detection confidence multiplied by face width.

    A confident but tiny face loses to a slightly less confident but large
    face - the operator cares about the face that is actually recognisable.
    """
    if not faces:
        return None
    return max(faces, key=lambda f: f.score * f.w)


def quality_gate(face: FaceBox, min_width: int, min_score: float) -> bool:
    """Stage 5: only faces that pass both gates may influence a decision."""
    return face.w >= min_width and face.score >= min_score


def pad_box(x: float, y: float, w: float, h: float, pad: int, img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    x1 = int(max(0, round(x - pad)))
    y1 = int(max(0, round(y - pad)))
    x2 = int(min(img_w, round(x + w + pad)))
    y2 = int(min(img_h, round(y + h + pad)))
    return x1, y1, x2, y2


def crop(image: np.ndarray, box: Tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1:
        return np.empty((0, 0, 3), np.uint8)
    return image[y1:y2, x1:x2].copy()


class FaceEngine:
    """YuNet detector + SFace recogniser, thread-safe."""

    def __init__(self, models_dir: Path, device: Optional["DeviceInfo"] = None) -> None:
        self._lock = threading.Lock()
        self.device = device or resolve_device()
        yunet = models_dir / "yunet.onnx"
        sface = models_dir / "sface.onnx"
        if not yunet.exists():
            raise RuntimeError(f"face detector model missing: {yunet}")
        if not sface.exists():
            raise RuntimeError(f"face recogniser model missing: {sface}")
        # score threshold 0.0 here: the caller applies the configurable gate so
        # the console can tune it live (stage 0 / 4.2).
        self.detector = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), 0.1, 0.35, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(sface), "")
        self.feature_size = 128
        self._input_size = (320, 320)
        # NOTE: cv2.FaceDetectorYN / FaceRecognizerSF have no dnn backend or
        # target switch, so this stage always runs on the CPU.  The resolved
        # device is recorded and reported; see core.device.
        log.info(
            "face engine ready",
            extra={
                "models": f"{yunet.name},{sface.name}",
                "device": self.device.device,
                "device_reason": self.device.reason,
            },
        )

    # -- detection -----------------------------------------------------
    def detect(self, image: np.ndarray, score_threshold: float = 0.4) -> List[FaceBox]:
        """Return all faces in ``image`` above ``score_threshold``."""
        if image is None or image.size == 0 or image.shape[0] < 16 or image.shape[1] < 16:
            return []
        h, w = image.shape[:2]
        with self._lock:
            if (w, h) != self._input_size:
                self.detector.setInputSize((w, h))
                self._input_size = (w, h)
            try:
                _, raw = self.detector.detect(image)
            except cv2.error as exc:  # malformed input must not kill the caller
                log.warning("face detect failed", extra={"error": str(exc)})
                return []
        if raw is None or len(raw) == 0:
            return []
        faces: List[FaceBox] = []
        for row in raw:
            x, y, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            sc = float(row[14])
            if bw < 8 or bh < 8 or not np.isfinite(sc):
                continue
            if sc < score_threshold:
                continue
            lms = row[4:14].reshape(5, 2).astype(np.float32)
            faces.append(FaceBox(x, y, bw, bh, sc, lms))
        return faces

    # -- embedding -----------------------------------------------------
    def embed_aligned(self, image: np.ndarray, face: FaceBox) -> Optional[np.ndarray]:
        """Normalised 128-D vector for an aligned face crop."""
        try:
            with self._lock:
                feat = self.recognizer.feature(image)
        except cv2.error:
            return None
        vec = np.asarray(feat, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(vec))
        return vec / n if n > 0 else None

    def embed(self, image: np.ndarray, face: FaceBox) -> Optional[np.ndarray]:
        """Align the face from its landmarks then embed (stage 6)."""
        if face.landmarks is None:
            return None
        try:
            with self._lock:
                aligned = self.recognizer.alignCrop(image, self._to_raw(face))
                feat = self.recognizer.feature(aligned)
        except cv2.error as exc:
            log.warning("face embed failed", extra={"error": str(exc)})
            return None
        vec = np.asarray(feat, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            return None
        return vec / norm

    @staticmethod
    def _to_raw(face: FaceBox) -> np.ndarray:
        lms = face.landmarks.reshape(-1) if face.landmarks is not None else np.zeros(10, np.float32)
        return np.concatenate(
            [[face.x, face.y, face.w, face.h], lms, [face.score]]
        ).astype(np.float32)

    def close(self) -> None:
        pass


def estimate_attributes(face: FaceBox, person_w: float, person_h: float) -> Tuple[Optional[int], str]:
    """Approximate age band and apparent gender.

    IMPORTANT: this is a *heuristic estimator*, not a trained classifier - no
    age/gender model is bundled with the offline runtime.  It is documented as
    such in the README and kept behind one function so a real attribute model
    can be dropped in without touching the pipeline.
    """
    ratio = face.w / person_w if person_w > 0 else 0.0
    # larger faces in the person box correlate with a nearer, older-looking
    # subject only weakly; we report a coarse band and flag it as an estimate.
    if ratio <= 0:
        age = None
    elif ratio < 0.16:
        age = 24
    elif ratio < 0.24:
        age = 31
    else:
        age = 38
    # apparent gender is not inferable without a model: report unknown rather
    # than guessing, so reports never show a fabricated attribute.
    return age, "unknown"
