"""Access-control relay actuation (stage 12).

Requirements handled explicitly:

* Zone mode: pulse the relay bound to the zone where the person was seen.
  Otherwise: pulse the globally configured channel(s).
* With two channels both are pulsed **in order** with a short delay.
* A failure on one channel must NOT abort the remaining channels.
* Nothing here may raise into the pipeline: every path is caught and logged.
* Relays default to fail-closed: on any ambiguity (no channel configured,
  connection error) the relay is left closed.
* Pulse duration is bounded by configuration.

Transports: ``RelayClient`` is behind a small interface.  Two are shipped:

* ``NrcRelay`` speaks the NRC board protocol (telnet-style commands on port
  23, see :mod:`facw.pipeline.nrc`).  Each zone may bind its *own* board and
  relay number, so a region can open a different door than the rest.
* ``HttpRelay`` speaks the common HTTP relay-board protocol
  (``GET /relay/on?ch=N``).

``SimRelay`` records actuations for tests and for running a demo without
hardware.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core.logging_setup import get_logger
from .nrc import NrcClient

log = get_logger("access.relay")


@dataclass(frozen=True)
class RelayTarget:
    """A concrete relay to actuate: host, credentials and relay number.

    A zone in region mode binds its own target, which is what makes one
    region open a different door than another.

    ``channel`` keeps the configured channel name (HTTP boards and the
    simulator use it as a name), while ``number`` is the relay index the NRC
    board understands.  A zone target has no channel name: it addresses one
    relay number directly.
    """

    host: str
    port: int = 23
    username: str = "admin"
    password: str = "admin"
    number: int = 1
    channel: str = ""

    @property
    def label(self) -> str:
        """What the action log calls this relay."""
        return str(self.channel or self.number)

    def as_dict(self) -> Dict:
        # The password is never part of a log line or an audit record.
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "number": self.number,
            "channel": self.label,
        }


@dataclass
class RelayAction:
    channel: str
    ok: bool
    at: float = field(default_factory=time.time)
    error: str = ""
    duration_ms: int = 0
    target: Optional[Dict] = None

    def as_dict(self) -> Dict:
        out = {
            "channel": self.channel,
            "ok": self.ok,
            "at": self.at,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
        if self.target:
            out["target"] = self.target
        return out


class RelayTransport(Protocol):
    def pulse(self, channel: str, duration_ms: int) -> bool: ...

    def pulse_target(self, target: RelayTarget, duration_ms: int) -> bool: ...

    def ping(self) -> bool: ...

    def close(self) -> None: ...


class HttpRelay:
    """HTTP relay board: /relay/on?ch=..&ms=..  (widely used protocol)."""

    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def _url(self, path: str, params: Dict) -> str:
        qs = urlencode(params)
        scheme = "https" if self.port == 443 else "http"
        return f"{scheme}://{self.host}:{self.port}{path}?{qs}"

    def pulse(self, channel: str, duration_ms: int) -> bool:
        if not channel:
            return False
        url = self._url("/relay/on", {"ch": channel, "ms": int(duration_ms)})
        try:
            req = Request(url, headers={"User-Agent": "facw/1.0"})
            with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - LAN device
                return 200 <= resp.status < 300
        except Exception as exc:  # noqa: BLE001 - never propagate
            log.warning("relay pulse failed", extra={"channel": channel, "error": str(exc)})
            return False

    def pulse_target(self, target: RelayTarget, duration_ms: int) -> bool:
        """Pulse relay ``target.number`` on ``target.host``.

        An HTTP board has no notion of "channel N" beyond the query
        parameter, so the target's channel name (or its number) is used.
        """
        if not target.host:
            return False
        board = HttpRelay(target.host, target.port, self.timeout)
        return board.pulse(target.label, duration_ms)

    def ping(self) -> bool:
        try:
            req = Request(self._url("/relay/status", {}), headers={"User-Agent": "facw/1.0"})
            with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return 200 <= resp.status < 500
        except Exception:
            return False

    def close(self) -> None:
        pass


class NrcRelay:
    """NRC relay board (telnet-style command protocol on port 23).

    Every actuation opens its own short-lived connection: the board holds no
    state between commands, and a connection that dies mid-command must not
    take the next one with it.
    """

    def __init__(
        self,
        host: str,
        port: int = 23,
        username: str = "admin",
        password: str = "admin",
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.timeout = float(timeout)

    def _client(self, host: Optional[str] = None, port: Optional[int] = None,
                username: Optional[str] = None, password: Optional[str] = None) -> NrcClient:
        return NrcClient(
            host or self.host,
            int(port if port is not None else self.port),
            username if username is not None else self.username,
            password if password is not None else self.password,
            timeout=self.timeout,
        )

    def pulse(self, channel: str, duration_ms: int) -> bool:
        """Pulse one channel of the configured board."""
        try:
            number = int(channel)
        except (TypeError, ValueError):
            return False
        return self.pulse_target(RelayTarget(self.host, self.port, self.username, self.password, number), duration_ms)

    def pulse_target(self, target: RelayTarget, duration_ms: int) -> bool:
        """Pulse relay ``target.number`` on ``target.host``."""
        if not target.host:
            return False
        client = self._client(target.host, target.port, target.username, target.password)
        try:
            if not client.login():
                return False
            return client.relay_contact(target.number, duration_ms)
        except Exception as exc:  # noqa: BLE001 - a relay must never break the pipeline
            log.warning("relay pulse failed", extra={"target": target.as_dict(), "error": str(exc)})
            return False
        finally:
            client.close()

    def ping(self) -> bool:
        client = self._client()
        try:
            return client.login()
        except Exception:  # noqa: BLE001
            return False
        finally:
            client.close()

    def close(self) -> None:
        pass


class SimRelay:
    """In-memory relay for tests / hardware-free demos."""

    def __init__(self, fail_channels: Optional[List[str]] = None) -> None:
        self.actuations: List[RelayAction] = []
        self.fail_channels = set(fail_channels or [])
        self._lock = threading.Lock()

    def pulse(self, channel: str, duration_ms: int) -> bool:
        ok = channel not in self.fail_channels and bool(channel)
        with self._lock:
            self.actuations.append(RelayAction(channel, ok, duration_ms=duration_ms))
        return ok

    def pulse_target(self, target: RelayTarget, duration_ms: int) -> bool:
        channel = target.label
        ok = channel not in self.fail_channels and bool(channel)
        with self._lock:
            self.actuations.append(
                RelayAction(channel, ok, duration_ms=duration_ms, target=target.as_dict())
            )
        return ok

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


class RelayController:
    """Turns a committed identity into relay pulses."""

    def __init__(self) -> None:
        self._transport: Optional[RelayTransport] = None
        self._lock = threading.Lock()
        self._log: List[Dict] = []
        self._max_log = 500

    # -- wiring --------------------------------------------------------
    def configure(self, transport: Optional[RelayTransport]) -> None:
        with self._lock:
            old = self._transport
            self._transport = transport
        if old and old is not transport:
            try:
                old.close()
            except Exception:
                pass

    @property
    def transport(self) -> Optional[RelayTransport]:
        return self._transport

    # -- actuation -----------------------------------------------------
    def channels_for(self, settings, zone_channel: Optional[str] = None) -> List[str]:
        """Which channels to pulse.

        Zone mode with a bound zone wins; otherwise the global channels, in
        order (channel 1 then channel 2).
        """
        if zone_channel:
            return [zone_channel]
        chans = [settings.relay_channel_1, settings.relay_channel_2]
        return [c for c in chans if c]

    def targets_for(self, settings, zone_channel: Optional[str] = None) -> List[RelayTarget]:
        """The concrete relays to pulse, built from the global configuration.

        Channel 1 is pulsed first and channel 2 second; an empty channel is
        skipped, which is how "one channel" is expressed.  A channel that is
        a plain number is also the relay number on an NRC board; a named
        channel falls back to its position (1, then 2).
        """
        host = str(getattr(settings, "relay_host", "") or "")
        port = int(getattr(settings, "relay_port", 23) or 23)
        username = str(getattr(settings, "relay_user", "admin") or "admin")
        password = str(getattr(settings, "relay_password", "admin") or "admin")
        targets = []
        for index, ch in enumerate(self.channels_for(settings, zone_channel), start=1):
            try:
                number = int(str(ch).strip())
            except (TypeError, ValueError):
                number = index
            targets.append(
                RelayTarget(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    number=number,
                    channel=str(ch),
                )
            )
        return targets

    def actuate(
        self,
        settings,
        zone_channel: Optional[str] = None,
        target: Optional[RelayTarget] = None,
        reason: str = "",
    ) -> List[Dict]:
        """Pulse every applicable relay; one failure never stops the rest.

        ``target`` is the relay of the region the person was seen in (region
        mode).  Without it the globally configured channels are pulsed in
        order, with a short gap between them so the two pulses stay distinct.
        """
        if not settings.access_control:
            return []
        transport = self.transport
        if transport is None:
            log.warning("relay transport not configured", extra={"reason": reason})
            if target is not None:
                chans = [target.label]
            else:
                chans = self.channels_for(settings, zone_channel)
            return [{"channel": c, "ok": False, "error": "no transport"} for c in chans]

        if target is not None:
            targets = [target]
        else:
            targets = self.targets_for(settings, zone_channel)
        if not targets:
            log.warning("access control enabled but no relay channel configured", extra={"reason": reason})
            return []

        results: List[Dict] = []
        for idx, tgt in enumerate(targets):
            if idx > 0:
                # A gap between the two channels so they stay distinct
                # actuations (the reference behaviour was a one second wait).
                gap = getattr(settings, "relay_gap_ms", 1000)
                time.sleep(max(0.0, min(float(gap), 5000.0)) / 1000.0)
            try:
                ok = transport.pulse_target(tgt, int(settings.relay_pulse_ms))
                err = "" if ok else "device rejected pulse"
            except Exception as exc:  # noqa: BLE001 - isolation
                ok, err = False, str(exc)
            action = RelayAction(
                tgt.label,
                ok,
                error=err,
                duration_ms=int(settings.relay_pulse_ms),
                target=tgt.as_dict(),
            )
            results.append(action.as_dict())
            log.info(
                "relay pulse",
                extra={"channel": tgt.label, "host": tgt.host, "ok": ok, "error": err,
                       "reason": reason, "duration_ms": settings.relay_pulse_ms},
            )
        with self._lock:
            self._log.extend(results)
            if len(self._log) > self._max_log:
                del self._log[: len(self._log) - self._max_log]
        return results

    def test(self, settings) -> Dict:
        """Connection test for the settings screen."""
        transport = self.transport
        if transport is None:
            return {"ok": False, "detail": "no relay transport configured"}
        try:
            ok = transport.ping()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {
            "ok": bool(ok),
            "detail": "reachable" if ok else "no answer from relay host",
            "host": getattr(settings, "relay_host", ""),
            "port": getattr(settings, "relay_port", 23),
        }

    def recent(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return list(self._log[-limit:])
