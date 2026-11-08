"""Person detection - MediaPipe person detector (ONNX) driven by cv2.dnn.

Pure function of (frame, settings) -> list of detections with Nx4 boxes and
scores.  Kept behind a small interface so another detector can be swapped in
(spec 9.6).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

import cv2
import numpy as np

from ..core.device import DeviceInfo, apply_dnn_target, resolve as resolve_device
from ..core.logging_setup import get_logger

log = get_logger("vision.person")


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def as_list(self) -> List[float]:
        return [self.x1, self.y1, self.x2, self.y2, self.score]


class PersonDetector(Protocol):
    def detect(self, frame: np.ndarray, score_threshold: float) -> List[Detection]: ...

    def close(self) -> None: ...


def _iou(a: Detection, b: Detection) -> float:
    x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
    x2, y2 = min(a.x2, b.x2), min(a.y2, b.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def clamp_box(x1: float, y1: float, x2: float, y2: float, w: int, h: int) -> Optional[Detection]:
    """Clamp to the frame and drop degenerate boxes (stage 3)."""
    x1 = max(0.0, min(float(x1), w - 1.0))
    y1 = max(0.0, min(float(y1), h - 1.0))
    x2 = max(0.0, min(float(x2), w - 1.0))
    y2 = max(0.0, min(float(y2), h - 1.0))
    if x2 - x1 < 8 or y2 - y1 < 16:
        return None
    return Detection(x1, y1, x2, y2, 0.0)


class MediaPipePersonDetector:
    """Reimplementation of the opencv_zoo MediaPipe person detector."""

    INPUT_SIZE = np.array([224, 224])  # hw

    def __init__(self, model_path: Path, device: Optional["DeviceInfo"] = None) -> None:
        self._lock = threading.Lock()
        self.device = device or resolve_device()
        self.net = cv2.dnn.readNet(str(model_path))
        # This net is a plain dnn graph, so the CUDA target applies here.
        self.backend = apply_dnn_target(self.net, self.device)
        self._anchors = self._load_anchors()
        log.info(
            "person detector loaded",
            extra={
                "model": str(model_path),
                "device": self.device.device,
                "backend": self.backend,
                "reason": self.device.reason,
            },
        )

    @staticmethod
    def _load_anchors() -> np.ndarray:
        """Expand the model's multi-scale anchor table to a flat (N, 2) array.

        The opencv_zoo reference hard-codes 2254 coordinate pairs.  They are
        kept as structured rows in ``_anchor_rows`` rather than 2254 literals,
        and the count is asserted so a model swap fails loudly instead of
        silently producing wrong boxes.
        """
        from ._anchor_rows import ANCHOR_ROWS

        anchors = np.array(
            [[x, y] for y, xs in ANCHOR_ROWS for x in xs], dtype=np.float32
        )
        if anchors.shape[0] != 2254:  # pragma: no cover - guarded by tests
            raise RuntimeError(f"unexpected anchor count {anchors.shape[0]}")
        return anchors

    def _ensure_anchor_count(self, n: int) -> None:
        if len(self._anchors) != n:  # pragma: no cover
            raise RuntimeError(f"anchor/model mismatch: {len(self._anchors)} vs {n}")

    def _preprocess(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = (img - 0.5) * 2.0
        ratio = min(self.INPUT_SIZE / np.array([h, w]))
        new_w, new_h = int(round(w * ratio)), int(round(h * ratio))
        resized = cv2.resize(img, (max(1, new_w), max(1, new_h)))
        pad_h, pad_w = self.INPUT_SIZE[0] - new_h, self.INPUT_SIZE[1] - new_w
        top, left = pad_h // 2, pad_w // 2
        padded = cv2.copyMakeBorder(
            resized, top, pad_h - top, left, pad_w - left, cv2.BORDER_CONSTANT, value=(0, 0, 0)
        )
        blob = np.transpose(padded, [2, 0, 1])[np.newaxis, :, :, :]
        pad_bias = np.array([left, top], dtype=np.float32) / ratio
        return np.ascontiguousarray(blob, dtype=np.float32), pad_bias

    def detect(self, frame: np.ndarray, score_threshold: float = 0.4) -> List[Detection]:
        if frame is None or frame.size == 0:
            return []
        h, w = frame.shape[:2]
        blob, pad_bias = self._preprocess(frame)
        with self._lock:
            self.net.setInput(blob)
            outs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        # The graph exposes two outputs: scores (N, 1) and regressors (N, 12).
        # Select by shape rather than by position, because the ordering differs
        # between OpenCV builds (the opencv_zoo reference assumes regressors
        # first; OpenCV 5.0 emits scores first).
        reg_out = next(o for o in outs if o.shape[-1] > 4)
        score_out = next(o for o in outs if o.shape[-1] == 1)
        reg = reg_out[0]
        sco = score_out[0][:, 0]
        self._ensure_anchor_count(reg.shape[0])

        scores = 1.0 / (1.0 + np.exp(-np.clip(sco.astype(np.float64), -100, 100)))
        keep_pre = scores >= max(0.05, score_threshold * 0.5)
        if not np.any(keep_pre):
            return []
        reg = reg[keep_pre]
        scores = scores[keep_pre]
        anchors = self._anchors[keep_pre]

        scale = float(max(h, w))
        cxy = reg[:, :2] / self.INPUT_SIZE
        wh = reg[:, 2:4] / self.INPUT_SIZE
        xy1 = (cxy - wh / 2 + anchors) * scale
        xy2 = (cxy + wh / 2 + anchors) * scale
        boxes = np.concatenate([xy1, xy2], axis=1)
        boxes -= [pad_bias[0], pad_bias[1], pad_bias[0], pad_bias[1]]

        dets: List[Detection] = []
        for box, sc in zip(boxes, scores):
            clamped = clamp_box(box[0], box[1], box[2], box[3], w, h)
            if clamped is None:
                continue
            clamped.score = float(sc)
            dets.append(clamped)
        return nms(dets, 0.25)

    def close(self) -> None:
        self.net = None  # type: ignore[assignment]


def _containment(inner: Detection, outer: Detection) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer``."""
    x1, y1 = max(inner.x1, outer.x1), max(inner.y1, outer.y1)
    x2, y2 = min(inner.x2, outer.x2), min(inner.y2, outer.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return inter / inner.area if inner.area > 0 else 0.0


def nms(dets: List[Detection], thresh: float = 0.25) -> List[Detection]:
    """Greedy non-maximum suppression.

    Default 0.25 (not 0.45): this model emits several slabs per person offset
    by roughly half a box, whose IoU is only ~0.4-0.5, so a loose threshold
    left 6 boxes on one stationary person - and 6 parallel tracks.
    """
    if not dets:
        return []
    kept: List[Detection] = []
    for d in sorted(dets, key=lambda x: x.score, reverse=True):
        if any(
            _iou(d, k) > thresh or _containment(d, k) > 0.85 or _containment(k, d) > 0.85
            for k in kept
        ):
            continue
        kept.append(d)
    return kept


class HaarPersonDetector:
    """Very small fallback: OpenCV HOG people detector.

    Only used if the ONNX model file is missing, so the service still starts
    and the console still works (degraded).
    """

    def __init__(self) -> None:
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        log.warning("person detector: falling back to HOG (ONNX model missing)")

    def detect(self, frame: np.ndarray, score_threshold: float = 0.4) -> List[Detection]:
        if frame is None or frame.size == 0:
            return []
        h, w = frame.shape[:2]
        rects, weights = self.hog.detectMultiScale(frame, winStride=(8, 8), padding=(8, 8), scale=1.08)
        out: List[Detection] = []
        for (x, y, bw, bh), sc in zip(rects, weights):
            c = clamp_box(x, y, x + bw, y + bh, w, h)
            if c is not None:
                c.score = float(min(1.0, max(0.0, sc)))
                out.append(c)
        return nms(out, 0.5)

    def close(self) -> None:
        pass


def build_person_detector(models_dir: Path, device: Optional[DeviceInfo] = None) -> PersonDetector:
    path = models_dir / "persondet.onnx"
    try:
        if path.exists():
            return MediaPipePersonDetector(path, device=device)
    except Exception as exc:  # noqa: BLE001
        log.error("person detector init failed", extra={"error": str(exc)})
    return HaarPersonDetector()
