"""Install / remove FACW autostart (spec 11.1).

Registers the service to come up automatically after a reboot, so a single
launch of the machine brings the whole system up (data store included).

    python -m facw.tools.autostart install [--port 8770]
    python -m facw.tools.autostart status
    python -m facw.tools.autostart remove

Windows: a Task Scheduler task running at logon (no admin rights required).
Linux:   a systemd *user* unit (``systemctl --user``), or $HOME/.config/autostart.
macOS:   a launchd LaunchAgent.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
TASK_NAME = "FACW-Service"
UNIT_NAME = "facw.service"


def _pythonw() -> str:
    """Use pythonw.exe on Windows so no console window appears at logon."""
    if os.name == "nt":
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def _command(port: int) -> list[str]:
    return [_pythonw(), "-m", "facw.server"]


# --------------------------------------------------------------------------
# Windows: Task Scheduler
# --------------------------------------------------------------------------
def _win_install(port: int) -> int:
    # Build the action via schtasks; the working directory is set with /TR's
    # wrapper through cmd so relative model/data paths resolve.
    cmd = f'cd /d "{ROOT}" && "{_pythonw()}" -m facw.server'
    args = [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", TASK_NAME,
        "/TR", f'cmd /c {cmd}',
    ]
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(r.stdout or r.stderr, file=sys.stderr)
        return r.returncode
    print(f"installed logon task '{TASK_NAME}' -> {ROOT}")
    return 0


def _win_status() -> int:
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, check=False
    )
    if r.returncode == 0:
        print(f"autostart task '{TASK_NAME}' is registered")
        print(r.stdout.strip()[:400])
        return 0
    print(f"autostart task '{TASK_NAME}' is not registered")
    return 1


def _win_remove() -> int:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       capture_output=True, text=True, check=False)
    print("removed" if r.returncode == 0 else (r.stdout or r.stderr).strip())
    return 0


# --------------------------------------------------------------------------
# POSIX: systemd --user (fallback: XDG autostart)
# --------------------------------------------------------------------------
def _systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def _posix_install(port: int) -> int:
    unit = f"""[Unit]
Description=FACW face access control service
After=network.target

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment=FACW_PORT={port}
ExecStart={sys.executable} -m facw.server
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    if subprocess.run(["sh", "-c", "command -v systemctl"], capture_output=True).returncode == 0:
        d = _systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / UNIT_NAME).write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=False)
        print(f"installed and enabled systemd user unit {d / UNIT_NAME}")
        return 0
    # fallback: XDG autostart entry
    auto = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
    auto.mkdir(parents=True, exist_ok=True)
    entry = auto / "facw.desktop"
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=FACW\n"
        f"Exec=sh -c 'cd {ROOT} && FACW_PORT={port} {sys.executable} -m facw.server'\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    print(f"installed XDG autostart entry {entry}")
    return 0


def _posix_status() -> int:
    if subprocess.run(["sh", "-c", "command -v systemctl"], capture_output=True).returncode == 0:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", UNIT_NAME], capture_output=True, text=True, check=False
        )
        print(f"{UNIT_NAME}: {(r.stdout or r.stderr).strip()}")
        return 0 if r.returncode == 0 else 1
    entry = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "facw.desktop"
    print(f"{entry}: {'present' if entry.exists() else 'absent'}")
    return 0 if entry.exists() else 1


def _posix_remove() -> int:
    if subprocess.run(["sh", "-c", "command -v systemctl"], capture_output=True).returncode == 0:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
        try:
            (_systemd_dir() / UNIT_NAME).unlink()
        except OSError:
            pass
        print("systemd user unit removed")
        return 0
    entry = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "facw.desktop"
    entry.unlink(missing_ok=True)
    print("XDG autostart entry removed")
    return 0


# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="facw-autostart", description="FACW autostart control")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FACW_PORT", "8770")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("status")
    sub.add_parser("remove")
    args = ap.parse_args(argv)

    if os.name == "nt":
        return {"install": lambda: _win_install(args.port),
                "status": _win_status,
                "remove": _win_remove}[args.cmd]()
    return {"install": lambda: _posix_install(args.port),
            "status": _posix_status,
            "remove": _posix_remove}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
