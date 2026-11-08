"""Application container: builds and owns every component exactly once.

The service entry point constructs one ``App``; routes and helper tools read
components from it.  Keeping construction in one place avoids the "dead knob"
problem (pitfall 12) where declared configuration is never wired up.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Paths, SettingsManager
from .device import DeviceInfo, configure_opencv, resolve as resolve_device
from .logging_setup import configure, get_logger, set_level
from .security import Sealer
from ..pipeline.capture import StreamRegistry
from ..pipeline.discovery import DiscoveryScanner
from ..pipeline.engine import EventWriter, Pipeline, PipelineContext, PipelineMetrics
from ..pipeline.enrolment import EnrolmentService
from ..pipeline.matching import MatchIndex
from ..pipeline.notifications import Notifier
from ..pipeline.relay import HttpRelay, RelayController, SimRelay
from ..pipeline.tracking import TrackManager
from ..storage.db import Database
from ..storage.media import MediaStore
from ..storage.repositories import IdentityRepo
from ..storage.repos_more import (
    AuditRepo,
    CameraRepo,
    EventRepo,
    UserRepo,
    ZoneRepo,
)
from ..vision.face import FaceEngine
from ..vision.person import build_person_detector

log = get_logger("app")


class App:
    def __init__(self, paths: Optional[Paths] = None) -> None:
        self.paths = paths or Paths.load()
        self.paths.ensure()
        self.settings = SettingsManager(self.paths)
        cfg = self.settings.get()
        self.log_file = configure(self.paths.log_dir, cfg.log_level)
        self.sealer = Sealer.from_path(self.paths.secret_key_file)

        # --- storage ---
        self.db = Database(self.paths.db_path)
        self.db.bootstrap()
        self.media = MediaStore(self.paths.media_dir, self.paths.zones_dir)

        # --- repositories ---
        self.identities = IdentityRepo(self.db, self.sealer)
        self.events = EventRepo(self.db)
        self.cameras = CameraRepo(self.db, self.sealer)
        self.users = UserRepo(self.db)
        self.zones = ZoneRepo(self.db)
        self.audit = AuditRepo(self.db)

        # --- vision ---
        # Resolve the execution device once so face and person stages agree.
        self.device: DeviceInfo = resolve_device(self.settings.get().processing_device)
        configure_opencv(self.device)
        self.face = FaceEngine(self.paths.models_dir, device=self.device)
        self.person = build_person_detector(self.paths.models_dir, device=self.device)
        self._refresh_accel()
        self.index = MatchIndex(dim=self.face.feature_size)
        self.index.rebuild(self.identities.load_vectors(actor="boot"))

        # --- pipeline ---
        self.tracks = TrackManager(cfg.track_ttl_seconds)
        self.streams = StreamRegistry()
        self.relay = RelayController()
        self._wire_relay(cfg)
        self.notifier = Notifier()
        self.writer = EventWriter(self.events, self.media, self.settings, self.db.bus)
        self.ctx = PipelineContext(
            settings=self.settings,
            media=self.media,
            person=self.person,
            face=self.face,
            index=self.index,
            tracks=self.tracks,
            writer=self.writer,
            relay=self.relay,
            notifier=self.notifier,
            bus=self.db.bus,
            zone_repo=self.zones,
            audit=self.audit,
            metrics=PipelineMetrics(),
        )
        self.pipeline = Pipeline(self.ctx, self.streams)
        self.discovery = DiscoveryScanner(cfg.discovery_rate_limit_seconds)
        self.enrolment = EnrolmentService(
            self.media, self.identities, self.face, self.index, self.settings, self.audit, self.db.bus
        )

        self.started_at = time.time()
        self._ready = False
        self._lock = threading.RLock()

        self.settings.on_change(self._on_settings_change)
        self.db.bus.publish  # touch to assert availability

    # -- runtime wiring -------------------------------------------------
    def _wire_relay(self, cfg) -> None:
        if cfg.relay_host and cfg.relay_host not in ("sim", "none"):
            self.relay.configure(HttpRelay(cfg.relay_host, cfg.relay_port))
        else:
            self.relay.configure(SimRelay())

    def _on_settings_change(self, new, changed: Dict[str, Any]) -> None:
        """Apply live settings changes to the running system."""
        if "log_level" in changed:
            set_level(new.log_level)
        if any(k in changed for k in ("relay_host", "relay_port")):
            self._wire_relay(new)
        if "processing_device" in changed:
            # Reload the vision stages on the newly resolved device.  The
            # workers pick the objects up on their next frame.
            self.reload_vision(new)
        if "discovery_rate_limit_seconds" in changed:
            self.discovery._rate_limit = new.discovery_rate_limit_seconds
        if changed:
            self.audit.record(
                "settings_changed",
                actor="console",
                target=",".join(sorted(changed.keys())),
                detail=str({k: getattr(new, k) for k in changed}),
            )
        log.info("settings applied", extra={"changed": sorted(changed.keys())})

    # -- lifecycle ------------------------------------------------------
    def reload_vision(self, cfg=None) -> Dict[str, Any]:
        """Rebuild the face/person stages (used after a device change)."""
        cfg = cfg or self.settings.get()
        with self._lock:
            self.device = resolve_device(cfg.processing_device)
            configure_opencv(self.device)
            face = FaceEngine(self.paths.models_dir, device=self.device)
            person = build_person_detector(self.paths.models_dir, device=self.device)
            old_face, old_person = self.face, self.person
            self.face = face
            self.person = person
            self.ctx.face = face
            self.ctx.person = person
            self.enrolment.face = face
            self.index_dim = face.feature_size
        for old in (old_face, old_person):
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("vision stages reloaded", extra={"device": self.device.as_dict()})
        return self.device.as_dict()

    def start(self) -> None:
        with self._lock:
            if self._ready:
                return
            self.pipeline.start_sweeper()
            self.pipeline.sync(self.cameras.list())
            self._ready = True
        log.info("service ready", extra={"port": self.settings.get().port})

    def stop(self) -> None:
        with self._lock:
            if not self._ready:
                return
            self._ready = False
        log.info("shutting down: releasing cameras and flushing queues")
        # flush the write queue before stopping writers (clean shutdown 9.2)
        deadline = time.time() + 5.0
        while self.writer.depth > 0 and time.time() < deadline:
            time.sleep(0.05)
        self.pipeline.stop()
        self.discovery.stop()
        self.db.close()
        log.info("shutdown complete")

    @property
    def ready(self) -> bool:
        return self._ready

    def resync_cameras(self) -> None:
        self.pipeline.sync(self.cameras.list())

    def latest_frame(self, camera_id: int, flavour: str = "annotated"):
        """Newest frame of one camera for the live view (or ``None``).

        ``annotated`` returns the pipeline's overlay frame (boxes, zones);
        ``raw`` returns the capture frame untouched.  Never blocks and never
        starts a camera that nobody is watching.
        """
        cid = int(camera_id)
        stream = self.streams.get(cid)
        if stream is None:
            return None
        if flavour == "annotated":
            buf = self.pipeline.annotated(cid)
            if buf is not None:
                got = buf.get(-1, timeout=0.0)
                if got is not None:
                    return got[0]
            return None
        return stream.snapshot()

    # -- health / resources --------------------------------------------
    def health(self) -> Dict[str, Any]:
        db_ok = False
        try:
            db_ok = self.db.query_one("SELECT 1 AS ok") is not None
        except Exception:  # noqa: BLE001
            db_ok = False
        return {
            "status": "ok" if (self._ready and db_ok) else "degraded",
            "ready": self._ready,
            "database": db_ok,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "version": "1.0.0",
            "server_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "index": self.index.stats(),
            "cameras": self.streams.all_status(),
            "device": self.device.as_dict(),
            # accelerator detail: which backend actually runs each stage, so
            # "is it using the GPU?" is answerable from the console instead of
            # guessed from a log line.
            "accelerator": self._refresh_accel(),
        }

    def _refresh_accel(self) -> Dict[str, Any]:
        """Which backend is *actually* running each stage.

        The setting says what was asked for; this says what happened.  A user
        who selects GPU on a machine without CUDA needs to see that the stages
        fell back, and why, without reading the log.
        """
        info = self.device.as_dict()
        self._accel = {
            "requested": info.get("requested"),
            "device": info.get("device"),
            "reason": info.get("reason"),
            "cuda_devices": info.get("cuda_devices"),
            "opencv_cuda": info.get("opencv_cuda"),
            "dnn_cuda": info.get("dnn_cuda"),
            "dnn_opencl": info.get("dnn_opencl"),
            "ort_usable": info.get("ort_usable"),
            "face_backend": getattr(self.face, "backend", "?"),
            "person_backend": getattr(self.person, "backend", "?"),
            "using_gpu": bool(getattr(self.face, "backend", "") == "onnxruntime"
                              or getattr(self.person, "backend", "") in ("cuda", "opencl")),
        }
        return self._accel

    def resources(self) -> Dict[str, Any]:
        """Resource view.  Every value is read from its real owner - the
        queue depths come from the writer/pipeline objects themselves, not
        from a proxy that always errors (pitfall 1)."""
        import os
        import sys

        cpu_pct = None
        mem_mb = None
        try:
            import psutil  # optional

            proc = psutil.Process(os.getpid())
            cpu_pct = round(proc.cpu_percent(interval=0.0), 1)
            mem_mb = round(proc.memory_info().rss / 1048576.0, 1)
        except Exception:  # noqa: BLE001
            try:
                import resource  # unix only

                mem_mb = round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    / (1024.0 if sys.platform != "darwin" else 1048576.0),
                    1,
                )
            except Exception:
                mem_mb = None
        import cv2

        accel = "cpu"
        try:
            if cv2.cuda.getCudaEnabledDeviceCount() > 0:
                accel = f"cuda x{cv2.cuda.getCudaEnabledDeviceCount()}"
            elif self.device.dnn_opencl and hasattr(cv2, "ocl") and cv2.ocl.useOpenCL():
                accel = "opencl"
        except Exception:
            accel = "cpu"
        pmetrics = self.pipeline.metrics()
        return {
            "cpu_percent": cpu_pct,
            "memory_mb": mem_mb,
            "accelerator": accel,
            "device": self.device.as_dict(),
            "face_backend": getattr(self.face, "backend", "-"),
            "person_backend": getattr(self.person, "backend", "-"),
            "processing_device": self.settings.get().processing_device,
            "identities": self.index.stats()["identities"],
            "reference_rows": self.index.stats()["rows"],
            "queues": pmetrics["queues"],
            "events_per_minute": self.events.count_since(time.time() - 60),
            "cameras": self.streams.all_status(),
            "pipeline": pmetrics,
            "subscribers": self.db.bus.subscriber_count,
            "threads": threading.active_count(),
        }
