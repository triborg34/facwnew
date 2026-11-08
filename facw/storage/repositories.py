"""Identity + face-reference repositories.

Design rule (spec 6.2 / pitfall 24): one row per reference photo in
``face_refs``.  Vectors are sealed (AES-GCM) at rest and every read is
recorded in ``biometric_access``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from ..core.security import Sealer
from .db import Database

APPROVAL_ROLES = ("approve", "deny", "visitor", "colleague")
GENDERS = ("male", "female", "unknown")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class IdentityRepo:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self.db = db
        self.sealer = sealer

    # -- reads ---------------------------------------------------------
    def get(self, identity_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM identities WHERE id=?", (identity_id,))
        if row is None:
            return None
        d = dict(row)
        d["ref_count"] = self.count_refs(identity_id)
        return d

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM identities WHERE name=? ORDER BY id LIMIT 1", (name,)
        )
        return dict(row) if row else None

    def list(
        self,
        page: int = 1,
        page_size: int = 24,
        search: str = "",
        role: str = "",
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        where, params = [], []
        if search:
            like = f"%{search.strip()}%"
            where.append(
                "(name LIKE ? OR family_name LIKE ? OR national_number LIKE ? OR description LIKE ?)"
            )
            params += [like, like, like, like]
        if role and role in APPROVAL_ROLES:
            where.append("role = ?")
            params.append(role)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) AS c FROM identities {clause}", params)
        rows = self.db.query(
            f"""SELECT i.*, (SELECT COUNT(*) FROM face_refs r WHERE r.identity_id=i.id) AS ref_count
                FROM identities i {clause}
                ORDER BY i.id DESC LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [dict(r) for r in rows],
            "total": int(total["c"]) if total else 0,
            "page": page,
            "page_size": page_size,
        }

    def all_with_refs(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            """SELECT i.*, (SELECT COUNT(*) FROM face_refs r WHERE r.identity_id=i.id) AS ref_count
               FROM identities i ORDER BY i.id"""
        )
        return [dict(r) for r in rows]

    # -- writes --------------------------------------------------------
    def create(self, data: Dict[str, Any]) -> int:
        role = data.get("role", "colleague")
        if role not in APPROVAL_ROLES:
            raise ValueError(f"invalid approval role: {role}")
        gender = data.get("gender", "unknown")
        if gender not in GENDERS:
            raise ValueError(f"invalid gender: {gender}")
        age = data.get("age")
        age = int(age) if age not in (None, "", "null") else None
        now = now_iso()
        with self.db.transaction() as c:
            cur = c.execute(
                """INSERT INTO identities(
                     name, family_name, age, gender, role, national_number, description,
                     registered_by, avatar_file, reference_file, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(data.get("name", "")).strip() or "unnamed",
                    str(data.get("family_name", "")).strip(),
                    age,
                    gender,
                    role,
                    str(data.get("national_number", "")).strip(),
                    str(data.get("description", "")).strip(),
                    str(data.get("registered_by", "")).strip(),
                    str(data.get("avatar_file", "")),
                    str(data.get("reference_file", "")),
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid or 0)
        self.db.bus.publish("identities", {"action": "created", "id": new_id})
        return new_id

    def update(self, identity_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get(identity_id)
        if existing is None:
            return None
        allowed = {
            "name", "family_name", "age", "gender", "role",
            "national_number", "description", "registered_by",
            "avatar_file", "reference_file",
        }
        fields, params = [], []
        for key, val in patch.items():
            if key not in allowed:
                continue
            if key == "role" and val not in APPROVAL_ROLES:
                raise ValueError(f"invalid approval role: {val}")
            if key == "gender" and val not in GENDERS:
                raise ValueError(f"invalid gender: {val}")
            if key == "age":
                val = int(val) if val not in (None, "", "null") else None
            fields.append(f"{key}=?")
            params.append(val)
        if not fields:
            return existing
        fields.append("updated_at=?")
        params.extend([now_iso(), identity_id])
        self.db.execute(f"UPDATE identities SET {', '.join(fields)} WHERE id=?", params)
        self.db.bus.publish("identities", {"action": "updated", "id": identity_id})
        return self.get(identity_id)

    def delete(self, identity_id: int) -> bool:
        with self.db.transaction() as c:
            cur = c.execute("DELETE FROM identities WHERE id=?", (identity_id,))
            c.execute("DELETE FROM face_refs WHERE identity_id=?", (identity_id,))
            deleted = (cur.rowcount or 0) > 0
        if deleted:
            self.db.bus.publish("identities", {"action": "deleted", "id": identity_id})
        return deleted

    # -- references ----------------------------------------------------
    def count_refs(self, identity_id: int) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM face_refs WHERE identity_id=?", (identity_id,))
        return int(row["c"]) if row else 0

    def list_refs(self, identity_id: int) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, identity_id, image_file, face_width, face_score, created_at "
            "FROM face_refs WHERE identity_id=? ORDER BY id",
            (identity_id,),
        )
        return [dict(r) for r in rows]

    def add_ref(
        self,
        identity_id: int,
        vector: np.ndarray,
        image_file: str,
        face_width: int = 0,
        face_score: float = 0.0,
        actor: str = "",
    ) -> int:
        """Append one reference photo row (never replace - spec 5.1)."""
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        sealed = self.sealer.seal_bytes(
            json.dumps([float(x) for x in vec]).encode("utf-8"), aad=b"facw-ref-v1"
        )
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT INTO face_refs(identity_id, vector_sealed, image_file, face_width, face_score, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (identity_id, sealed, image_file, int(face_width), float(face_score), now_iso()),
            )
            ref_id = int(cur.lastrowid or 0)
            # first reference also becomes the identity's representative image
            c.execute(
                """UPDATE identities SET reference_file=CASE WHEN reference_file='' THEN ? ELSE reference_file END,
                          avatar_file=CASE WHEN avatar_file='' THEN ? ELSE avatar_file END,
                          updated_at=? WHERE id=?""",
                (image_file, image_file, now_iso(), identity_id),
            )
        self._log_biometric("reference_added", identity_id, f"ref={ref_id}", actor)
        self.db.bus.publish("identities", {"action": "ref_added", "id": identity_id})
        return ref_id

    def remove_ref(self, identity_id: int, index: int, actor: str = "") -> Optional[str]:
        """Remove the reference at 1-based ``index``; returns its image file."""
        refs = self.list_refs(identity_id)
        if index < 1 or index > len(refs):
            return None
        target = refs[index - 1]
        self.db.execute("DELETE FROM face_refs WHERE id=?", (target["id"],))
        self._log_biometric("reference_removed", identity_id, f"idx={index}", actor)
        self.db.bus.publish("identities", {"action": "ref_removed", "id": identity_id})
        return str(target["image_file"])

    def delete_ref_by_file(self, identity_id: int, image_file: str, actor: str = "") -> bool:
        cur = self.db.execute(
            "DELETE FROM face_refs WHERE identity_id=? AND image_file=?", (identity_id, image_file)
        )
        if cur:
            self._log_biometric("reference_removed", identity_id, f"file={image_file}", actor)
            self.db.bus.publish("identities", {"action": "ref_removed", "id": identity_id})
            return True
        return False

    def load_vectors(self, actor: str = "pipeline") -> List[Dict[str, Any]]:
        """Read every reference vector (unsealed) with its identity metadata.

        This is the *only* read path for biometric vectors.  It records the
        access (spec 6.7) and is called once whenever the index is rebuilt.
        """
        rows = self.db.query(
            """SELECT r.id AS ref_id, r.identity_id, r.vector_sealed, r.image_file,
                      i.name, i.family_name, i.age, i.gender, i.role, i.national_number,
                      i.description
               FROM face_refs r JOIN identities i ON i.id = r.identity_id
               ORDER BY i.id, r.id"""
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                raw = self.sealer.open_bytes(row["vector_sealed"], aad=b"facw-ref-v1")
                vec = np.asarray(json.loads(raw.decode("utf-8")), dtype=np.float32)
            except Exception:
                # malformed record must not kill the index rebuild (spec 9.2)
                continue
            out.append(
                {
                    "ref_id": row["ref_id"],
                    "identity_id": row["identity_id"],
                    "vector": vec,
                    "image_file": row["image_file"],
                    "name": row["name"],
                    "family_name": row["family_name"],
                    "age": row["age"],
                    "gender": row["gender"],
                    "role": row["role"],
                    "national_number": row["national_number"],
                    "description": row["description"],
                }
            )
        if out:
            self._log_biometric("index_read", None, f"rows={len(out)}", actor)
        return out

    def _log_biometric(self, action: str, identity_id: Optional[int], detail: str, actor: str) -> None:
        try:
            self.db.execute(
                "INSERT INTO biometric_access(ts, actor, action, identity_id, detail) VALUES (?,?,?,?,?)",
                (time.time(), actor or "system", action, identity_id, detail),
            )
        except Exception:
            pass
