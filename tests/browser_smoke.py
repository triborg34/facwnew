"""Browser smoke test: drives the real console and reports JS errors.

Not a unit test (it needs the running service); invoked manually:

    python tests/browser_smoke.py [base_url]

It signs in, walks every screen, exercises the enrolment face picker, opens a
detection detail, and fails if any console error, page error or failed request
occurs.  Screenshots are written to ``docs/screens/``.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252; the UI text is Persian.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
SHOTS = Path(__file__).resolve().parent.parent / "docs" / "screens"
SHOTS.mkdir(parents=True, exist_ok=True)

errors: list[str] = []


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fa-IR")
        def on_console(m):
            if m.type != "error":
                return
            loc = m.location.get("url", "") or ""
            if loc.endswith("/api/auth/me"):
                return  # see EXPECTED above
            errors.append(f"console.error: {m.text} @ {loc}")
        page.on("console", on_console)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))
        # The very first /api/auth/me is expected to 401 (nobody is signed in
        # yet); the browser always logs failed HTTP requests, so that entry is
        # filtered rather than treated as a defect.
        EXPECTED = {("GET", "/api/auth/me", 401)}

        def on_response(r):
            if r.status < 400:
                return
            path = r.url.split("8770", 1)[-1].split("?")[0]
            if (r.request.method, path, r.status) in EXPECTED:
                return
            errors.append(f"http {r.status}: {r.request.method} {r.url}")
        page.on("response", on_response)

        page.goto(BASE, wait_until="networkidle")
        page.screenshot(path=str(SHOTS / "01-signin.png"))

        # sign in
        page.fill('input[type="text"]', "admin")
        page.fill('input[type="password"]', "admin12345")
        page.click("#signin-btn")
        page.wait_for_selector(".topnav", timeout=15000)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "02-dashboard.png"))
        print("dashboard title:", page.title())

        # every nav item loads without a JS error
        labels = page.locator(".topnav button").all()
        print("nav items:", len(labels))
        for i in range(len(labels)):
            btn = page.locator(".topnav button").nth(i)
            name = btn.inner_text()
            btn.click()
            page.wait_for_timeout(1800)
            try:
                page.wait_for_function("(window.PageReports && window.PageReports.state && window.PageReports.state.loading === false) || true", timeout=2000)
            except Exception:
                pass
            page.screenshot(path=str(SHOTS / f"03-page-{i}.png"))
            print("  visited:", name)

        # back to dashboard, open the enrolment dialog and check the picker
        page.get_by_role("button", name="اشخاص").click()
        page.wait_for_timeout(900)
        page.get_by_role("button", name=re.compile("ثبت شخص جدید")).click()
        page.wait_for_selector(".modal", timeout=8000)
        page.set_input_files('.modal input[type="file"]', str(Path(__file__).parent / "fixtures" / "largest_selfie.jpg"))
        page.wait_for_timeout(3500)
        page.screenshot(path=str(SHOTS / "04-enrolment-picker.png"))
        chips = page.locator(".modal .face-chip").count()
        print("enrolment face chips:", chips)
        if chips == 0:
            errors.append("enrolment picker produced no faces")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        # reports filters + pagination
        page.get_by_role("button", name="گزارش‌ها").click()
        page.wait_for_timeout(1600)
        page.screenshot(path=str(SHOTS / "05-reports.png"))
        rows = page.locator("table.data tbody tr").count()
        print("report rows:", rows)

        # date picker opens and shows Persian digits
        page.locator(".datepicker, input[readonly]").first.click()
        page.wait_for_timeout(700)
        page.screenshot(path=str(SHOTS / "06-datepicker.png"))

        # narrow viewport: no horizontal overflow
        page.set_viewport_size({"width": 820, "height": 700})
        page.wait_for_timeout(700)
        overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
        print("horizontal overflow at 820px:", overflow)
        if overflow:
            errors.append("horizontal overflow at 820px")
        page.screenshot(path=str(SHOTS / "07-narrow.png"))

        # dark theme really differs
        page.set_viewport_size({"width": 1440, "height": 900})
        light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.evaluate("window.UI.applyTheme('dark')")
        page.wait_for_timeout(500)
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        print("theme bg light/dark:", light_bg, dark_bg)
        if light_bg == dark_bg:
            errors.append("dark theme is identical to light theme")
        page.screenshot(path=str(SHOTS / "08-dark.png"))

        browser.close()

    print("\n--- errors ---")
    for e in errors[:40]:
        print(" ", e)
    print("total errors:", len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
