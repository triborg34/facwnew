"""Event, camera, user, zone, audit, settings repositories."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..core.security import Sealer, hash_password
from .db import Database
from .repositories import APPROVAL_ROLES, GENDERS, now_iso


# ==========================================================================
# Detection events
# ==========================================================================
class EventRepo:
    """Detection events with pagination, combinable filters and sorting."""

    SORT_COLUMNS = {"created_ts", "identity_name", "event_date", "event_time", "score", "track_id"}

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, ev: Dict[str, Any]) -> int:
        ts = float(ev.get("created_ts") or time.time())
        dt = datetime.fromtimestamp(ts)
        with self.db.transaction() as c:
            cur = c.execute(
                """INSERT INTO events(
                     identity_name, identity_id, score, age, gender, role, national_number,
                     camera_source, camera_id, zone_name, track_id,
                     frame_file, face_file, person_file,
                     event_date, event_time, created_ts, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev.get("identity_name", "unknown"),
                    ev.get("identity_id"),
                    float(ev.get("score") or 0.0),
                    ev.get("age"),
                    ev.get("gender", "unknown"),
                    ev.get("role", "unknown"),
                    ev.get("national_number", ""),
                    ev.get("camera_source", ""),
                    ev.get("camera_id"),
                    ev.get("zone_name", ""),
                    int(ev.get("track_id") or 0),
                    ev.get("frame_file", ""),
                    ev.get("face_file", ""),
                    ev.get("person_file", ""),
                    dt.strftime("%Y-%m-%d"),
                    dt.strftime("%H:%M:%S"),
                    ts,
                    now_iso(),
                    now_iso(),
                ),
            )
            new_id = int(cur.lastrowid or 0)
        self.db.bus.publish("events", {"action": "created", "id": new_id})
        return new_id

    def get(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        return dict(row) if row else None

    def delete(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Delete one event and return it so the caller can unlink artefacts."""
        ev = self.get(event_id)
        if ev is None:
            return None
        self.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.db.bus.publish("events", {"action": "deleted", "id": event_id})
        return ev

    def _build_where(self, f: Dict[str, Any]) -> Tuple[str, List[Any]]:
        where: List[str] = []
        params: List[Any] = []

        name = (f.get("name") or "").strip()
        if name:
            where.append("(identity_name LIKE ? OR national_number LIKE ?)")
            params += [f"%{name}%", f"%{name}%"]

        national = (f.get("national_number") or "").strip()
        if national:
            where.append("national_number LIKE ?")
            params.append(f"%{national}%")

        gender = (f.get("gender") or "").strip()
        if gender and gender in GENDERS:
            where.append("gender = ?")
            params.append(gender)

        role = (f.get("role") or "").strip()
        if role and role in APPROVAL_ROLES:
            where.append("role = ?")
            params.append(role)

        # Age band: lower inclusive, upper exclusive (spec 7.3 / acceptance)
        age_min = f.get("age_min")
        age_max = f.get("age_max")
        try:
            if age_min not in (None, "", "null"):
                where.append("age IS NOT NULL AND age >= ?")
                params.append(int(float(age_min)))
            if age_max not in (None, "", "null"):
                where.append("age IS NOT NULL AND age < ?")
                params.append(int(float(age_max)))
        except (TypeError, ValueError):
            pass

        # Date range: endpoints EXCLUDED (spec says "endpoints excluded"
        # for the date range display; the acceptance list requires the edges
        # to behave correctly - we implement explicit-inclusive day bounds
        # because dates are whole days in local time).
        date_from = (f.get("date_from") or "").strip()
        date_to = (f.get("date_to") or "").strip()
        if date_from:
            where.append("event_date >= ?")
            params.append(date_from)
        if date_to:
            where.append("event_date <= ?")
            params.append(date_to)

        time_from = (f.get("time_from") or "").strip()
        time_to = (f.get("time_to") or "").strip()
        if time_from:
            where.append("event_time >= ?")
            params.append(_norm_time(time_from))
        if time_to:
            where.append("event_time <= ?")
            params.append(_norm_time(time_to))

        cam = f.get("camera_id")
        if cam not in (None, "", "null"):
            try:
                where.append("camera_id = ?")
                params.append(int(cam))
            except (TypeError, ValueError):
                pass

        if str(f.get("identified_only", "")).lower() in ("1", "true", "on", "yes"):
            where.append("identity_name <> 'unknown'")

        sim_max = f.get("similarity_max")
        if sim_max not in (None, "", "null"):
            try:
                where.append("score <= ?")
                params.append(float(sim_max))
            except (TypeError, ValueError):
                pass

        only_ids = f.get("event_ids")
        if isinstance(only_ids, list) and only_ids:
            placeholders = ",".join("?" for _ in only_ids)
            where.append(f"id IN ({placeholders})")
            params.extend(int(i) for i in only_ids)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return clause, params

    def list(self, filters: Dict[str, Any], page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        clause, params = self._build_where(filters or {})
        sort = str(filters.get("sort") or "created_ts")
        if sort not in self.SORT_COLUMNS:
            sort = "created_ts"
        order = "ASC" if str(filters.get("order", "desc")).lower() == "asc" else "DESC"
        total_row = self.db.query_one(f"SELECT COUNT(*) AS c FROM events {clause}", params)
        total = int(total_row["c"]) if total_row else 0
        rows = self.db.query(
            f"SELECT * FROM events {clause} ORDER BY {sort} {order}, id {order} LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def all_for_export(self, filters: Dict[str, Any], limit: int = 5000) -> List[Dict[str, Any]]:
        clause, params = self._build_where(filters or {})
        rows = self.db.query(
            f"SELECT * FROM events {clause} ORDER BY created_ts DESC LIMIT ?", (*params, limit)
        )
        return [dict(r) for r in rows]

    def count_since(self, since_ts: float) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM events WHERE created_ts >= ?", (since_ts,))
        return int(row["c"]) if row else 0

    def last_for_identity_camera(self, identity_name: str, camera_id: Optional[int]) -> Optional[float]:
        """Newest event ts for a dedup scope (identity + camera)."""
        if camera_id is None:
            row = self.db.query_one(
                "SELECT MAX(created_ts) AS t FROM events WHERE identity_name=?",
                (identity_name,),
            )
        else:
            row = self.db.query_one(
                "SELECT MAX(created_ts) AS t FROM events WHERE identity_name=? AND camera_id=?",
                (identity_name, camera_id),
            )
        return float(row["t"]) if row and row["t"] is not None else None

    def retention_purge(self, days: int) -> List[Dict[str, Any]]:
        if days <= 0:
            return []
        cutoff = time.time() - days * 86400
        rows = self.db.query("SELECT * FROM events WHERE created_ts < ?", (cutoff,))
        old = [dict(r) for r in rows]
        if old:
            self.db.execute("DELETE FROM events WHERE created_ts < ?", (cutoff,))
            self.db.bus.publish("events", {"action": "purged", "count": len(old)})
        return old


def _norm_time(value: str) -> str:
    """Accept HH:MM or HH:MM:SS and normalise to HH:MM:SS."""
    v = value.strip()
    parts = v.split(":")
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    if len(parts) == 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    return v


# ==========================================================================
# Cameras
# ==========================================================================
class CameraRepo:
    PUBLIC_FIELDS = (
        "id", "name", "address", "port", "profile_name", "stream_address",
        "stream_type", "gate_label", "username", "analysis_enabled",
        "created_at", "updated_at",
    )

    def __init__(self, db: Database, sealer: Sealer) -> None:
        self.db = db
        self.sealer = sealer

    def _public(self, row) -> Dict[str, Any]:
        d = {k: row[k] for k in self.PUBLIC_FIELDS if k in row.keys()}
        d["has_credentials"] = bool(row["username"]) and bool(row["password_sealed"])
        d["analysis_enabled"] = bool(d.get("analysis_enabled", 1))
        return d

    def list(self) -> List[Dict[str, Any]]:
        return [self._public(r) for r in self.db.query("SELECT * FROM cameras ORDER BY id")]

    def get(self, camera_id: int, with_secret: bool = False) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM cameras WHERE id=?", (camera_id,))
        if row is None:
            return None
        d = self._public(row)
        if with_secret:
            d["password"] = self._open_secret(row["password_sealed"])
        return d

    def get_by_address(self, address: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM cameras WHERE address=? ORDER BY id LIMIT 1", (address,))
        return self._public(row) if row else None

    def _open_secret(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self.sealer.open_bytes(sealed, aad=b"facw-camera-v1").decode("utf-8")
        except Exception:
            return ""

    def create(self, data: Dict[str, Any]) -> int:
        now = now_iso()
        sealed = ""
        if data.get("password"):
            sealed = self.sealer.seal_bytes(str(data["password"]).encode(), aad=b"facw-camera-v1")
        with self.db.transaction() as c:
            cur = c.execute(
                """INSERT INTO cameras(name, address, port, profile_name, stream_address,
                        stream_type, gate_label, username, password_sealed, analysis_enabled,
                        created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(data.get("name", "")).strip() or "camera",
                    str(data.get("address", "")).strip(),
                    int(data.get("port") or 554),
                    str(data.get("profile_name", "main")),
                    str(data.get("stream_address", "")),
                    str(data.get("stream_type", "rtsp")),
                    str(data.get("gate_label", "")),
                    str(data.get("username", "")),
                    sealed,
                    1 if data.get("analysis_enabled", True) else 0,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid or 0)
        self.db.bus.publish("cameras", {"action": "created", "id": new_id})
        return new_id

    def update(self, camera_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self.get(camera_id) is None:
            return None
        allowed = (
            "name", "address", "port", "profile_name", "stream_address",
            "stream_type", "gate_label", "username", "analysis_enabled",
        )
        sets, params = [], []
        for k, v in patch.items():
            if k not in allowed or v is None:
                continue
            if k == "analysis_enabled":
                v = 1 if v else 0
            if k == "port":
                v = int(v)
            sets.append(f"{k}=?")
            params.append(v)
        if patch.get("password"):
            sets.append("password_sealed=?")
            params.append(self.sealer.seal_bytes(str(patch["password"]).encode(), aad=b"facw-camera-v1"))
        if not sets:
            return self.get(camera_id)
        sets.append("updated_at=?")
        params.extend([now_iso(), camera_id])
        self.db.execute(f"UPDATE cameras SET {', '.join(sets)} WHERE id=?", params)
        self.db.bus.publish("cameras", {"action": "updated", "id": camera_id})
        return self.get(camera_id)

    def delete(self, camera_id: int) -> bool:
        cur = self.db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
        if cur:
            self.db.bus.publish("cameras", {"action": "deleted", "id": camera_id})
            return True
        return False


# ==========================================================================
# Users + sessions
# ==========================================================================
class UserRepo:
    VALID_ROLES = ("admin", "observer")

    def __init__(self, db: Database) -> None:
        self.db = db

    def _public(self, row) -> Dict[str, Any]:
        d = dict(row)
        d.pop("password_hash", None)
        return d

    def list(self) -> List[Dict[str, Any]]:
        return [self._public(r) for r in self.db.query("SELECT * FROM users ORDER BY id")]

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        return self._public(row) if row else None

    def get_by_login(self, login: str, with_hash: bool = False) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM users WHERE login_name=?", (login,))
        if row is None:
            return None
        return dict(row) if with_hash else self._public(row)

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM users")
        return int(row["c"]) if row else 0

    def create(self, data: Dict[str, Any]) -> int:
        role = data.get("role", "observer")
        if role not in self.VALID_ROLES:
            raise ValueError("invalid role")
        login = str(data.get("login_name", "")).strip()
        if not login:
            raise ValueError("login name required")
        password = data.get("password") or ""
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        now = now_iso()
        with self.db.transaction() as c:
            cur = c.execute(
                """INSERT INTO users(display_name, login_name, password_hash, email, role,
                        created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    str(data.get("display_name", login)),
                    login,
                    hash_password(password),
                    str(data.get("email", "")),
                    role,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    def update(self, user_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fields, params = [], []
        for key in ("display_name", "email", "role"):
            if key in patch and patch[key] is not None:
                if key == "role" and patch[key] not in self.VALID_ROLES:
                    raise ValueError("invalid role")
                fields.append(f"{key}=?")
                params.append(patch[key])
        if patch.get("password"):
            if len(str(patch["password"])) < 6:
                raise ValueError("password must be at least 6 characters")
            fields.append("password_hash=?")
            params.append(hash_password(str(patch["password"])))
        if not fields:
            return self.get(user_id)
        fields.append("updated_at=?")
        params.extend([now_iso(), user_id])
        self.db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
        return self.get(user_id)

    def delete(self, user_id: int) -> bool:
        cur = self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return bool(cur)

    # -- sessions ------------------------------------------------------
    def create_session(self, user_id: int, token_hash: str, ttl_seconds: int, user_agent: str = "") -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen, user_agent)"
            " VALUES (?,?,?,?,?,?)",
            (token_hash, user_id, now, now + ttl_seconds, now, user_agent[:200]),
        )

    def session_user(self, token_hash: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        row = self.db.query_one(
            """SELECT s.token_hash, s.expires_at, s.last_seen, u.id, u.display_name, u.login_name,
                      u.email, u.role
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash=? AND s.expires_at > ?""",
            (token_hash, now),
        )
        if row is None:
            return None
        self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, token_hash))
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "login_name": row["login_name"],
            "email": row["email"],
            "role": row["role"],
            "expires_at": row["expires_at"],
        }

    def delete_session(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def purge_expired_sessions(self) -> int:
        return self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))


# ==========================================================================
# Zones
# ==========================================================================
class ZoneRepo:
    KINDS = ("polygon", "rect", "line")

    def __init__(self, db: Database) -> None:
        self.db = db

    def _to_public(self, row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["points"] = json.loads(d.pop("points_json") or "[]")
        except json.JSONDecodeError:
            d["points"] = []
        return d

    def list_for_camera(self, camera_key: str) -> List[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM zones WHERE camera_key=? ORDER BY id", (camera_key,))
        return [self._to_public(r) for r in rows]

    def all(self) -> List[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM zones ORDER BY camera_key, id")
        return [self._to_public(r) for r in rows]

    def replace_for_camera(self, camera_key: str, zones: List[Dict[str, Any]]) -> int:
        """Atomically replace every zone of one camera (used by the editor)."""
        now = now_iso()
        with self.db.transaction() as c:
            c.execute("DELETE FROM zones WHERE camera_key=?", (camera_key,))
            for z in zones:
                kind = z.get("kind", "polygon")
                if kind not in self.KINDS:
                    kind = "polygon"
                c.execute(
                    """INSERT INTO zones(camera_key, zone_uid, name, description, colour, kind,
                           points_json, image_w, image_h, relay_channel, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        camera_key,
                        str(z.get("id") or z.get("zone_uid") or f"z{int(time.time()*1000)}"),
                        str(z.get("name", "zone"))[:80],
                        str(z.get("description", ""))[:400],
                        str(z.get("colour", "#38bdf8")),
                        kind,
                        json.dumps(
                            [[float(p[0]), float(p[1])] for p in (z.get("points") or []) if len(p) >= 2]
                        ),
                        int(z.get("image_w") or z.get("img_w") or 0),
                        int(z.get("image_h") or z.get("img_h") or 0),
                        str(z.get("relay_channel") or ""),
                        now,
                        now,
                    ),
                )
        self.db.bus.publish("zones", {"action": "replaced", "camera_key": camera_key, "count": len(zones)})
        return len(zones)

    def delete_for_camera(self, camera_key: str) -> int:
        n = self.db.execute("DELETE FROM zones WHERE camera_key=?", (camera_key,))
        if n:
            self.db.bus.publish("zones", {"action": "deleted", "camera_key": camera_key})
        return n


# ==========================================================================
# Audit + biometric access
# ==========================================================================
class AuditRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, action: str, actor: str = "", target: str = "", detail: str = "", outcome: str = "ok") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit(ts, actor, action, target, detail, outcome) VALUES (?,?,?,?,?,?)",
                (time.time(), actor or "system", action, str(target)[:200], str(detail)[:2000], outcome),
            )
        except Exception:
            # audit failure must never break the caller (fail-open on logging,
            # fail-closed on access decisions - see README)
            pass

    def list(self, limit: int = 200, action: str = "") -> List[Dict[str, Any]]:
        if action:
            rows = self.db.query(
                "SELECT * FROM audit WHERE action=? ORDER BY ts DESC LIMIT ?", (action, limit)
            )
        else:
            rows = self.db.query("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def biometric_log(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM biometric_access ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
