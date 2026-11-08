"""Identity matching index (stage 7 of the pipeline).

* The enrolled set is kept in memory as one matrix of unit vectors, one row
  per reference photo, plus per-row metadata.  Name -> row ids is kept as an
  index so top-K aggregation per identity is cheap.
* Matching is ONE vectorised pass for the whole frame's queries - never a
  Python loop over identities per frame (spec 9.1).
* Per identity we take the best K rows and average them, so a single poor
  photo cannot drag an identity down.
* Below the match threshold -> unknown.  Gap to the runner-up below the
  ambiguity margin -> unknown (never a coin flip).

The index is rebuilt whenever enrolment data changes, so a new person is
matchable immediately (spec 7 / no manual refresh).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..core.logging_setup import get_logger

log = get_logger("pipeline.matching")

TOP_K = 3


@dataclass
class MatchResult:
    name: str                       # identity name, or "unknown"
    score: float                    # 0..1 similarity of the winner
    identity_id: Optional[int] = None
    role: str = "unknown"
    age: Optional[int] = None
    gender: str = "unknown"
    national_number: str = ""
    description: str = ""
    ambiguous: bool = False         # True when the margin test rejected a winner
    runner_up: str = ""             # second-best identity name (diagnostics)
    runner_up_score: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(float(self.score), 4),
            "identity_id": self.identity_id,
            "role": self.role,
            "age": self.age,
            "gender": self.gender,
            "national_number": self.national_number,
            "ambiguous": self.ambiguous,
            "runner_up": self.runner_up,
            "runner_up_score": round(float(self.runner_up_score), 4),
        }


UNKNOWN = MatchResult(name="unknown", score=0.0)


def normalise(vec: np.ndarray) -> Optional[np.ndarray]:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n <= 0:
        return None
    return v / n


def top_k_mean(sims: np.ndarray, k: int = TOP_K) -> float:
    """Average of the best ``k`` similarities (fewer rows -> average of what
    exists).  Isolated so it can be unit-tested directly."""
    if sims.size == 0:
        return 0.0
    k = max(1, min(k, sims.size))
    if k == sims.size:
        return float(sims.mean())
    part = np.partition(sims, -k)[-k:]
    return float(part.mean())


class MatchIndex:
    """Thread-safe, atomically-swappable in-memory identity index."""

    def __init__(self, dim: int = 128) -> None:
        self._lock = threading.RLock()
        self._dim = dim
        self._matrix: Optional[np.ndarray] = None          # (R, dim) unit vectors
        self._names: List[str] = []                        # row -> identity name
        self._meta: List[Dict[str, Any]] = []              # row -> metadata
        self._name_rows: Dict[str, List[int]] = {}         # identity name -> rows
        self._name_meta: Dict[str, Dict[str, Any]] = {}    # identity -> metadata
        self.version = 0
        self.built_at = 0.0
        self.row_count = 0

    # -- build ---------------------------------------------------------
    @staticmethod
    def _key(identity_id: Any, name: str) -> str:
        """Stable grouping key: the identity id when known, else the name."""
        if identity_id is None or identity_id == "":
            return f"name:{name}"
        return f"id:{identity_id}"

    def rebuild(self, rows: List[Dict[str, Any]]) -> None:
        vecs: List[np.ndarray] = []
        names: List[str] = []
        meta: List[Dict[str, Any]] = []
        for r in rows:
            v = normalise(r.get("vector"))
            if v is None or v.size != self._dim:
                continue  # skip malformed / wrong-dim rows instead of raising
            vecs.append(v)
            names.append(str(r.get("name", "unnamed")))
            meta.append(
                {
                    "identity_id": r.get("identity_id"),
                    "name": r.get("name", "unnamed"),
                    "age": r.get("age"),
                    "gender": r.get("gender", "unknown"),
                    "role": r.get("role", "colleague"),
                    "national_number": r.get("national_number", ""),
                    "description": r.get("description", ""),
                    "ref_id": r.get("ref_id"),
                }
            )
        # Group references by *identity id*, not by display name.  Two people can
        # legitimately share a name, and duplicate registrations of the same name
        # previously produced two entries with identical scores, which made the
        # ambiguity check reject every match as "unknown".
        name_rows: Dict[str, List[int]] = {}
        name_meta: Dict[str, Dict[str, Any]] = {}
        for i, nm in enumerate(names):
            key = self._key(meta[i].get("identity_id"), nm)
            name_rows.setdefault(key, []).append(i)
            name_meta.setdefault(key, meta[i])
        matrix = np.vstack(vecs).astype(np.float32) if vecs else None

        with self._lock:  # atomic swap: readers see old or new, never partial
            self._matrix = matrix
            self._names = names
            self._meta = meta
            self._name_rows = name_rows
            self._name_meta = name_meta
            self.row_count = len(names)
            self.version += 1
            self.built_at = time.time()
        log.info(
            "match index rebuilt",
            extra={"rows": len(names), "identities": len(name_rows), "version": self.version},
        )

    # -- query ---------------------------------------------------------
    def match(self, vector: np.ndarray, threshold: float, margin: float) -> MatchResult:
        q = normalise(vector)
        if q is None or q.size != self._dim:
            return UNKNOWN
        with self._lock:
            matrix = self._matrix
            if matrix is None or matrix.shape[0] == 0:
                return UNKNOWN
            # ONE vectorised pass over every reference row.
            sims = matrix @ q
            name_rows = self._name_rows
            name_meta = self._name_meta

        scores: List[Tuple[str, float]] = []
        for key, idx in name_rows.items():
            scores.append((key, top_k_mean(sims[np.asarray(idx, dtype=np.int64)])))
        # rank identities by aggregated score
        scores.sort(key=lambda t: t[1], reverse=True)
        best_name, best_score = scores[0]
        second_score = scores[1][1] if len(scores) > 1 else -1.0
        second_name = scores[1][0] if len(scores) > 1 else ""

        best_display = str(name_meta.get(best_name, {}).get("name", best_name))
        second_display = str(name_meta.get(second_name, {}).get("name", second_name))

        if best_score < threshold:
            return MatchResult(
                name="unknown",
                score=float(best_score),
                runner_up=second_display,
                runner_up_score=float(max(0.0, second_score)),
            )
        if second_score >= 0 and (best_score - second_score) < margin:
            # top two are too close - refuse to choose
            return MatchResult(
                name="unknown",
                score=float(best_score),
                ambiguous=True,
                runner_up=second_display,
                runner_up_score=float(second_score),
            )
        m = name_meta.get(best_name, {})
        return MatchResult(
            name=str(m.get("name", best_name)),
            score=float(best_score),
            identity_id=m.get("identity_id"),
            role=m.get("role", "colleague"),
            age=m.get("age"),
            gender=m.get("gender", "unknown"),
            national_number=m.get("national_number", ""),
            description=m.get("description", ""),
            runner_up=second_display,
            runner_up_score=float(max(0.0, second_score)),
        )

    # -- introspection -------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rows": self.row_count,
                "identities": len(self._name_rows),
                "version": self.version,
                "built_at": self.built_at,
                "dim": self._dim,
            }

    def list_identities(self) -> List[str]:
        with self._lock:
            return list(self._name_rows.keys())

    def meta_for(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._name_meta[name]) if name in self._name_meta else None
