"""Execution-device resolution: use CUDA when it is really available, else CPU.

Spec 9.6 asks for a swappable processing backend.  The ``processing_device``
setting (auto | cpu | gpu) is resolved here *once* at start-up, and the answer
is recorded so the health view and the console report what is really being used
rather than what was requested.

Design rules:
  - ``gpu`` is a *request*, not a guarantee.  If CUDA is not usable the device
    silently falls back to CPU and the reason is kept for diagnostics.  The
    service must never fail to start because a GPU is missing.
  - Detection is cheap and definitive: ask OpenCV which acceleration backends
    its ``dnn`` module actually advertises, and ask ONNX Runtime whether it
    exposes a CUDA execution provider.  No imports of optional packages at
    module import time.
  - Column-major models (YuNet / SFace) go through ONNX Runtime when it is
    usable, so that stage can also run on CUDA; otherwise they stay on the
    OpenCV path (CPU or OpenCL).
  - The choice is cached so repeated engine construction does not re-probe.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from .logging_setup import get_logger

log = get_logger("core.device")


@dataclass
class DeviceInfo:
    """The resolved device plus the evidence used to choose it."""

    device: str = "cpu"                     # "cpu" | "cuda" | "opencl"
    requested: str = "auto"
    cuda_devices: int = 0
    opencv_cuda: bool = False               # cv2.cuda build *and* a device
    dnn_cuda: bool = False                  # cv2.dnn accepts the CUDA target
    dnn_opencl: bool = False                # cv2.dnn accepts the OpenCL target
    ort_providers: List[str] = field(default_factory=list)
    ort_cuda: bool = False
    ort_usable: bool = False
    reason: str = ""
    details: List[str] = field(default_factory=list)

    @property
    def use_cuda(self) -> bool:
        """True when the resolved device is CUDA (a device exists and OpenCV
        can drive it).  ``dnn_cuda``/``ort_cuda`` then say *which* stages were
        confirmed to accept an accelerated target; ``apply_dnn_target`` probes
        the rest at run time and degrades gracefully."""
        return bool(self.device == "cuda" and self.opencv_cuda)

    @property
    def use_onnxruntime(self) -> bool:
        """True when the face models should be served by ONNX Runtime.

        Used for CUDA.  The OpenCV CPU path is faster than ORT-CPU for these
        tiny models, so plain CPU keeps using OpenCV.
        """
        return bool(self.ort_usable and self.ort_cuda)

    def as_dict(self) -> dict:
        return {
            "device": self.device,
            "requested": self.requested,
            "cuda_devices": self.cuda_devices,
            "opencv_cuda": self.opencv_cuda,
            "dnn_cuda": self.dnn_cuda,
            "dnn_opencl": self.dnn_opencl,
            "ort_cuda": self.ort_cuda,
            "ort_usable": self.ort_usable,
            "ort_providers": list(self.ort_providers),
            "reason": self.reason,
            "details": list(self.details),
        }


_lock = threading.Lock()
_cached: Optional[DeviceInfo] = None


def _cuda_device_count() -> int:
    """Number of CUDA devices OpenCV reports (0 on non-CUDA builds)."""
    try:
        import cv2

        if not hasattr(cv2, "cuda") or not hasattr(cv2.cuda, "getCudaEnabledDeviceCount"):
            return 0
        return int(cv2.cuda.getCudaEnabledDeviceCount())
    except Exception:  # noqa: BLE001 - optional dependency, never fatal
        return 0


def _cuda_usable() -> bool:
    """A device exists *and* OpenCV reports it as usable.

    ``cv2.cuda.printCudaDeviceInfo`` prints only; rather than depending on
    its output, we ask the build for the device count (cheap, definitive) and
    treat a build without the CUDA module as "no GPU".
    """
    if _cuda_device_count() <= 0:
        return False
    try:
        import cv2

        locker = getattr(getattr(cv2, "cuda", None), "DeviceInfo", None)
        if locker is not None:
            return bool(cv2.cuda.DeviceInfo(0).isCompatible())
    except Exception:  # noqa: BLE001 - the probe is advisory
        pass
    return True


def _probe_dnn_targets() -> tuple:
    """Ask cv2.dnn which acceleration targets it really advertises.

    ``DNN_BACKEND_CUDA``/``DNN_TARGET_CUDA`` exist on every build, so merely
    having the constant proves nothing.  The query below is answered by the
    backend registry: it is what actually consumes the target.
    """
    cuda = False
    opencl = False
    try:
        import cv2

        dnn = getattr(cv2, "dnn", None)
        if dnn is None:
            return False, False
        query = getattr(dnn, "getAvailableTargets", None)
        targets = set()
        backend_cuda = getattr(dnn, "DNN_BACKEND_CUDA", None)
        if query is not None and backend_cuda is not None:
            try:
                targets = {int(t) for t in query(backend_cuda)}
            except Exception:  # noqa: BLE001
                targets = set()
        cuda_targets = {
            t
            for t in (
                getattr(dnn, "DNN_TARGET_CUDA", None),
                getattr(dnn, "DNN_TARGET_CUDA_FP16", None),
            )
            if t is not None
        }
        cuda = bool(cuda_targets & targets)
        # OpenCL (T-API) is advertised for the default backend.
        backend_default = getattr(dnn, "DNN_BACKEND_DEFAULT", 0)
        try:
            default_targets = {int(t) for t in query(backend_default)} if query else set()
        except Exception:  # noqa: BLE001
            default_targets = set()
        opencl = getattr(dnn, "DNN_TARGET_OPENCL", None) in default_targets
        opencl = opencl or getattr(dnn, "DNN_TARGET_OPENCL_FP16", None) in default_targets
    except Exception:  # noqa: BLE001
        return False, False
    return cuda, opencl


def _probe_onnxruntime() -> tuple:
    """(providers, cuda_available, usable, details) for ONNX Runtime, if present."""
    try:
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
        has_cuda = any(p.startswith("CUDAExecutionProvider") for p in providers)
        usable = False
        notes: List[str] = []
        if has_cuda:
            # The provider is compiled in but needs a CUDA/cuDNN runtime on the
            # machine.  Creating one session is the honest test: a missing DLL
            # only fails here, not at get_available_providers().  The model is
            # deliberately invalid, so *any* model/parse error means the
            # provider itself loaded.
            try:
                import numpy as np

                opt = ort.SessionOptions()
                opt.log_severity_level = 3
                sess = ort.InferenceSession(
                    np.zeros((1, 1), dtype=np.float32).tobytes(),
                    sess_options=opt,
                    providers=["CUDAExecutionProvider"],
                )
                del sess
                usable = True
            except Exception as exc:  # noqa: BLE001 - invalid model OR runtime fault
                first = (str(exc).splitlines() or [""])[0][:200]
                low = first.lower()
                fatal = any(k in low for k in ("cuda", "cudnn", "dll", "loadlibrary", "not found"))
                usable = not fatal
                if fatal:
                    notes.append(f"onnxruntime CUDA provider cannot start: {first}")
        return providers, has_cuda, usable, notes
    except Exception as exc:  # noqa: BLE001 - onnxruntime is optional
        return [], False, False, [f"onnxruntime unavailable: {exc}"]


def _probe() -> DeviceInfo:
    info = DeviceInfo()

    # --- OpenCV: is this build CUDA-capable, and is a device present? ---
    info.cuda_devices = _cuda_device_count()
    info.opencv_cuda = info.cuda_devices > 0 and _cuda_usable()
    if os.environ.get("OPENCV_OPENCL_RUNTIME", "").strip().lower() in ("disabled", "none", "0"):
        info.dnn_opencl = False
    else:
        _dnn_cuda, info.dnn_opencl = _probe_dnn_targets()
        # The CUDA target is only meaningful when a device exists.
        info.dnn_cuda = bool(_dnn_cuda and info.opencv_cuda)

    # --- ONNX Runtime: which execution providers are compiled in? ---
    providers, ort_cuda, ort_usable, notes = _probe_onnxruntime()
    info.ort_providers = providers
    info.ort_cuda = bool(ort_cuda and ort_usable)
    info.ort_usable = ort_usable
    info.details.extend(notes)

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

        gpu_paths = []
        if info.opencv_cuda:
            gpu_paths.append(f"cv2.dnn CUDA ({info.cuda_devices} device(s))")
        if info.ort_cuda:
            gpu_paths.append("onnxruntime CUDA")

        if info.requested == "cpu":
            info.reason = "CPU requested explicitly"
        elif info.requested == "gpu":
            if gpu_paths:
                info.device = "cuda"
                info.reason = "GPU requested; " + ", ".join(gpu_paths)
            else:
                if info.cuda_devices and not info.opencv_cuda:
                    info.reason = (
                        "GPU requested but OpenCV cannot use the CUDA device "
                        f"(devices={info.cuda_devices}); falling back to CPU"
                    )
                elif info.cuda_devices:
                    info.reason = "GPU requested but no acceleration path accepted it - falling back to CPU"
                else:
                    info.reason = "GPU requested but no CUDA device found - falling back to CPU"
                if info.ort_providers and not info.ort_cuda:
                    info.details.append(
                        "onnxruntime provider list: " + ",".join(info.ort_providers)
                    )
        else:  # auto
            if gpu_paths:
                info.device = "cuda"
                info.reason = "auto: " + ", ".join(gpu_paths)
            elif info.dnn_opencl:
                info.device = "opencl"
                info.reason = "auto: no CUDA device, using OpenCL (T-API)"
            else:
                info.reason = "auto: no CUDA device and no OpenCL, using CPU"

        _cached = info
        log.info(
            "processing device resolved",
            extra={
                "device": info.device,
                "requested": info.requested,
                "cuda_devices": info.cuda_devices,
                "opencv_cuda": info.opencv_cuda,
                "dnn_cuda": info.dnn_cuda,
                "dnn_opencl": info.dnn_opencl,
                "ort_cuda": info.ort_cuda,
                "ort_usable": info.ort_usable,
                "reason": info.reason,
            },
        )
        return info


def current() -> DeviceInfo:
    """Return the cached decision, probing on first use."""
    return _cached if _cached is not None else resolve("auto")


def reset(requested: Optional[str] = None) -> DeviceInfo:
    """Forget the cached decision so a settings change can be applied live."""
    global _cached
    with _lock:
        _cached = None
    return resolve(requested or "auto")


def configure_opencv(info: DeviceInfo) -> None:
    """Enable OpenCL when that is the resolved device (T-API, process wide)."""
    if not info.dnn_opencl:
        return
    try:
        import cv2

        if hasattr(cv2, "ocl") and hasattr(cv2.ocl, "setUseOpenCL"):
            cv2.ocl.setUseOpenCL(True)
    except Exception:  # noqa: BLE001
        pass


def use_gpu(info: DeviceInfo) -> bool:
    """True when the resolved device is CUDA (OpenCV can drive a device).

    Being defensive here prevents a crash that has bitten OpenCV users: with a
    CUDA *build* but no GPU, loading a cuDNN-capable graph throws
    ``Can't create layer ... No graph engine backend``.  The count therefore
    gates every accelerated backend, not the build flags.
    """
    return bool(info.device == "cuda" and info.cuda_devices > 0)


def apply_dnn_target(net, info: DeviceInfo) -> str:
    """Point a ``cv2.dnn`` net at CUDA/OpenCL when resolved, else leave it on CPU.

    Returns the backend actually applied ("cuda" | "opencl" | "cpu").  Some
    OpenCV builds accept the CUDA target and then silently fall back per-layer,
    so the caller is told what was *requested*; correctness never depends on it.
    """
    import cv2

    dnn = getattr(cv2, "dnn", None)
    if dnn is None:
        return "cpu"

    if use_gpu(info):
        try:
            net.setPreferableBackend(dnn.DNN_BACKEND_CUDA)
            net.setPreferableTarget(dnn.DNN_TARGET_CUDA_FP16)
            return "cuda"
        except Exception as exc:  # noqa: BLE001 - a GPU fault must not stop the service
            log.warning("CUDA target not accepted; trying OpenCL", extra={"error": str(exc)})
            try:
                net.setPreferableBackend(dnn.DNN_BACKEND_DEFAULT)
                net.setPreferableTarget(dnn.DNN_TARGET_CUDA)
                return "cuda"
            except Exception:  # noqa: BLE001
                pass
            try:
                net.setPreferableBackend(dnn.DNN_BACKEND_DEFAULT)
                net.setPreferableTarget(dnn.DNN_TARGET_OPENCL)
                return "opencl"
            except Exception:  # noqa: BLE001
                return "cpu"

    if info.device == "opencl" and info.dnn_opencl:
        try:
            net.setPreferableBackend(dnn.DNN_BACKEND_DEFAULT)
            net.setPreferableTarget(dnn.DNN_TARGET_OPENCL)
            return "opencl"
        except Exception as exc:  # noqa: BLE001
            log.warning("OpenCL target not accepted; staying on CPU", extra={"error": str(exc)})
    return "cpu"


def ort_providers(info: DeviceInfo) -> List:
    """Providers for an ONNX Runtime session, best first."""
    if info.ort_cuda:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]
