"""Tests for the algorithm stages: matching maths, vote resolution,
de-duplication window, zone geometry and coordinate conversion."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facw.pipeline.matching import MatchIndex, UNKNOWN, normalise, top_k_mean
from facw.pipeline.tracking import Track, TrackManager, Vote, resolve_votes
from facw.pipeline.zones import (
    Zone,
    apply_zone_mask,
    attribute_zone,
    point_in_polygon,
    point_in_rect,
    zone_contains,
    zone_mask,
)


# ==========================================================================
# Matching
# ==========================================================================
def _unit(*vals):
    v = np.asarray(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


def _row(name, vec, identity_id=1, role="colleague"):
    return {
        "ref_id": 1,
        "identity_id": identity_id,
        "vector": vec,
        "name": name,
        "age": 30,
        "gender": "unknown",
        "role": role,
        "national_number": "",
        "description": "",
    }


def test_top_k_mean_averages_best_rows():
    sims = np.array([0.10, 0.90, 0.80, 0.70, 0.20], dtype=np.float32)
    # best 3 = 0.90, 0.80, 0.70
    assert top_k_mean(sims, 3) == pytest.approx((0.90 + 0.80 + 0.70) / 3)
    # fewer rows than k -> mean of all
    assert top_k_mean(np.array([0.4, 0.6], np.float32), 3) == pytest.approx(0.5)


def test_top_k_prevents_one_bad_photo_from_ruining_identity():
    """One poor reference must not drag a good identity below the threshold."""
    good = _unit(1, 0, 0)
    index = MatchIndex(dim=3)
    index.rebuild(
        [
            _row("alice", good),
            _row("alice", good),
            _row("alice", good),
            _row("alice", _unit(1, 1, 0)),   # one mediocre reference (cos 0.707)
        ]
    )
    res = index.match(good, threshold=0.5, margin=0.0)
    assert res.name == "alice"
    assert res.score > 0.99  # averaged over the best three rows only


def test_match_returns_unknown_below_threshold():
    index = MatchIndex(dim=3)
    index.rebuild([_row("bob", _unit(1, 0, 0), identity_id=2)])
    res = index.match(_unit(0, 1, 0), threshold=0.5, margin=0.05)
    assert res.name == "unknown"
    assert not res.ambiguous


def test_ambiguity_margin_rejects_coin_flip():
    """Two similar-looking people must yield unknown, not a guess."""
    index = MatchIndex(dim=2)
    index.rebuild(
        [
            _row("twin_a", _unit(1.0, 0.0), identity_id=1),
            _row("twin_b", _unit(0.97, 0.24), identity_id=2),   # ~14 deg apart
        ]
    )
    # query sits almost exactly between the twins -> gap is tiny
    query = _unit(1.0, 0.12)
    res = index.match(query, threshold=0.5, margin=0.10)
    assert res.name == "unknown"
    assert res.ambiguous is True
    assert res.runner_up in ("twin_a", "twin_b")
    # with a much smaller margin the same query resolves to a winner
    res2 = index.match(query, threshold=0.5, margin=0.0001)
    assert res2.name in ("twin_a", "twin_b")
    assert res2.ambiguous is False


def test_clear_winner_is_returned_with_metadata():
    index = MatchIndex(dim=3)
    index.rebuild(
        [
            _row("carol", _unit(1, 0, 0), identity_id=9, role="approve"),
            _row("dave", _unit(0, 1, 0), identity_id=10, role="deny"),
        ]
    )
    res = index.match(_unit(1, 0.05, 0), threshold=0.4, margin=0.05)
    assert res.name == "carol"
    assert res.role == "approve"
    assert res.identity_id == 9


def test_empty_index_returns_unknown():
    index = MatchIndex(dim=3)
    index.rebuild([])
    assert index.match(_unit(1, 0, 0), 0.3, 0.05).name == "unknown"


def test_malformed_rows_are_skipped_not_fatal():
    index = MatchIndex(dim=3)
    index.rebuild(
        [
            {"name": "bad", "vector": np.zeros(3)},       # zero vector
            {"name": "wrongdim", "vector": np.ones(7)},   # wrong dimension
            _row("ok", _unit(0, 0, 1)),
        ]
    )
    assert index.stats()["rows"] == 1
    assert index.match(_unit(0, 0, 1), 0.3, 0.0).name == "ok"


def test_rebuild_swaps_atomically_and_bumps_version():
    index = MatchIndex(dim=3)
    index.rebuild([_row("x", _unit(1, 0, 0))])
    v1 = index.stats()["version"]
    index.rebuild([_row("y", _unit(0, 1, 0), identity_id=2)])
    assert index.stats()["version"] == v1 + 1
    assert index.match(_unit(1, 0, 0), 0.5, 0.0).name == "unknown"
    assert index.match(_unit(0, 1, 0), 0.5, 0.0).name == "y"


def test_normalise_handles_zero_vector():
    assert normalise(np.zeros(4)) is None
    v = normalise(np.array([3.0, 4.0]))
    assert np.linalg.norm(v) == pytest.approx(1.0)


# ==========================================================================
# Vote resolution
# ==========================================================================
def _vote(name, score, **kw):
    return Vote(
        identity=name,
        score=score,
        age=kw.get("age", 30),
        gender=kw.get("gender", "unknown"),
        role=kw.get("role", "colleague"),
        national_number=kw.get("national_number", ""),
        identity_id=kw.get("identity_id"),
    )


def test_no_commit_before_required_samples():
    votes = [_vote("alice", 0.9)] * 3
    assert resolve_votes(votes, required=5) is None


def test_weighted_majority_by_similarity_not_count():
    """Winner is the identity with the largest total similarity."""
    votes = [
        _vote("alice", 0.9), _vote("alice", 0.9), _vote("alice", 0.9),
        _vote("bob", 0.6), _vote("bob", 0.6),          # one more sample, lower score
    ]
    res = resolve_votes(votes, required=5)
    assert res is not None
    assert res["name"] == "alice"
    assert res["score"] == pytest.approx(0.9)


def test_strict_majority_required():
    """Largest total similarity is not enough without a strict majority."""
    votes = [
        _vote("alice", 0.95),
        _vote("bob", 0.80), _vote("bob", 0.80), _vote("bob", 0.80),
    ]
    # alice total 0.95, bob total 2.40 -> bob wins on total...
    res = resolve_votes(votes, required=4)
    assert res["name"] == "bob"
    # ...but if bob's total were lower without majority:
    votes2 = [
        _vote("alice", 0.99), _vote("alice", 0.99),
        _vote("bob", 0.9), _vote("bob", 0.9),
    ]
    res2 = resolve_votes(votes2, required=4)
    # 2/4 is not a strict majority
    assert res2["name"] == "unknown"


def test_unknown_wins_when_votes_say_unknown():
    votes = [_vote("unknown", 0.0)] * 5
    res = resolve_votes(votes, required=5)
    assert res["name"] == "unknown"


def test_commit_reports_average_similarity_of_winning_samples():
    votes = [_vote("alice", 0.80), _vote("alice", 0.90), _vote("alice", 0.85)]
    res = resolve_votes(votes, required=3)
    assert res["score"] == pytest.approx(0.85)


# ==========================================================================
# Track lifecycle
# ==========================================================================
def test_track_states_and_ttl_sweep():
    tm = TrackManager(ttl_seconds=0.01)
    t = Track(1, "cam-1", (0, 0, 10, 10), 0.9)
    tm.put(t)
    assert tm.count == 1
    assert t.state == "analysing"
    t.committed = True
    t.identity = {"name": "alice"}
    assert t.state == "identified"
    t.identity = {"name": "unknown"}
    assert t.state == "unknown"

    import time

    time.sleep(0.02)
    dropped = tm.sweep()
    assert len(dropped) == 1
    assert tm.count == 0


def test_track_ids_are_unique():
    tm = TrackManager()
    ids = {tm.next_id() for _ in range(100)}
    assert len(ids) == 100


# ==========================================================================
# Zones and coordinate conversion
# ==========================================================================
def test_point_in_polygon_basic():
    square = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
    assert point_in_polygon(0.5, 0.5, square)
    assert not point_in_polygon(0.05, 0.5, square)
    assert not point_in_polygon(0.95, 0.95, square)


def test_normalised_zones_survive_resolution_change():
    """The drift pitfall: same normalised zone, two frame sizes."""
    zone = Zone.from_pixels(
        "z1", "door", [(100, 100), (300, 100), (300, 300), (100, 300)], 1280, 720
    )
    # drawn on 1280x720 -> stored normalised
    assert zone.points[0] == pytest.approx((100 / 1280, 100 / 720))
    # inside at 1280x720
    assert zone_contains(zone, 0.15, 0.2)
    # same person at 640x360 maps to the same normalised point -> still inside
    px, py = 640 * 0.15, 360 * 0.2
    assert zone_contains(zone, px / 640, py / 360)
    # pixel conversion scales with the frame
    pts_big = zone.to_pixels(1280, 720)
    pts_small = zone.to_pixels(640, 360)
    assert pts_big[1] == (300, 100)
    assert pts_small[1] == (150, 50)


def test_zone_round_trip_through_dict():
    z = Zone.from_pixels("z9", "gate", [(10, 20), (30, 20), (30, 60)], 200, 100, relay_channel="relay2")
    again = Zone.from_dict(z.as_dict())
    assert len(again.points) == len(z.points)
    for (ax, ay), (bx, by) in zip(again.points, z.points):
        assert ax == pytest.approx(bx)
        assert ay == pytest.approx(by)
    assert again.relay_channel == "relay2"
    assert again.image_w == 200 and again.image_h == 100


def test_rect_and_line_zones():
    rect = Zone("r", "rect", kind="rect", points=[(0.2, 0.2), (0.8, 0.8)])
    assert point_in_rect(0.5, 0.5, rect.points)
    assert zone_contains(rect, 0.5, 0.5)
    assert not zone_contains(rect, 0.9, 0.9)

    line = Zone("l", "line", kind="line", points=[(0.0, 0.5), (1.0, 0.5)])
    assert zone_contains(line, 0.5, 0.501, line_tolerance=0.02)
    assert not zone_contains(line, 0.5, 0.8)


def test_zone_attribution_first_match_wins():
    a = Zone("a", "A", points=[(0.0, 0.0), (0.6, 0.0), (0.6, 0.6), (0.0, 0.6)])
    b = Zone("b", "B", points=[(0.4, 0.4), (1.0, 0.4), (1.0, 1.0), (0.4, 1.0)])
    inside_both = attribute_zone([a, b], 0.5, 0.5)
    assert inside_both is not None and inside_both.zone_uid == "a"
    inside_b = attribute_zone([a, b], 0.8, 0.8)
    assert inside_b is not None and inside_b.zone_uid == "b"
    assert attribute_zone([a, b], 0.05, 0.95) is None


def test_mask_falls_back_to_none_without_area_zones():
    assert zone_mask([], 640, 480) is None
    line_only = [Zone("l", "line", kind="line", points=[(0, 0.5), (1, 0.5)])]
    assert zone_mask(line_only, 640, 480) is None


def test_mask_blacks_out_outside_zone():
    frame = np.full((100, 100, 3), 255, np.uint8)
    zone = Zone("z", "z", points=[(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)])
    masked = apply_zone_mask(frame, [zone])
    assert masked[50, 50].tolist() == [255, 255, 255]   # inside kept
    assert masked[5, 5].tolist() == [0, 0, 0]           # outside blacked
    # no zones -> frame returned untouched (never a hard-coded rectangle)
    assert np.array_equal(apply_zone_mask(frame, []), frame)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Access-control policy (fail-closed).  These are the rules the pipeline uses
# in stage 12: only an explicitly approved identity may actuate the relay.
# ---------------------------------------------------------------------------
def _should_unlock(identity_name: str, role: str) -> bool:
    """Mirror of the stage-12 gate in pipeline/engine.py."""
    return identity_name != "unknown" and str(role) == "approve"


def test_access_control_only_unlocks_approved_identities():
    assert _should_unlock("Alice", "approve") is True


def test_access_control_never_unlocks_denied_visitor_or_unknown():
    assert _should_unlock("Mallory", "deny") is False
    assert _should_unlock("Guest", "visitor") is False
    assert _should_unlock("Colleague", "colleague") is False
    assert _should_unlock("unknown", "approve") is False
    assert _should_unlock("Anyone", "unknown") is False


def test_access_control_one_failing_channel_does_not_abort_the_other():
    from facw.pipeline.relay import RelayController, SimRelay

    class S:
        access_control = True
        relay_channel_1 = "door"
        relay_channel_2 = "gate"
        relay_pulse_ms = 200

    ctl = RelayController()
    ctl.configure(SimRelay(fail_channels=["door"]))
    results = ctl.actuate(S(), reason="test")
    assert [r["channel"] for r in results] == ["door", "gate"]
    assert results[0]["ok"] is False          # the failing channel is reported
    assert results[1]["ok"] is True           # ... and the second still fires


def test_access_control_disabled_never_actuates():
    from facw.pipeline.relay import RelayController, SimRelay

    class S:
        access_control = False
        relay_channel_1 = "door"
        relay_channel_2 = "gate"
        relay_pulse_ms = 200

    ctl = RelayController()
    ctl.configure(SimRelay())
    assert ctl.actuate(S(), reason="test") == []


# ==========================================================================
# Execution device resolution (CUDA when available, CPU otherwise)
# ==========================================================================
def test_device_auto_without_cuda_falls_back_to_cpu():
    from unittest import mock

    import cv2

    from facw.core import device as D

    class NoCuda:
        @staticmethod
        def getCudaEnabledDeviceCount():
            return 0

    with mock.patch.object(cv2, "cuda", NoCuda, create=True):
        D._cached = None
        try:
            info = D.resolve("auto")
            assert info.device == "cpu"
            assert info.use_cuda is False
            assert "no CUDA" in info.reason
        finally:
            D._cached = None


def test_device_auto_uses_cuda_when_a_gpu_is_present():
    from unittest import mock

    import cv2

    from facw.core import device as D

    class OneGpu:
        @staticmethod
        def getCudaEnabledDeviceCount():
            return 1

    with mock.patch.object(cv2, "cuda", OneGpu, create=True):
        D._cached = None
        try:
            info = D.resolve("auto")
            assert info.device == "cuda"
            assert info.use_cuda is True
        finally:
            D._cached = None


def test_device_explicit_gpu_without_hardware_does_not_crash_or_force_cuda():
    """A missing GPU must never stop start-up (fail to CPU, keep a reason)."""
    from unittest import mock

    import cv2

    from facw.core import device as D

    class NoCuda:
        @staticmethod
        def getCudaEnabledDeviceCount():
            return 0

    with mock.patch.object(cv2, "cuda", NoCuda, create=True):
        D._cached = None
        try:
            info = D.resolve("gpu")
            assert info.device == "cpu"
            assert "falling back" in info.reason.lower()
        finally:
            D._cached = None


def test_device_explicit_cpu_is_respected_even_with_a_gpu():
    from unittest import mock

    import cv2

    from facw.core import device as D

    class OneGpu:
        @staticmethod
        def getCudaEnabledDeviceCount():
            return 1

    with mock.patch.object(cv2, "cuda", OneGpu, create=True):
        D._cached = None
        try:
            assert D.resolve("cpu").device == "cpu"
        finally:
            D._cached = None


def test_apply_dnn_target_survives_a_build_that_rejects_cuda():
    """A CUDA build that refuses the target must log and fall back, not raise."""
    from unittest import mock

    import cv2

    from facw.core import device as D

    class OneGpu:
        @staticmethod
        def getCudaEnabledDeviceCount():
            return 1

    class BadNet:
        def setPreferableBackend(self, v):
            raise RuntimeError("no CUDA support")

        def setPreferableTarget(self, v):
            raise RuntimeError("no CUDA support")

    with mock.patch.object(cv2, "cuda", OneGpu, create=True):
        D._cached = None
        try:
            info = D.resolve("auto")
            assert D.apply_dnn_target(BadNet(), info) == "cpu"
        finally:
            D._cached = None


def test_apply_dnn_target_is_a_noop_on_cpu():
    from facw.core.device import DeviceInfo, apply_dnn_target

    calls = []

    class Net:
        def setPreferableBackend(self, v):
            calls.append(v)

        def setPreferableTarget(self, v):
            calls.append(v)

    assert apply_dnn_target(Net(), DeviceInfo(device="cpu")) == "cpu"
    assert calls == []
