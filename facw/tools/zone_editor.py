"""FACW zone editor (spec 11.3).

A desktop polygon editor for access zones.  It loads a live camera snapshot so
the operator can draw the area that must contain a person before access is
granted, then saves the zone through the API.  Zone geometry is normalised to
0..1 so it is resolution-independent.

    python -m facw.tools.zone_editor [--url http://127.0.0.1:8770]
"""
from __future__ import annotations

import argparse
import json
import os
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import cv2
import numpy as np


class ZoneEditor(tk.Tk):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.title("FACW — ویرایشگر ناحیه")
        self.geometry("980x680")
        self.base = base.rstrip("/")
        self.cookie = ""
        self.cameras: list[dict] = []
        self.canvas: Optional[tk.Canvas] = None
        self._tk_img = None
        self._orig: Optional[np.ndarray] = None
        self._display_scale = 1.0
        self._points: list[tuple[float, float]] = []
        self._drawn = None
        self._zone_id = None
        self._build_login()

    # -- helpers -------------------------------------------------------
    def _request(self, method: str, path: str, body=None, raw: Optional[bytes] = None,
                 content_type: Optional[str] = None):
        url = self.base + path
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                sc = resp.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                payload = resp.read()
                if not payload:
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None

    def _build_login(self) -> None:
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="ورود به سامانه", font=("Segoe UI", 16)).pack(anchor="e", pady=(0, 16))
        ttk.Label(frame, text="نام کاربری").pack(anchor="e")
        self.user_var = tk.StringVar(value="admin")
        ttk.Entry(frame, textvariable=self.user_var, width=32).pack(anchor="e", pady=(2, 10))
        ttk.Label(frame, text="رمز عبور").pack(anchor="e")
        self.pass_var = tk.StringVar()
        e = ttk.Entry(frame, textvariable=self.pass_var, show="•", width=32)
        e.pack(anchor="e", pady=(2, 16))
        e.bind("<Return>", lambda _x: self._login())
        ttk.Button(frame, text="ورود", command=self._login).pack(anchor="e")
        self.status = ttk.Label(frame, text="", foreground="#b91c1c")
        self.status.pack(anchor="e", pady=(12, 0))

    def _login(self) -> None:
        try:
            r = self._request(
                "POST", "/api/auth/login-cookie",
                {"login_name": self.user_var.get().strip(), "password": self.pass_var.get()},
            )
            self._me = r.get("user", {})
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=str(exc))
            return
        if self._me.get("role") != "admin":
            messagebox.showerror("FACW", "ویرایش ناحیه فقط برای مدیر مجاز است")
            return
        self._build_editor()

    def _build_editor(self) -> None:
        self._clear()
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="دوربین").pack(side="right")
        self.cam_var = tk.StringVar()
        cam_box = ttk.Combobox(bar, textvariable=self.cam_var, state="readonly", width=30)
        cam_box.pack(side="right", padx=6)
        cam_box.bind("<<ComboboxSelected>>", lambda _x: self._load_snapshot())
        ttk.Button(bar, text="بارگذاری تصویر", command=self._load_snapshot).pack(side="right", padx=6)
        ttk.Button(bar, text="پاک‌کردن نقطه‌ها", command=self._clear_points).pack(side="right", padx=6)
        ttk.Button(bar, text="ذخیره ناحیه", command=self._save).pack(side="right", padx=6)
        ttk.Label(bar, text="کانال رله").pack(side="right", padx=(12, 2))
        self.channel_var = tk.StringVar(value="relay1")
        ttk.Entry(bar, textvariable=self.channel_var, width=10).pack(side="right")
        ttk.Label(bar, text="نام ناحیه").pack(side="right", padx=(12, 2))
        self.zone_name = tk.StringVar(value="")
        ttk.Entry(bar, textvariable=self.zone_name, width=16).pack(side="right")
        self.hint = ttk.Label(self, text="با کلیک روی تصویر، گوشه‌های ناحیه را بچینید؛ با کلیک راست ناحیه را ببندید",
                              foreground="#1d4ed8")
        self.hint.pack(anchor="e", padx=10)

        self.canvas = tk.Canvas(self, bg="#111", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=6)
        self.canvas.bind("<Button-1>", self._add_point)
        self.canvas.bind("<Button-3>", self._close_zone)
        self.status = ttk.Label(self, text="", foreground="#b91c1c")
        self.status.pack(anchor="e", padx=10, pady=6)

        try:
            cams = self._request("GET", "/api/cameras").get("items", [])
            self.cameras = cams
            names = [c["name"] for c in cams]
            cam_box["values"] = names
            if names:
                cam_box.current(0)
                self.cam_var.set(names[0])
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=str(exc))

    def _clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def _load_snapshot(self) -> None:
        if not self.canvas:
            return
        idx = self.cam_var.get() and next(
            (i for i, c in enumerate(self.cameras) if c["name"] == self.cam_var.get()), None
        )
        if idx is None:
            return
        cam = self.cameras[idx]
        # the snapshot is an image, not JSON, so it bypasses the JSON helper
        url = f"{self.base}/video/{cam['id']}/snapshot?flavour=raw"
        req = urllib.request.Request(url, headers={"Cookie": self.cookie})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            self.status.config(text=f"خطا در دریافت تصویر: {exc}")
            return
        arr = np.frombuffer(payload, dtype=np.uint8)
        self._orig = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if self._orig is None:
            self.status.config(text="دوربین هنوز قاب ندارد")
            return
        self._fit_image()
        self._draw_points()

    def _fit_image(self) -> None:
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10:
            cw, ch = 900, 560
        h, w = self._orig.shape[:2]
        self._display_scale = min(cw / w, ch / h, 1.0)
        disp_w, disp_h = int(w * self._display_scale), int(h * self._display_scale)
        rgb = cv2.cvtColor(self._orig, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (disp_w, disp_h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        import io
        self._tk_img = tk.PhotoImage(data=io.BytesIO(buf.tobytes()).read())
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._tk_img, anchor="nw")

    def _canvas_to_norm(self, event) -> tuple[float, float]:
        w = self._orig.shape[1]
        h = self._orig.shape[0]
        return (min(max(event.x / (w * self._display_scale), 0.0), 1.0),
                min(max(event.y / (h * self._display_scale), 0.0), 1.0))

    def _add_point(self, event) -> None:
        if self._orig is None:
            return
        p = self._canvas_to_norm(event)
        self._points.append(p)
        self._draw_points()

    def _clear_points(self) -> None:
        self._points = []
        self._draw_points()

    def _close_zone(self, _event) -> None:
        if len(self._points) >= 3:
            self._points.append(self._points[0])  # visually close
            self._draw_points()

    def _draw_points(self) -> None:
        if not self.canvas:
            return
        self.canvas.delete("zone")
        if not self._points:
            return
        w = self._orig.shape[1] * self._display_scale
        h = self._orig.shape[0] * self._display_scale
        pts = [(p[0] * w, p[1] * h) for p in self._points]
        if len(pts) >= 2:
            self.canvas.create_line(*[c for xy in pts for c in xy], fill="#22c55e",
                                    width=2, tags="zone")
        for i, (x, y) in enumerate(pts):
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#ef4444",
                                    outline="", tags="zone")

    def _save(self) -> None:
        if len(self._points) < 3:
            self.status.config(text="برای ناحیه حداقل ۳ نقطه لازم است")
            return
        idx = next((i for i, c in enumerate(self.cameras) if c["name"] == self.cam_var.get()), None)
        if idx is None:
            self.status.config(text="دوربینی انتخاب نشده")
            return
        cam = self.cameras[idx]

        zone = {
            "name": self.zone_name.get().strip() or f"ناحیه {cam['name']}",
            "kind": "polygon",
            "points": [[round(float(x), 5), round(float(y), 5)] for x, y in self._points],
            "colour": "#22c55e",
            "relay_channel": self.channel_var.get().strip(),
            "enabled": True,
        }
        try:
            # The API replaces the whole zone list for a camera, so keep any
            # other zones the operator already defined.
            current = self._request("GET", f"/api/zones/{cam['id']}").get("zones", [])
            name = zone["name"]
            kept = [z for z in current if z.get("name") != name]
            self._request("PUT", f"/api/zones/{cam['id']}", {"zones": kept + [zone]})
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=str(exc))
            return
        self.status.config(text=f"ناحیه ذخیره شد ({len(zone['points'])} نقطه)")
        messagebox.showinfo("FACW", f"ناحیه برای «{cam['name']}» ذخیره شد.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="FACW zone editor")
    ap.add_argument("--url", default=os.environ.get("FACW_URL", "http://127.0.0.1:8770"))
    args = ap.parse_args(argv)
    ZoneEditor(args.url).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
