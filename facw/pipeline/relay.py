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

Transports: ``RelayClient`` is behind a small interface.  The default
implementation speaks the common HTTP relay-board protocol
(``GET /relay/on?ch=N``); a ``SimRelay`` records actuations for tests and for
running a demo without hardware.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core.logging_setup import get_logger

log = get_logger("access.relay")


@dataclass
class RelayAction:
    channel: str
    ok: bool
    at: float = field(default_factory=time.time)
    error: str = ""
    duration_ms: int = 0

    def as_dict(self) -> Dict:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "at": self.at,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class RelayTransport(Protocol):
    def pulse(self, channel: str, duration_ms: int) -> bool: ...

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

    def ping(self) -> bool:
        try:
            req = Request(self._url("/relay/status", {}), headers={"User-Agent": "facw/1.0"})
            with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return 200 <= resp.status < 500
        except Exception:
            return False

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

    def actuate(
        self,
        settings,
        zone_channel: Optional[str] = None,
        reason: str = "",
    ) -> List[Dict]:
        """Pulse every applicable channel; one failure never stops the rest."""
        if not settings.access_control:
            return []
        channels = self.channels_for(settings, zone_channel)
        if not channels:
            log.warning("access control enabled but no relay channel configured", extra={"reason": reason})
            return []
        transport = self.transport
        results: List[Dict] = []
        if transport is None:
            log.warning("relay transport not configured", extra={"reason": reason})
            return [{"channel": c, "ok": False, "error": "no transport"} for c in channels]
        for idx, ch in enumerate(channels):
            if idx > 0:
                # short delay between channels so the two pulses are distinct
                time.sleep(min(0.25, settings.relay_pulse_ms / 4000.0))
            try:
                ok = transport.pulse(ch, int(settings.relay_pulse_ms))
                err = "" if ok else "device rejected pulse"
            except Exception as exc:  # noqa: BLE001 - isolation
                ok, err = False, str(exc)
            action = RelayAction(ch, ok, error=err, duration_ms=int(settings.relay_pulse_ms))
            results.append(action.as_dict())
            log.info(
                "relay pulse",
                extra={"channel": ch, "ok": ok, "error": err, "reason": reason,
                       "duration_ms": settings.relay_pulse_ms},
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
            "host": settings.relay_host,
            "port": settings.relay_port,
        }

    def recent(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return list(self._log[-limit:])
