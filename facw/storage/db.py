"""Embedded data store: SQLite (WAL mode) in a single relocatable file.

Why SQLite: zero-install, file-based, transactional, supports the indexes,
pagination and filtered queries the reports screen needs, and ships with
Python so the on-premise host needs nothing extra (spec 2.1b / 6.7).

Concurrency model: one ``Database`` object owns a small connection pool.
Connections are *not* shared across threads (sqlite3 default); every
repository call checks out a connection for its own duration.

Change notifications: a ``ChangeBus`` fans out ``(topic, payload)`` messages
to subscriber queues.  Long-poll/live clients read from those queues.
"""
from __future__ import annotations

import contextlib
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..core.logging_setup import get_logger

log = get_logger("store.db")


class ChangeBus:
    """Fan-out of change notifications to any number of subscribers."""

    def __init__(self, max_queue: int = 256) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._max = max_queue
        self._seq = 0

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: queue.Queue = queue.Queue(maxsize=self._max)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, topic: str, payload: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._seq += 1
            msg = {"topic": topic, "seq": self._seq, "ts": time.time(), "data": payload or {}}
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Slow consumer: drop its oldest message so it never blocks us.
                with contextlib.suppress(queue.Empty):
                    q.get_nowait()
                with contextlib.suppress(queue.Full):
                    q.put_nowait(msg)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Users (spec 6.5)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT NOT NULL,
    login_name    TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'observer',   -- admin | observer
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_users_login ON users(login_name);

-- Sessions: server side, revocable, expiring
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    user_agent  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sessions_exp ON sessions(expires_at);

-- Cameras (spec 6.4)
CREATE TABLE IF NOT EXISTS cameras (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    address        TEXT NOT NULL,
    port           INTEGER NOT NULL DEFAULT 554,
    profile_name   TEXT NOT NULL DEFAULT 'main',
    stream_address TEXT NOT NULL DEFAULT '',
    stream_type    TEXT NOT NULL DEFAULT 'rtsp',      -- rtsp | http | file | webcam
    gate_label     TEXT NOT NULL DEFAULT '',
    username       TEXT NOT NULL DEFAULT '',
    password_sealed TEXT NOT NULL DEFAULT '',
    analysis_enabled INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cameras_address ON cameras(address);

-- Identities (spec 6.1)
CREATE TABLE IF NOT EXISTS identities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    family_name    TEXT NOT NULL DEFAULT '',
    age            INTEGER,
    gender         TEXT NOT NULL DEFAULT 'unknown',
    role           TEXT NOT NULL DEFAULT 'colleague',  -- approve|deny|visitor|colleague
    national_number TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    registered_by  TEXT NOT NULL DEFAULT '',
    avatar_file    TEXT NOT NULL DEFAULT '',
    reference_file TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_identities_name ON identities(name);
CREATE INDEX IF NOT EXISTS ix_identities_family ON identities(family_name);
CREATE INDEX IF NOT EXISTS ix_identities_national ON identities(national_number);
CREATE INDEX IF NOT EXISTS ix_identities_role ON identities(role);

-- Face references: one row per enrolled photo (spec 6.2)
CREATE TABLE IF NOT EXISTS face_refs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    vector_sealed TEXT NOT NULL,
    image_file   TEXT NOT NULL DEFAULT '',
    face_width   INTEGER NOT NULL DEFAULT 0,
    face_score   REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_facerefs_identity ON face_refs(identity_id);

-- Detection events (spec 6.3)
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_name  TEXT NOT NULL DEFAULT 'unknown',
    identity_id    INTEGER,
    score          REAL NOT NULL DEFAULT 0,
    age            INTEGER,
    gender         TEXT NOT NULL DEFAULT 'unknown',
    role           TEXT NOT NULL DEFAULT 'unknown',
    national_number TEXT NOT NULL DEFAULT '',
    camera_source  TEXT NOT NULL DEFAULT '',
    camera_id      INTEGER,
    zone_name      TEXT NOT NULL DEFAULT '',
    track_id       INTEGER NOT NULL DEFAULT 0,
    frame_file     TEXT NOT NULL DEFAULT '',
    face_file      TEXT NOT NULL DEFAULT '',
    person_file    TEXT NOT NULL DEFAULT '',
    event_date     TEXT NOT NULL,           -- YYYY-MM-DD (local)
    event_time     TEXT NOT NULL,           -- HH:MM:SS  (local)
    created_ts     REAL NOT NULL,           -- unix seconds, for sorting/ranges
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_created_ts ON events(created_ts DESC);
CREATE INDEX IF NOT EXISTS ix_events_name ON events(identity_name);
CREATE INDEX IF NOT EXISTS ix_events_date ON events(event_date);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(event_time);
CREATE INDEX IF NOT EXISTS ix_events_track ON events(track_id);
CREATE INDEX IF NOT EXISTS ix_events_camera ON events(camera_id);
CREATE INDEX IF NOT EXISTS ix_events_role ON events(role);

-- Zones per camera, stored resolution-independently (spec 4.3)
CREATE TABLE IF NOT EXISTS zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_key  TEXT NOT NULL,
    zone_uid    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    colour      TEXT NOT NULL DEFAULT '#38bdf8',
    kind        TEXT NOT NULL DEFAULT 'polygon',   -- polygon | rect | line
    points_json TEXT NOT NULL,                     -- normalised 0..1 coords
    image_w     INTEGER NOT NULL DEFAULT 0,
    image_h     INTEGER NOT NULL DEFAULT 0,
    relay_channel TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_zones_camera ON zones(camera_key);

-- Audit log: every enrolment, deletion, settings change, relay, notification
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit(action);

-- Biometric access log (spec 6.7): every read of a face vector is recorded
CREATE TABLE IF NOT EXISTS biometric_access (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    identity_id INTEGER,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bio_ts ON biometric_access(ts DESC);
"""


class Database:
    """Owns the SQLite file and a per-thread connection set."""

    def __init__(self, path: Path, media_root: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.media_root = media_root
        self._local = threading.local()
        self._all_conns: List[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self.bus = ChangeBus()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connections ---------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        with self._conn_lock:
            self._all_conns.append(conn)
        return conn

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ROLLBACK")
            raise

    # -- lifecycle -----------------------------------------------------
    def bootstrap(self) -> None:
        """Create the schema if needed.  Idempotent."""
        c = self.conn()
        c.executescript(SCHEMA_SQL)
        c.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        log.info("database ready", extra={"path": str(self.path), "schema": SCHEMA_VERSION})

    def integrity_check(self) -> bool:
        try:
            row = self.conn().execute("PRAGMA integrity_check").fetchone()
            return bool(row and row[0] == "ok")
        except sqlite3.Error as exc:
            log.error("integrity check failed", extra={"error": str(exc)})
            return False

    def close(self) -> None:
        with self._conn_lock:
            conns, self._all_conns = self._all_conns, []
        for c in conns:
            with contextlib.suppress(sqlite3.Error):
                c.close()
        self._local = threading.local()

    # -- helpers -------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return list(self.conn().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.conn().execute(sql, tuple(params))
        return cur.lastrowid or cur.rowcount

    def backup_to(self, dest: Path) -> Path:
        """Consistent online backup (SQLite backup API)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn().backup(target)
        finally:
            target.close()
        return dest


def row_to_dict(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    return dict(row) if row is not None else {}
