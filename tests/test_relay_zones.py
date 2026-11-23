"""Tests for the relay actuation, the zone geometry and the derived age.

The NRC board is emulated by a tiny TCP server that speaks the same four
commands as the real device, so the protocol is exercised for real - over a
socket - without any hardware.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facw.core.birthdate import age_from_birth_date, parse_birth_date
from facw.pipeline.relay import RelayController, RelayTarget, SimRelay
from facw.pipeline.nrc import NrcClient, strip_telnet


# ==========================================================================
# A fake NRC board
# ==========================================================================
class FakeBoard:
    """A one-shot TCP server that answers like an NRC relay board."""

    def __init__(self, username: str = "admin", password: str = "admin",
                 answer: bytes = b"Successful Login", status: bytes = b"") -> None:
        self.username = username
        self.password = password
        self.answer = answer
        self.status = status
        self.commands: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self.sock.settimeout(5.0)
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(5.0)
            login = conn.recv(1024).decode("utf-8", "replace")
            if login == f"{self.username}:{self.password}":
                conn.sendall(self.answer)
            else:
                conn.sendall(b"Error")
            time.sleep(0.05)
            cmd = conn.recv(1024).decode("utf-8", "replace")
            self.commands.append(cmd)
            if self.status:
                conn.sendall(self.status)
        except OSError:
            pass
        finally:
            conn.close()
            self.sock.close()

    def wait(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)


# ==========================================================================
# NRC protocol
# ==========================================================================
def test_nrc_login_accepts_the_right_credentials():
    board = FakeBoard(username="admin", password="admin")
    client = NrcClient("127.0.0.1", board.port, "admin", "admin", timeout=3.0)
    try:
        assert client.login() is True
        assert client.logged_in is True
    finally:
        client.close()
    board.wait()


def test_nrc_login_refuses_wrong_credentials():
    board = FakeBoard(username="admin", password="admin")
    client = NrcClient("127.0.0.1", board.port, "admin", "wrong", timeout=3.0)
    try:
        assert client.login() is False
        assert client.logged_in is False
    finally:
        client.close()
    board.wait()


def test_nrc_relay_contact_sends_the_pulse_command():
    board = FakeBoard()
    client = NrcClient("127.0.0.1", board.port, "admin", "admin", timeout=3.0)
    try:
        assert client.login() is True
        client.relay_contact(2, 300)
    finally:
        client.close()
    board.wait()
    assert board.commands and board.commands[0] == "%RCT2:300"


def test_nrc_relay_values_parses_the_hex_status():
    board = FakeBoard(status=b"3h")
    client = NrcClient("127.0.0.1", board.port, "admin", "admin", timeout=3.0)
    try:
        assert client.login() is True
        assert client.relay_values() == 3
    finally:
        client.close()
    board.wait()


def test_nrc_client_survives_an_unreachable_board():
    """A board that is not there must never raise into the pipeline."""
    # port 1 on localhost: nothing listens there
    client = NrcClient("127.0.0.1", 1, "admin", "admin", timeout=1.0)
    try:
        assert client.login() is False
        assert client.relay_contact(1, 100) is False
    finally:
        client.close()


def test_strip_telnet_removes_option_negotiation():
    assert strip_telnet(b"ok\xff\xfd\x01done") == b"okdone"
    assert strip_telnet(b"plain") == b"plain"


# ==========================================================================
# Relay controller
# ==========================================================================
class _Settings:
    access_control = True
    relay_host = "192.168.1.200"
    relay_port = 23
    relay_user = "admin"
    relay_password = "admin"
    relay_channel_1 = "1"
    relay_channel_2 = "2"
    relay_pulse_ms = 300
    relay_gap_ms = 0


def test_actuate_pulses_both_channels_in_order():
    controller = RelayController()
    sim = SimRelay()
    controller.configure(sim)
    results = controller.actuate(_Settings(), reason="test")
    assert [a["channel"] for a in results] == ["1", "2"]
    assert [a.channel for a in sim.actuations] == ["1", "2"]
    assert all(a["ok"] for a in results)


def test_actuate_uses_the_regions_own_relay():
    """In region mode the zone's board and relay number win."""
    controller = RelayController()
    sim = SimRelay()
    controller.configure(sim)
    target = RelayTarget("192.168.1.77", 23, "admin", "secret", 3)
    results = controller.actuate(_Settings(), target=target, reason="region")
    assert len(results) == 1
    assert results[0]["target"]["host"] == "192.168.1.77"
    assert results[0]["target"]["number"] == 3
    # the password never leaves the controller
    assert "secret" not in str(results)


def test_actuate_does_nothing_when_access_control_is_off():
    class Off(_Settings):
        access_control = False

    controller = RelayController()
    sim = SimRelay()
    controller.configure(sim)
    assert controller.actuate(Off(), reason="test") == []
    assert sim.actuations == []


def test_one_failing_channel_does_not_stop_the_next():
    controller = RelayController()
    sim = SimRelay(fail_channels=["1"])
    controller.configure(sim)
    results = controller.actuate(_Settings(), reason="test")
    assert results[0]["ok"] is False
    assert results[1]["ok"] is True


# ==========================================================================
# Zones
# ==========================================================================
def test_zone_repo_keeps_the_relay_password_sealed(tmp_path):
    from facw.core.security import Sealer
    from facw.storage.db import Database
    from facw.storage.repos_more import ZoneRepo

    # The test runs in a temporary directory: it must not touch the real
    # installation in data/.
    db = Database(tmp_path / "zones.db")
    db.bootstrap()
    sealer = Sealer.from_path(tmp_path / "zones.key")
    repo = ZoneRepo(db, sealer)
    try:
        repo.replace_for_camera("192.168.1.5", [
            {
                "id": "z1",
                "name": "ورودی",
                "kind": "polygon",
                "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
                "relay_host": "192.168.1.200",
                "relay_port": 23,
                "relay_number": 2,
                "relay_user": "admin",
                "relay_password": "topsecret",
            }
        ])
        public = repo.list_for_camera("192.168.1.5")
        assert len(public) == 1
        zone = public[0]
        assert zone["relay_host"] == "192.168.1.200"
        assert zone["relay_number"] == 2
        assert zone["has_relay_password"] is True
        assert "relay_password" not in zone
        assert "topsecret" not in str(zone)
        # the pipeline can open it again
        assert repo.relay_secrets_for_camera("192.168.1.5")["z1"] == "topsecret"

        # saving again without the password keeps the stored one
        repo.replace_for_camera("192.168.1.5", [dict(zone)])
        assert repo.relay_secrets_for_camera("192.168.1.5")["z1"] == "topsecret"
    finally:
        db.close()


def test_zone_keeps_its_password_when_the_identifier_is_renamed(tmp_path):
    """Renaming a region in the editor must not throw its credential away."""
    from facw.core.security import Sealer
    from facw.storage.db import Database
    from facw.storage.repos_more import ZoneRepo

    db = Database(tmp_path / "zones.db")
    db.bootstrap()
    sealer = Sealer.from_path(tmp_path / "zones.key")
    repo = ZoneRepo(db, sealer)
    try:
        repo.replace_for_camera("192.168.1.5", [
            {"id": "z1", "name": "ورودی", "kind": "polygon",
             "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
             "relay_host": "192.168.1.200", "relay_number": 2,
             "relay_password": "topsecret"},
        ])
        zone = repo.list_for_camera("192.168.1.5")[0]
        renamed = dict(zone)
        renamed["id"] = "entrance"          # the operator gave the region a new name
        renamed["has_relay_password"] = True
        repo.replace_for_camera("192.168.1.5", [renamed])
        assert repo.relay_secrets_for_camera("192.168.1.5")["entrance"] == "topsecret"
    finally:
        db.close()


def test_zone_carries_the_points_normalised():
    from facw.pipeline.zones import Zone

    zone = Zone.from_pixels("z1", "test", [[10, 20], [110, 20], [110, 220]], 100, 200, kind="polygon")
    assert zone.points[0] == pytest.approx((0.1, 0.1))
    assert zone.points[2] == pytest.approx((1.1, 1.1))  # not clamped, kept as drawn
    back = zone.to_pixels(100, 200)
    assert back[0] == (10, 20)


# ==========================================================================
# Birth date and the derived age
# ==========================================================================
def test_age_is_derived_from_the_birth_date():
    from datetime import date

    today = date(2026, 11, 21)
    assert age_from_birth_date("1996-05-12", today) == 30
    # the birthday has not happened yet this year
    assert age_from_birth_date("2000-12-01", today) == 25
    assert age_from_birth_date("2000-11-21", today) == 26
    assert age_from_birth_date("", today) is None
    assert age_from_birth_date("not-a-date", today) is None
    # a date in the future is not an age
    assert age_from_birth_date("2030-01-01", today) is None


def test_parse_birth_date():
    from datetime import date

    assert parse_birth_date("1996-05-12") == date(1996, 5, 12)
    assert parse_birth_date("") is None
    assert parse_birth_date(None) is None


def test_identity_stores_the_derived_age(tmp_path):
    from facw.core.security import Sealer
    from facw.storage.db import Database
    from facw.storage.repositories import IdentityRepo

    db = Database(tmp_path / "test.db")
    db.bootstrap()
    sealer = Sealer.from_path(tmp_path / "key")
    repo = IdentityRepo(db, sealer)
    try:
        ident = repo.create({"name": "تست", "birth_date": "2000-01-01", "role": "colleague"})
        row = repo.get(ident)
        assert row["birth_date"] == "2000-01-01"
        assert row["age"] == age_from_birth_date("2000-01-01")
        # a new birth date recalculates the age
        repo.update(ident, {"birth_date": "1990-06-15"})
        row = repo.get(ident)
        assert row["age"] == age_from_birth_date("1990-06-15")
    finally:
        db.close()
