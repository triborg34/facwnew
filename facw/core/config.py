"""Runtime configuration.

One place for every threshold, path and port (spec 4.1 / 9.6).

Two layers:

* ``Paths`` - process-level, read once at start (data dir, models dir, etc).
* ``RuntimeSettings`` - the operator-tunable knobs (stage 0).  Held in a
  thread-safe holder so the console can change a value and the running
  pipeline picks it up on the very next frame, with no restart.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def _base_dir() -> Path:
    """Project root: the parent of the ``facw`` package directory."""
    return Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Paths:
    root: Path
    data_dir: Path
    db_path: Path
    media_dir: Path
    log_dir: Path
    models_dir: Path
    console_dir: Path
    zones_dir: Path
    backup_dir: Path
    secret_key_file: Path

    @staticmethod
    def load() -> "Paths":
        root = _base_dir()
        data = Path(os.environ.get("FACW_DATA_DIR", root / "data")).resolve()
        return Paths(
            root=root,
            data_dir=data,
            db_path=data / "facw.db",
            media_dir=data / "media",
            log_dir=Path(os.environ.get("FACW_LOG_DIR", root / "logs")).resolve(),
            models_dir=Path(os.environ.get("FACW_MODELS_DIR", root / "models")).resolve(),
            console_dir=root / "web" / "console",
            zones_dir=data / "zones",
            backup_dir=data / "backups",
            secret_key_file=data / "secret.key",
        )

    def ensure(self) -> None:
        """Create every directory we own.  Always creates each one
        (pitfall 4: previously sub-dirs were only made when the parent was
        missing, so a partially populated tree broke a later save)."""
        for p in (
            self.data_dir,
            self.media_dir,
            self.log_dir,
            self.zones_dir,
            self.backup_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        # artefact sub-directories - created unconditionally
        for name in ("frames", "faces", "persons", "temp", "refs", "avatars"):
            (self.media_dir / name).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Runtime settings (stage 0 of the recognition pipeline)
# --------------------------------------------------------------------------
APPROVAL_ROLES = ("approve", "deny", "visitor", "colleague")
GENDERS = ("male", "female", "unknown")


@dataclass
class RuntimeSettings:
    # --- engine tuning ------------------------------------------------
    face_accept_threshold: float = 0.6          # face detection score gate (stage 4)
    person_confidence: float = 0.42             # person detection threshold (stage 3)
    match_threshold: float = 0.36               # identity similarity threshold (stage 7)
    face_crop_padding: int = 18                 # pixels around the face crop
    snapshot_quality: int = 88                  # jpeg quality of artefacts
    overlap_threshold: float = 0.35             # detector/association IoU (stage 3)
    ambiguity_margin: float = 0.06              # best-second-best gap (stage 7)
    zone_mode: bool = False                     # stage 2 on/off
    access_control: bool = False                # relay mode (stage 12)
    relay_host: str = "192.168.1.100"
    relay_port: int = 5000
    relay_channel_1: str = "relay1"             # cross-platform name of channel 1
    relay_channel_2: str = "relay2"             # second channel; blank = single
    relay_pulse_ms: int = 1500                  # bounded pulse duration
    samples_required: int = 5                   # votes before an identity is committed
    fast_sample_ms: int = 120                   # cadence for undecided tracks (stage 16)
    slow_sample_ms: int = 900                   # cadence for decided tracks
    min_face_width: int = 52                    # quality gate (stage 5)
    min_face_score: float = 0.62                # quality gate (stage 5)
    processing_device: str = "auto"             # auto | cpu | gpu
    track_ttl_seconds: float = 3.0              # stage 9
    dedup_window_seconds: float = 10.0          # stage 11; scope is per-identity+camera

    # --- service ------------------------------------------------------
    port: int = 8770
    log_level: str = "info"                     # info | debug | warning | error
    alarm_enabled: bool = False
    notify_method: str = "log"                  # log | smtp
    notify_recipient: str = "operator@example.local"
    smtp_host: str = ""
    smtp_port: int = 25
    report_lock: bool = False
    retention_days: int = 30                    # 0 = keep forever
    max_upload_mb: int = 12
    discovery_rate_limit_seconds: float = 10.0

    # --- placement tuning (stage 3 / 4) -------------------------------
    person_max_side: int = 640                  # detector input cap
    person_crop_upscale: float = 1.0

    # ------------------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeSettings":
        """Coerce a stored dict into a valid settings object.

        Unknown keys are ignored, wrong types fall back to defaults, and
        numeric ranges are clamped.  Never raises: settings must always load.
        """
        s = cls()
        valid = {f.name: f for f in fields(cls)}
        for key, raw in (data or {}).items():
            f = valid.get(key)
            if f is None:
                continue
            try:
                if f.type in ("int", int):
                    val: Any = int(raw)
                elif f.type in ("float", float):
                    val = float(raw)
                elif f.type in ("bool", bool):
                    val = bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
                else:
                    val = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(s, key, val)
        s.validate()
        return s

    def validate(self) -> None:
        def clamp(name: str, lo, hi) -> None:
            v = getattr(self, name)
            setattr(self, name, max(lo, min(hi, v)))

        clamp("face_accept_threshold", 0.10, 0.99)
        clamp("person_confidence", 0.05, 0.99)
        clamp("match_threshold", 0.05, 0.99)
        clamp("ambiguity_margin", 0.0, 0.50)
        clamp("face_crop_padding", 0, 200)
        clamp("snapshot_quality", 30, 100)
        clamp("overlap_threshold", 0.05, 0.95)
        clamp("relay_pulse_ms", 100, 10_000)
        clamp("samples_required", 1, 50)
        clamp("fast_sample_ms", 30, 5_000)
        clamp("slow_sample_ms", 60, 10_000)
        clamp("min_face_width", 16, 512)
        clamp("min_face_score", 0.05, 0.99)
        clamp("track_ttl_seconds", 0.5, 60.0)
        clamp("dedup_window_seconds", 0.0, 3600.0)
        clamp("port", 1, 65535)
        clamp("relay_port", 1, 65535)
        clamp("retention_days", 0, 3650)
        clamp("max_upload_mb", 1, 64)
        clamp("person_max_side", 160, 1280)
        clamp("person_crop_upscale", 0.25, 4.0)
        clamp("discovery_rate_limit_seconds", 0.0, 300.0)
        if self.processing_device not in ("auto", "cpu", "gpu"):
            self.processing_device = "auto"
        if self.log_level not in ("debug", "info", "warning", "error"):
            self.log_level = "info"
        if self.notify_method not in ("log", "smtp"):
            self.notify_method = "log"
        if self.fast_sample_ms > self.slow_sample_ms:
            self.fast_sample_ms = self.slow_sample_ms


SETTINGS_FILE_NAME = "runtime_settings.json"


class SettingsManager:
    """Thread-safe holder for ``RuntimeSettings`` with JSON persistence.

    Every read returns a fresh dataclass copy; updates are applied under a
    lock and validated, then written to disk.  The writer and the reader use
    the *same* shape (pitfall 5): the JSON is a flat dict of the dataclass
    fields.
    """

    def __init__(self, paths: Paths) -> None:
        self._paths = paths
        self._lock = threading.RLock()
        self._file = paths.data_dir / SETTINGS_FILE_NAME
        self._settings = RuntimeSettings()
        self._listeners: list = []
        self._load()

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        try:
            if self._file.exists():
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._settings = RuntimeSettings.from_dict(raw)
        except Exception:  # corrupt file - fall back to defaults, keep going
            self._settings = RuntimeSettings()

    def _persist(self) -> None:
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._settings.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._file)

    # -- access --------------------------------------------------------
    def get(self) -> RuntimeSettings:
        with self._lock:
            # copy so callers can hold it without seeing later mutation
            return RuntimeSettings.from_dict(asdict(self._settings))

    def get_dict(self) -> Dict[str, Any]:
        with self._lock:
            return asdict(self._settings)

    def update(self, patch: Dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            merged = asdict(self._settings)
            merged.update({k: v for k, v in (patch or {}).items() if v is not None})
            new = RuntimeSettings.from_dict(merged)
            old = self._settings
            self._settings = new
            self._persist()
            changed = {k: getattr(new, k) for k in merged if getattr(old, k) != getattr(new, k)}
        for cb in list(self._listeners):
            try:
                cb(new, changed)
            except Exception:
                pass
        return new

    def on_change(self, cb) -> None:
        self._listeners.append(cb)
