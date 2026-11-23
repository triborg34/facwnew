"""NRC IP relay boards: the telnet-style command protocol on port 23.

The NRC boards (the "Network Relay Controller" family) speak a very small
line protocol over a plain TCP socket:

===================  ==========================================================
``user:password``    sign in; the board answers ``Successful Login``
``%RCT<n>``          momentary contact of relay <n> (the board's own default)
``%RCT<n>:<ms>``     momentary contact of relay <n> for <ms> milliseconds
``%RON<n>``          latch relay <n> on
``%ROF<n>``          latch relay <n> off
``%RST``             relay status; the answer is a hex number, e.g. ``3h``
``%ISW``             digital inputs as a hex number
``%IHV``             high-voltage inputs as a hex number
===================  ==========================================================

The protocol is implemented here rather than pulled in as a dependency
because the reference package (``nrcpy``) is a thin wrapper over the same
four lines, and because ``telnetlib`` - the obvious way to talk to a telnet
server - was removed from the standard library in Python 3.13.

Everything in this module is defensive: a relay board is a LAN device that
may be unreachable, may answer slowly, or may answer nothing at all.  No
method raises into the pipeline; they return a verdict instead.
"""
from __future__ import annotations

import socket
import time
from typing import Optional, Tuple

from ..core.logging_setup import get_logger

log = get_logger("access.nrc")

# Telnet control bytes (RFC 854).  The NRC boards do not negotiate options,
# but a stray IAC in the stream must never end up inside a parsed answer.
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240

LOGIN_OK = "Successful Login"


def strip_telnet(data: bytes) -> bytes:
    """Remove telnet option negotiation from a raw chunk."""
    if IAC not in data:
        return data
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        if byte != IAC:
            out.append(byte)
            i += 1
            continue
        if i + 1 >= n:
            break  # truncated sequence at the end of the chunk
        cmd = data[i + 1]
        if cmd == IAC:  # escaped 0xFF
            out.append(IAC)
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            i += 3  # command + option
        elif cmd == SB:  # sub-negotiation: up to IAC SE
            end = data.find(bytes([IAC, SE]), i + 2)
            i = n if end < 0 else end + 2
        else:
            i += 2
    return bytes(out)


class NrcClient:
    """One connection to an NRC relay board."""

    def __init__(
        self,
        host: str,
        port: int = 23,
        username: str = "admin",
        password: str = "admin",
        timeout: float = 3.0,
    ) -> None:
        self.host = str(host).strip()
        self.port = int(port)
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.sock: Optional[socket.socket] = None
        self.logged_in = False

    # -- connection -----------------------------------------------------
    def connect(self) -> bool:
        """Open the socket.  Never raises: a failure is a ``False``."""
        if self.sock is not None:
            return True
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            self.sock = sock
            # Some boards send a banner right after the connection is up.
            self._drain(0.25)
            return True
        except OSError as exc:
            log.warning("relay connect failed", extra={"host": self.host, "port": self.port, "error": str(exc)})
            self.sock = None
            return False

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.logged_in = False

    # -- low level ------------------------------------------------------
    def _read(self, timeout: Optional[float] = None) -> bytes:
        """Read whatever the board has to say, up to the timeout."""
        if self.sock is None:
            return b""
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(1024)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # A complete answer usually arrives in one packet; stop early
            # once we have something that could be a whole line.
            if b"\n" in buf or b"\r" in buf:
                break
        return strip_telnet(buf)

    def _drain(self, timeout: float) -> None:
        """Throw away a banner without waiting for a full timeout."""
        self._read(timeout)

    def send(self, command: str) -> str:
        """Send one command and return the answer as text (may be empty)."""
        if self.sock is None and not self.connect():
            return ""
        try:
            self.sock.sendall(str(command).encode("ascii", "replace"))
        except OSError as exc:
            log.warning("relay send failed", extra={"host": self.host, "error": str(exc)})
            self.close()
            return ""
        return self._read().decode("utf-8", "replace").strip()

    # -- protocol -------------------------------------------------------
    def login(self) -> bool:
        """Sign in.  ``True`` only when the board confirms it."""
        if not self.connect():
            return False
        answer = self.send(f"{self.username}:{self.password}")
        self.logged_in = answer.strip() == LOGIN_OK
        if not self.logged_in:
            log.warning("relay login refused", extra={"host": self.host, "answer": answer[:60]})
        return self.logged_in

    def _command(self, command: str, expect: str = "") -> Tuple[bool, str]:
        """Send a command that may or may not be answered."""
        if self.sock is None and not self.connect():
            return False, ""
        answer = self.send(command)
        if self.sock is None:
            # The board is gone: the command never reached a relay.
            return False, answer
        if expect and expect not in answer:
            return False, answer
        # A board that answers "Error" refused the command.
        if answer.strip().lower() == "error":
            return False, answer
        return True, answer

    def relay_contact(self, number: int, delay_ms: Optional[int] = None) -> bool:
        """Momentarily close relay <number> for <delay_ms> milliseconds."""
        number = int(number)
        if number < 1:
            return False
        if delay_ms is None:
            command = f"%RCT{number}"
        else:
            command = f"%RCT{number}:{int(delay_ms)}"
        ok, _ = self._command(command)
        return ok

    def relay_on(self, number: int) -> bool:
        ok, _ = self._command(f"%RON{int(number)}")
        return ok

    def relay_off(self, number: int) -> bool:
        ok, _ = self._command(f"%ROF{int(number)}")
        return ok

    def relay_values(self) -> Optional[int]:
        """The relay status word as an integer, or ``None`` when unreadable."""
        answer = self.send("%RST")
        text = answer.strip()
        if not text:
            return None
        if text.endswith("h"):
            text = text[:-1]
        try:
            return int(text, 16)
        except ValueError:
            return None

    def relay_state(self, number: int) -> Optional[bool]:
        """``True`` when relay <number> is currently closed, ``None`` if unknown."""
        values = self.relay_values()
        if values is None:
            return None
        return bool((values >> (int(number) - 1)) & 1)

    def __enter__(self) -> "NrcClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
