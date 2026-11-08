"""Track lifecycle (stage 9) and temporal voting (stage 8).

Vote resolution is deliberately *not* a plain majority count: the winner is
the identity with the largest total similarity, and it must also hold a strict
majority of samples.  Otherwise the result is unknown.

Kept free of OpenCV so it is directly unit-testable.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .matching import MatchResult


@dataclass
class Vote:
    identity: str
    score: float
    age: Optional[int]
    gender: str
    role: str
    national_number: str
    identity_id: Optional[int] = None
    description: str = ""
    at: float = field(default_factory=time.time)


def resolve_votes(votes: List[Vote], required: int) -> Optional[Dict[str, Any]]:
    """Resolve a vote list into a commit decision.

    Returns ``None`` while undecided, ``{"name": "unknown"}`` when the votes
    are conclusive but inconclusive for *this* person, or
    ``{"name": <identity>, "score": <avg>, ...}`` on a commit.
    """
    if len(votes) < max(1, required):
        return None
    totals: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for v in votes:
        totals[v.identity] = totals.get(v.identity, 0.0) + float(v.score)
        counts[v.identity] = counts.get(v.identity, 0) + 1
    winner = max(totals.items(), key=lambda kv: kv[1])[0]
    total_samples = len(votes)
    if winner == "unknown":
        return {"name": "unknown", "score": 0.0}
    # strict majority of *all* samples, not only of the identified ones
    if counts[winner] * 2 <= total_samples:
        return {"name": "unknown", "score": 0.0}
    winning = [v for v in votes if v.identity == winner]
    avg = sum(v.score for v in winning) / len(winning)
    ref = winning[-1]
    return {
        "name": winner,
        "score": avg,
        "identity_id": ref.identity_id,
        "age": ref.age,
        "gender": ref.gender,
        "role": ref.role,
        "national_number": ref.national_number,
        "description": ref.description,
        "votes": len(winning),
        "samples": total_samples,
    }


class Track:
    """Per-appearance state.  Everything per-track lives here so the sweep can
    drop it wholesale (stage 9)."""

    __slots__ = (
        "track_id", "camera_key", "bbox", "score", "first_seen", "last_seen",
        "votes", "committed", "identity", "face_box", "person_crop", "last_sample_at",
        "sample_count", "decided_notified", "emitted", "zone_name", "vector",
        "updated_at",
    )

    def __init__(self, track_id: int, camera_key: str, bbox, score: float) -> None:
        now = time.time()
        self.track_id = track_id
        self.camera_key = camera_key
        self.bbox = bbox                      # (x1, y1, x2, y2)
        self.score = score
        self.first_seen = now
        self.last_seen = now
        self.votes: List[Vote] = []
        self.committed = False
        self.identity: Optional[Dict[str, Any]] = None
        self.face_box = None
        self.person_crop = None
        self.last_sample_at = 0.0
        self.sample_count = 0
        self.decided_notified = False
        self.emitted = False
        self.zone_name = ""
        self.vector = None
        #: bumped whenever the box or state changes; the console's live view
        #: polls with the value it already holds and only re-renders on change.
        self.updated_at = now

    @property
    def state(self) -> str:
        """What the console shows: analysing, identified, or unknown."""
        if self.committed and self.identity:
            nm = self.identity.get("name", "unknown")
            return "identified" if nm != "unknown" else "unknown"
        return "analysing"


class TrackManager:
    """Assigns stable track ids and sweeps stale tracks."""

    def __init__(self, ttl_seconds: float = 3.0) -> None:
        self._lock = threading.Lock()
        self._tracks: Dict[str, Dict[int, Track]] = {}
        self._next_id = 1
        self.ttl = ttl_seconds

    def get(self, camera_key: str, track_id: int) -> Optional[Track]:
        with self._lock:
            return self._tracks.get(camera_key, {}).get(track_id)

    def put(self, track: Track) -> None:
        with self._lock:
            self._tracks.setdefault(track.camera_key, {})[track.track_id] = track

    def next_id(self) -> int:
        with self._lock:
            tid = self._next_id
            self._next_id += 1
            return tid

    def for_camera(self, camera_key: str) -> List[Track]:
        with self._lock:
            return list(self._tracks.get(camera_key, {}).values())

    def drop(self, camera_key: str, track_id: int) -> None:
        with self._lock:
            self._tracks.get(camera_key, {}).pop(track_id, None)

    def clear_camera(self, camera_key: str) -> None:
        with self._lock:
            self._tracks.pop(camera_key, None)

    def sweep(self, now: Optional[float] = None) -> List[Track]:
        """Drop tracks unseen for longer than the TTL and return them."""
        now = now or time.time()
        dropped: List[Track] = []
        with self._lock:
            for cam, tracks in self._tracks.items():
                for tid in [t for t, tr in tracks.items() if now - tr.last_seen > self.ttl]:
                    dropped.append(tracks.pop(tid))
        return dropped

    def all_tracks(self) -> List[Track]:
        with self._lock:
            return [t for tracks in self._tracks.values() for t in tracks.values()]

    @property
    def count(self) -> int:
        with self._lock:
            return sum(len(t) for t in self._tracks.values())
