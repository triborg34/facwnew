"""Seed a demo dataset through the public API (no direct DB access).

Creates identities from the bundled fixtures, registers a file-based demo
camera, and lets the pipeline produce real events.

    python scripts/seed_demo.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
COOKIE: dict[str, str] = {}


def _req(method: str, path: str, body=None, files=None, form=None):
    url = BASE + path
    headers = {}
    data = None
    if COOKIE.get("cookie"):
        headers["Cookie"] = COOKIE["cookie"]
    if files is not None:
        boundary = "----facw" + uuid.uuid4().hex
        parts = []
        for name, (filename, content, ctype) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
            )
        for name, value in (form or {}).items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        sc = resp.headers.get("Set-Cookie")
        if sc:
            COOKIE["cookie"] = sc.split(";")[0]
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    me = _req("POST", "/api/auth/login-cookie", {"login_name": "admin", "password": "admin12345"})
    print("signed in:", me["user"]["login_name"])

    fixtures = ROOT / "tests" / "fixtures"
    people = [
        ("Lena Fixture", "approve", "lena.jpg", "1100000001"),
        ("Messi Fixture", "visitor", "messi5.jpg", "1100000002"),
    ]
    existing = {p["name"] for p in _req("GET", "/api/identities?page_size=500")["items"]}
    for name, role, fname, national in people:
        if name in existing:
            print("already enrolled:", name)
            continue
        content = (fixtures / fname).read_bytes()
        try:
            res = _req(
                "POST",
                "/api/enrolment/save",
                files={"files": (fname, content, "image/jpeg")},
                form={
                    "faces": "[0]",
                    "metadata": json.dumps(
                        {
                            "name": name,
                            "family_name": "Demo",
                            "age": 32,
                            "gender": "unknown",
                            "role": role,
                            "national_number": national,
                            "description": "seeded demo identity",
                        }
                    ),
                },
            )
            print("enrolled", name, "->", res.get("identity_id"))
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            print("enrol failed", name, exc.read().decode()[:200])

    # demo camera from the synthetic clip if it exists
    clip = ROOT / "data" / "temp" / "livecam.avi"
    if clip.exists():
        cams = _req("GET", "/api/cameras")["items"]
        if not any(c["name"] == "Hallway Demo" for c in cams):
            try:
                _req(
                    "POST",
                    "/api/cameras",
                    {
                        "name": "Hallway Demo",
                        "address": str(clip).replace("\\", "/"),
                        "stream_type": "file",
                        "port": 0,
                        "gate_label": "Demo door",
                    },
                )
                print("camera registered: Hallway Demo")
            except Exception as exc:  # noqa: BLE001
                print("camera registration failed:", exc)
    else:
        print("no demo clip at", clip, "- skipping camera")

    print("index:", _req("GET", "/api/settings")["index"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
