"""Whole-image fingerprint for image search (spec 5.7 / pitfall 8).

Compares images, not identity vectors.  The fingerprint is a small, fixed-size
descriptor (difference hash + HSV histogram) so thousands of stored events can
be searched without loading full bitmaps repeatedly.  The fingerprint cache is
bounded and invalidated by event id + file name, so it is not rebuilt per
request (pitfall 8).
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

import cv2
import numpy as np

_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_CACHE_MAX = 5000
_LOCK = threading.Lock()


def fingerprint(image: np.ndarray) -> Optional[np.ndarray]:
    """64-bit dHash + 3x8 normalised HSV histogram -> 25-element unit vector."""
    if image is None or getattr(image, "size", 0) == 0:
        return None
    try:
        small = cv2.resize(image, (9, 8), interpolation=cv2.INTER_AREA)
    except cv2.error:
        return None
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
    bits = (gray[:, 1:] > gray[:, :-1]).astype(np.float32).reshape(-1)  # 64 bits

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [3, 2, 4], [0, 180, 0, 256, 0, 256]).astype(np.float32)
    hist = hist.reshape(-1)
    total = float(hist.sum())
    hist = hist / total if total > 0 else hist

    vec = np.concatenate([bits, hist / (np.linalg.norm(hist) + 1e-9) * 3.0])
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else None


def similarity(a: np.ndarray, b: np.ndarray, cache_key: int = 0) -> float:
    """Cosine similarity mapped to 0..1."""
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(max(0.0, min(1.0, float(a @ b))))


def cached(rel: str, event_id: int, loader) -> Optional[np.ndarray]:
    key = (event_id, rel)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    img = loader(rel)
    fp = fingerprint(img) if img is not None else None
    if fp is not None:
        with _LOCK:
            _CACHE[key] = fp
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
    return fp
