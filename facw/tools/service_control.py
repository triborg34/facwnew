"""FACW service control (spec 11.1).

Start, stop, restart and inspect the service without hunting for PIDs by hand.
Works on Windows and POSIX, and is the module referenced by
``scripts/devrestart.sh``.

    python -m facw.tools.service_control status
    python -m facw.tools.service_control start [--port 8770]
    python -m facw.tools.service_control stop
    python -m facw.tools.service_control restart
    python -m facw.tools.service_control logs [-n 40]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
PID_FILE = ROOT / "data" / "facw.pid"
LOG_DIR = ROOT / "logs"
BOOT_LOG = LOG_DIR / "boot.log"
APP_LOG = LOG_DIR / "facw.log"


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------
def _pid_file() -> Optional[int]:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """True when the process exists (works on Windows and POSIX)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _port_holders(port: int) -> list[int]:
    """PIDs listening on ``port`` (needed when the pid file is missing)."""
    pids: list[int] = []
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=False).stdout
        for line in out.splitlines():
            if f":{port}" in line and ("LISTENING" in line.upper() or "LISTEN" in line.upper()):
                tail = line.split()
                if tail and tail[-1].isdigit():
                    pids.append(int(tail[-1]))
    else:
        out = subprocess.run(
            ["sh", "-c", f"lsof -ti tcp:{port} 2>/dev/null || true"],
            capture_output=True, text=True, check=False,
        ).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return sorted(set(pids))


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def health(port: int, timeout: float = 2.0) -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            data = json.loads(body) if body else {}
            return r.status == 200 and data.get("ready") is True, body
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    pid = _pid_file()
    running = bool(pid and _alive(pid))
    ok, body = health(args.port)
    holders = _port_holders(args.port)
    print(f"pid file      : {pid if pid else '-'}")
    print(f"process alive : {'yes' if running else 'no'}")
    print(f"port {args.port} free  : {'yes' if _free(args.port) else 'no'}")
    print(f"port holders  : {holders or '-'}")
    print(f"health        : {'ready' if ok else 'not ready'} {body[:200]}")
    return 0 if ok else 1


def cmd_stop(args) -> int:
    killed = False
    pid = _pid_file()
    if pid and _alive(pid):
        print(f"stopping pid {pid}")
        _kill(pid)
        killed = True
    for holder in _port_holders(args.port):
        if holder and holder != os.getpid():
            print(f"stopping port holder {holder}")
            _kill(holder)
            killed = True
    # wait for the port to come free
    for _ in range(40):
        if _free(args.port):
            break
        time.sleep(0.25)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    print("stopped" if killed else "nothing to stop")
    return 0


def cmd_start(args) -> int:
    if not _free(args.port):
        print(f"port {args.port} is already in use; run 'stop' first", file=sys.stderr)
        return 2
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("FACW_PORT", str(args.port))
    # The service opens the console in the default browser once healthy.  Callers
    # that must not spawn a window (tests, servers) set FACW_OPEN_BROWSER=0.
    env.setdefault("FACW_OPEN_BROWSER", "1")
    with open(BOOT_LOG, "ab") as log:
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            [sys.executable, "-m", "facw.server"],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=creation,
        )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"started pid {proc.pid}; waiting for health")
    for _ in range(80):
        ok, _body = health(args.port)
        if ok:
            print(f"ready on http://127.0.0.1:{args.port}")
            return 0
        if proc.poll() is not None:
            print("service exited during startup; see logs/boot.log", file=sys.stderr)
            return 3
        time.sleep(0.25)
    print("service did not become ready in time", file=sys.stderr)
    return 4


def cmd_restart(args) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_logs(args) -> int:
    for path in (APP_LOG, BOOT_LOG):
        if not path.exists():
            print(f"--- {path.name}: missing ---")
            continue
        print(f"--- {path.name} (last {args.n}) ---")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(exc)
            continue
        for line in lines[-args.n:]:
            print(line)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="facw-service", description="FACW service control")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FACW_PORT", "8770")))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("start").set_defaults(func=cmd_start)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("restart").set_defaults(func=cmd_restart)
    p_logs = sub.add_parser("logs")
    p_logs.add_argument("-n", type=int, default=40)
    p_logs.set_defaults(func=cmd_logs)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
