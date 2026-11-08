"""Execution-device resolution: use CUDA when it is really available, else CPU.

Spec 9.6 asks for a swappable processing backend.  The ``processing_device``
setting (auto | cpu | gpu) is resolved here *once* at start-up, and the answer
is recorded so the health view and the console report what is really being used
rather than what was requested.

Design rules:
  - ``gpu`` is a *request*, not a guarantee.  If CUDA is not usable the device
    silently falls back to CPU and the reason is kept for diagnostics.  The
    service must never fail to start because a GPU is missing.
  - Detection is cheap and definitive: ask OpenCV for its CUDA device count and
    ask ONNX Runtime whether it exposes a CUDA execution provider.  No imports
    of optional packages at module import time.
  - The choice is cached so repeated engine construction does not re-probe.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional

from .logging_setup import get_logger

log = get_logger("core.device")


@dataclass
class DeviceInfo:
    """The resolved device plus the evidence used to choose it."""

    device: str = "cpu"                     # "cpu" | "cuda"
    requested: str = "auto"
    cuda_devices: int = 0
    opencv_cuda: bool = False
    ort_providers: List[str] = field(default_factory=list)
    ort_cuda: bool = False
    reason: str = ""

    @property
    def use_cuda(self) -> bool:
        return self.device == "cuda"

    def as_dict(self) -> dict:
        return {
            "device": self.device,
            "requested": self.requested,
            "cuda_devices": self.cuda_devices,
            "opencv_cuda": self.opencv_cuda,
            "ort_cuda": self.ort_cuda,
            "ort_providers": list(self.ort_providers),
            "reason": self.reason,
        }


_lock = threading.Lock()
_cached: Optional[DeviceInfo] = None


def _probe() -> DeviceInfo:
    info = DeviceInfo()

    # --- OpenCV: is this build CUDA-capable, and is a device present? ---
    try:
        import cv2

        getattr(cv2, "cuda", None)  # raises AttributeError on non-CUDA builds
        if hasattr(cv2, "cuda") and hasattr(cv2.cuda, "getCudaEnabledDeviceCount"):
            info.cuda_devices = int(cv2.cuda.getCudaEnabledDeviceCount())
            info.opencv_cuda = info.cuda_devices > 0
    except Exception:  # noqa: BLE001 - optional dependency, never fatal
        info.opencv_cuda = False

    # --- ONNX Runtime: which execution providers are compiled in? ---
    try:
        import onnxruntime as ort

        info.ort_providers = list(ort.get_available_providers())
        info.ort_cuda = any(p.startswith("CUDAExecutionProvider") for p in info.ort_providers)
    except Exception:  # noqa: BLE001 - onnxruntime is optional
        info.ort_providers = []
        info.ort_cuda = False

    return info


def resolve(requested: str = "auto") -> DeviceInfo:
    """Decide the device once, honouring the operator's request when possible.

    ``requested`` is one of ``auto`` / ``cpu`` / ``gpu``.
    """
    global _cached
    with _lock:
        info = _probe()
        info.requested = requested or "auto"
        info.device = "cpu"

        if info.requested == "cpu":
            info.reason = "CPU requested explicitly"
        elif info.requested == "gpu":
            if info.opencv_cuda or info.ort_cuda:
                info.device = "cuda" if info.opencv_cuda else "cpu"
                if info.opencv_cuda:
                    info.reason = f"GPU requested; {info.cuda_devices} CUDA device(s)"
                else:
                    info.reason = (
                        "GPU requested; no OpenCV CUDA devices "
                        "(onnxruntime CUDA provider present but unused by this backend)"
                    )
            else:
                info.reason = "GPU requested but unavailable - falling back to CPU"
        else:  # auto
            if info.opencv_cuda:
                info.device = "cuda"
                info.reason = f"auto: {info.cuda_devices} CUDA device(s) detected"
            elif info.ort_cuda:
                info.device = "cpu"
                info.reason = "auto: onnxruntime has CUDA but no OpenCV CUDA device; using CPU"
            else:
                info.reason = "auto: no CUDA available, using CPU"

        _cached = info
        log.info(
            "processing device resolved",
            extra={
                "device": info.device,
                "requested": info.requested,
                "cuda_devices": info.cuda_devices,
                "opencv_cuda": info.opencv_cuda,
                "ort_cuda": info.ort_cuda,
                "reason": info.reason,
            },
        )
        return info


def current() -> DeviceInfo:
    """Return the cached decision, probing on first use."""
    return _cached if _cached is not None else resolve("auto")


def apply_dnn_target(net, info: DeviceInfo) -> str:
    """Point a ``cv2.dnn`` net at CUDA when resolved, else leave it on CPU.

    Returns the backend actually applied ("cuda" or "cpu").  Some OpenCV builds
    accept the CUDA target and then silently fall back per-layer, so the caller
    is told what was *requested* and the returned value is what we asked for;
    correctness never depends on it.
    """
    if not info.use_cuda:
        return "cpu"
    try:
        import cv2

        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA_FP16)
        return "cuda"
    except Exception as exc:  # noqa: BLE001 - a GPU fault must not stop the service
        log.warning("CUDA target not accepted; staying on CPU", extra={"error": str(exc)})
        return "cpu"
