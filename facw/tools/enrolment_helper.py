"""FACW enrolment helper (spec 11.2).

A small desktop client for batch/desk enrolment that talks to the running
service over its authenticated API - it never touches the database or the
biometric files directly.  Useful on a reception machine where the operator
wants a native window instead of a browser tab.

    python -m facw.tools.enrolment_helper [--url http://127.0.0.1:8770]
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
from tkinter import filedialog, messagebox, ttk
from typing import Optional

ROLE_CHOICES = [
    ("colleague", "همکار"),
    ("approve", "مجاز"),
    ("visitor", "مهمان"),
    ("deny", "غیرمجاز"),
]
GENDER_CHOICES = [("unknown", "نامشخص"), ("male", "مرد"), ("female", "زن")]


class Api:
    """Minimal cookie-authenticated JSON client."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.cookie = ""

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
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return json.loads(payload)
                return {"raw": payload}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None

    def login(self, user: str, password: str):
        return self._request("POST", "/api/auth/login-cookie", {"login_name": user, "password": password})

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body):
        return self._request("POST", path, body)

    def upload_photo(self, path: Path):
        """POST one image to /api/enrolment/detect and return its faces."""
        boundary = "----facw" + uuid.uuid4().hex
        content = path.read_bytes()
        ext = path.suffix.lower().lstrip(".") or "jpg"
        ctype = {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
            + content
            + b"\r\n"
            + f"--{boundary}--\r\n".encode()
        )
        return self._request(
            "POST", "/api/enrolment/detect", raw=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )


class EnrolmentHelper(tk.Tk):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.title("FACW — ثبت‌نام شخص")
        self.geometry("760x620")
        self.api = Api(base)
        self.photos: list[Path] = []
        self.face_report: list[dict] = []
        self._build_login()

    # -- login ---------------------------------------------------------
    def _build_login(self) -> None:
        self._clear()
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="ورود به سامانه", font=("Segoe UI", 16)).pack(anchor="e", pady=(0, 16))

        ttk.Label(frame, text="نام کاربری").pack(anchor="e")
        self.user_var = tk.StringVar(value="admin")
        ttk.Entry(frame, textvariable=self.user_var, width=32).pack(anchor="e", pady=(2, 10))

        ttk.Label(frame, text="رمز عبور").pack(anchor="e")
        self.pass_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.pass_var, show="•", width=32)
        entry.pack(anchor="e", pady=(2, 16))
        entry.bind("<Return>", lambda _e: self._do_login())

        ttk.Button(frame, text="ورود", command=self._do_login).pack(anchor="e")
        self.status = ttk.Label(frame, text="", foreground="#b91c1c")
        self.status.pack(anchor="e", pady=(12, 0))

    def _do_login(self) -> None:
        try:
            self.api.login(self.user_var.get().strip(), self.pass_var.get())
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=str(exc))
            return
        self._build_form()

    # -- enrolment form ------------------------------------------------
    def _build_form(self) -> None:
        self._clear()
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Button(top, text="افزودن عکس…", command=self._pick_files).pack(side="right")
        ttk.Button(top, text="پاک‌کردن فهرست", command=self._clear_photos).pack(side="right", padx=6)
        self.count_label = ttk.Label(top, text="۰ عکس")
        self.count_label.pack(side="right", padx=12)

        self.photo_list = tk.Listbox(outer, height=5)
        self.photo_list.pack(fill="x", pady=8)

        form = ttk.Frame(outer)
        form.pack(fill="x", pady=6)
        self.fields = {}
        rows = [
            ("name", "نام *", None),
            ("family_name", "نام خانوادگی", None),
            ("age", "سن", None),
            ("national_number", "شماره ملی", None),
        ]
        for idx, (key, label, _) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=idx, column=2, sticky="e", padx=4, pady=3)
            var = tk.StringVar()
            ttk.Entry(form, textvariable=var, width=26).grid(row=idx, column=1, sticky="e", pady=3)
            self.fields[key] = var

        self.role_var = tk.StringVar(value="colleague")
        ttk.Label(form, text="وضعیت مجوز").grid(row=0, column=0, sticky="e", padx=4)
        ttk.Combobox(form, textvariable=self.role_var, values=[c[0] for c in ROLE_CHOICES],
                     state="readonly", width=14).grid(row=0, column=-1, sticky="w")

        self.gender_var = tk.StringVar(value="unknown")
        ttk.Label(form, text="جنسیت").grid(row=1, column=0, sticky="e", padx=4)
        ttk.Combobox(form, textvariable=self.gender_var, values=[g[0] for g in GENDER_CHOICES],
                     state="readonly", width=14).grid(row=1, column=-1, sticky="w")

        ttk.Label(outer, text="توضیحات").pack(anchor="e", pady=(8, 0))
        self.desc = tk.Text(outer, height=3)
        self.desc.pack(fill="x")

        self.status = ttk.Label(outer, text="", foreground="#b91c1c")
        self.status.pack(anchor="e", pady=6)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="ثبت شخص", command=self._submit).pack(side="right")
        ttk.Button(buttons, text="بررسی چهره‌ها", command=self._scan).pack(side="right", padx=6)

    def _clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def _clear_photos(self) -> None:
        self.photos.clear()
        self.photo_list.delete(0, tk.END)
        self.count_label.config(text="۰ عکس")

    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="انتخاب عکس", filetypes=[("تصویر", "*.jpg *.jpeg *.png *.webp *.bmp")]
        )
        for p in paths:
            path = Path(p)
            if path not in self.photos:
                self.photos.append(path)
                self.photo_list.insert(tk.END, f"{path.name}  ({path.stat().st_size // 1024} KB)")
        self.count_label.config(text=f"{len(self.photos)} عکس")

    def _scan(self) -> None:
        """Detect faces so the operator sees whether a photo is usable."""
        if not self.photos:
            self.status.config(text="عکسی انتخاب نشده است")
            return
        lines = []
        for path in self.photos:
            try:
                res = self.api.upload_photo(path)
                faces = res.get("faces", [])
                lines.append(f"{path.name}: {len(faces)} چهره")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{path.name}: خطا — {exc}")
        self.status.config(text=" | ".join(lines))

    def _submit(self) -> None:
        if not self.photos:
            self.status.config(text="حداقل یک عکس لازم است")
            return
        name = self.fields["name"].get().strip()
        if not name:
            self.status.config(text="نام الزامی است")
            return
        meta = {
            "name": name,
            "family_name": self.fields["family_name"].get().strip(),
            "age": self.fields["age"].get().strip() or None,
            "gender": self.gender_var.get(),
            "role": self.role_var.get(),
            "national_number": self.fields["national_number"].get().strip(),
            "description": self.desc.get("1.0", tk.END).strip(),
        }
        try:
            # Reuse the console endpoint: multipart with files, faces and metadata.
            boundary = "----facw" + uuid.uuid4().hex
            parts = []
            for path in self.photos:
                ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                    f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
                    + path.read_bytes() + b"\r\n"
                )
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"faces\"\r\n\r\n"
                f"{json.dumps([0] * len(self.photos))}\r\n".encode()
            )
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n"
                f"{json.dumps(meta)}\r\n".encode()
            )
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            self.api._request(
                "POST", "/api/enrolment/save", raw=body,
                content_type=f"multipart/form-data; boundary={boundary}",
            )
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=str(exc))
            return
        messagebox.showinfo("FACW", f"«{name}» ثبت شد.")
        self._clear_photos()
        for var in self.fields.values():
            var.set("")
        self.desc.delete("1.0", tk.END)
        self.status.config(text="ثبت با موفقیت انجام شد")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="FACW enrolment helper")
    ap.add_argument("--url", default=os.environ.get("FACW_URL", "http://127.0.0.1:8770"))
    args = ap.parse_args(argv)
    EnrolmentHelper(args.url).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
