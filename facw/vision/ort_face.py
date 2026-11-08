"""Thin ONNX Runtime wrapper for the face models (YuNet + SFace).

The OpenCV CPU path stays the default because it is fast for these two small
models.  When the resolved device is CUDA, the same ONNX graphs are served by
ONNX Runtime so the face stage can use the GPU too; this module reproduces the
pre/post-processing that ``cv2.FaceDetectorYN`` / ``cv2.FaceRecognizerSF`` do
internally, with the constants taken from OpenCV's own source:

* YuNet  : letterbox to ``nms_threshold`` square, mean subtraction, NMS, then
           decode ``(x, y) * (w, h) * W/H`` from the raw anchors.
* SFace  : 112x112 aligned crop (``facealigner`` template), 1/255 scaling,
           BGR->RGB.

Everything here is optional: if anything fails to load, ``build`` returns
``None`` and the caller keeps the OpenCV implementation.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .logging_setup import get_logger

log = get_logger("vision.ort")

# --- YuNet -----------------------------------------------------------------
YU_SIGMA = 0.9
YU_MEAN = (0.485, 0.456, 0.406)
YU_SCALE = 1.0 / 255.0
YU_SIZE = np.array([320, 320], dtype=np.float32)

# --- SFace -----------------------------------------------------------------
SFACE_MEAN = np.array([0.5, 0.5, 0.5], dtype=np.float32)
SFACE_STD = np.array([0.5, 0.5, 0.5], dtype=np.float32)
ALIGN_SIZE = np.array([112.0, 112.0], dtype=np.float32)
# ArcFace/FaceAligners 5-point template, as used by cv2.FaceRecognizerSF.
ALIGN_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        return 1.0 / (1.0 + np.exp(-x))


class OrtFaceModels:
    """YuNet detection + SFace embedding on ONNX Runtime providers."""

    def __init__(self, yunet_path: Path, sface_path: Path, providers: List[str]) -> None:
        import onnxruntime as ort

        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.providers = providers
        self.det = ort.InferenceSession(str(yunet_path), sess_options=opt, providers=providers)
        self.rec = ort.InferenceSession(str(sface_path), sess_options=opt, providers=providers)
        self.det_in = self.det.get_inputs()[0].name
        self.rec_in = self.rec.get_inputs()[0].name
        self._known = set(tuple(i.shape) for i in self.rec.get_inputs())
        log.info(
            "onnxruntime face sessions ready",
            extra={
                "providers": ",".join(self.det.get_providers()),
                "yunet_inputs": str([(i.name, i.shape) for i in self.det.get_inputs()]),
                "sface_inputs": str([(i.name, i.shape) for i in self.rec.get_inputs()]),
            },
        )

    # -- detection ---------------------------------------------------
    def detect(self, image: np.ndarray, score_threshold: float = 0.4, nms_threshold: float = 0.35) -> Optional[np.ndarray]:
        """Return an ``(N, 15)`` array like cv2.FaceDetectorYN.detect."""
        if image is None or image.size == 0:
            return None
        h, w = image.shape[:2]
        if h < 16 or w < 16:
            return None
        scale = min(YU_SIZE[0] / w, YU_SIZE[1] / h)
        new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((int(YU_SIZE[1]), int(YU_SIZE[0]), 3), dtype=np.uint8)
        canvas[:] = (104, 117, 123)  # the value OpenCV pads with
        canvas[:new_h, :new_w] = resized
        blob = cv2.dnn.blobFromImage(
            canvas, YU_SCALE, (int(YU_SIZE[0]), int(YU_SIZE[1])), YU_MEAN, swapRB=True, crop=False
        )
        try:
            outs = self.det.run(None, {self.det_in: blob})
        except Exception as exc:  # noqa: BLE001 - a broken provider must not kill the call
            log.warning("yunet(ort) failed", extra={"error": str(exc)})
            return None
        if not outs:
            return None

        # The graph emits several heads; pick them by shape.
        cls_list, obj_list, bbox_list, kps_list = [], [], [], []
        for out in outs:
            o = np.asarray(out)
            if o.ndim != 3:
                continue
            o = o[0]
            for i in range(o.shape[1]):
                col = np.ascontiguousarray(o[:, i])
                if col.shape[0] == 1:
                    cls_list.append(col)
                elif col.shape[0] == 4:
                    bbox_list.append(col)
                elif col.shape[0] == 10:
                    kps_list.append(col)
                elif col.shape[0] == -1:
                    obj_list.append(col)
        if not cls_list or not bbox_list:
            return None
        cls_v = _sigmoid(np.concatenate(cls_list).reshape(-1))
        bbox_v = np.concatenate(bbox_list, axis=1).T  # (N, 4)
        keep = cls_v >= score_threshold
        if not np.any(keep):
            return None
        bbox_v = bbox_v[keep]
        cls_v = cls_v[keep]
        kps_v = np.concatenate(kps_list, axis=1).T[keep] if kps_list else np.zeros((bbox_v.shape[0], 0))

        rects = np.zeros((bbox_v.shape[0], 4), dtype=np.float32)
        rects[:, 0] = (bbox_v[:, 0] - bbox_v[:, 2] / 2) * w / scale
        rects[:, 1] = (bbox_v[:, 1] - bbox_v[:, 3] / 2) * h / scale
        rects[:, 2] = bbox_v[:, 2] * w / scale
        rects[:, 3] = bbox_v[:, 3] * h / scale
        idx = cv2.dnn.NMSBoxes(
            [list(map(float, r)) for r in rects], [float(s) for s in cls_v], score_threshold, nms_threshold
        )
        if idx is None or len(idx) == 0:
            return None
        idx = np.array(idx).reshape(-1)
        rows = []
        for i in idx:
            pts = kps_v[i] * (np.array([w, h]) / scale) if kps_v.size else np.zeros(10, np.float32)
            rows.append(np.concatenate([[rects[i, 0], rects[i, 1], rects[i, 2], rects[i, 3]], pts, [cls_v[i]]]))
        return np.asarray(rows, dtype=np.float32)

    # -- embedding ---------------------------------------------------
    def embedding(self, image: np.ndarray, landmarks: np.ndarray) -> Optional[np.ndarray]:
        """128-D feature for an aligned crop, matching cv2.FaceRecognizerSF."""
        try:
            aligned = _align(image, landmarks)
        except cv2.error:
            return None
        if aligned is None:
            return None
        x = (aligned.astype(np.float32) / 255.0 - SFACE_MEAN) / SFACE_STD
        x = np.transpose(x, (2, 0, 1))[np.newaxis, :, :, :]
        try:
            outs = self.rec.run(None, {self.rec_in: np.ascontiguousarray(x)})
        except Exception as exc:  # noqa: BLE001
            log.warning("sface(ort) failed", extra={"error": str(exc)})
            return None
        vec = np.asarray(outs[0], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None


def _align(image: np.ndarray, landmarks: np.ndarray) -> Optional[np.ndarray]:
    pts = np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    M, _ = cv2.estimateAffinePartial2D(pts, ALIGN_TEMPLATE, method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(image, M, (int(ALIGN_SIZE[0]), int(ALIGN_SIZE[1])), borderValue=0.0)


def build(yunet_path: Path, sface_path: Path, providers: List[str]) -> Optional[OrtFaceModels]:
    try:
        return OrtFaceModels(yunet_path, sface_path, providers)
    except Exception as exc:  # noqa: BLE001 - optional acceleration
        log.info("onnxruntime face path unavailable", extra={"error": str(exc)})
        return None
