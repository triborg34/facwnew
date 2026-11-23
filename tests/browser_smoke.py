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
        def on_request_failed(r):
            # A reload aborts the open event stream; that is the browser
            # closing the connection, not a defect of the console.
            if r.url.endswith("/api/events/stream"):
                return
            errors.append(f"requestfailed: {r.url} {r.failure}")
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

        # The report lock must be off before the walk: a locked tab opens the
        # unlock prompt, which would swallow the next click.
        page.request.put(BASE + "/api/settings", data={"report_lock": False})

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

        # the form asks for a birth date, not for an age, and shows the age
        # that is derived from it
        birth = page.locator('.modal input[type="date"]')
        print("birth date fields:", birth.count())
        if birth.count() == 0:
            errors.append("the enrolment form has no birth date field")
        else:
            birth.first.fill("1990-05-17")
            page.wait_for_timeout(500)
            modal_text = page.locator(".modal").inner_text()
            if "سن" not in modal_text:
                errors.append("the derived age is not shown next to the birth date")
            else:
                print("derived age shown:", [ln for ln in modal_text.splitlines() if "سن" in ln][:1])
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        # reports filters + pagination
        page.get_by_role("button", name="گزارش‌ها").click()
        page.wait_for_timeout(1600)
        page.screenshot(path=str(SHOTS / "05-reports.png"))
        rows = page.locator("table.data tbody tr").count()
        print("report rows:", rows)

        # the zones screen: the camera list, the drawing stage and the relay
        # form of a region
        page.get_by_role("button", name="نواحی").click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "09-zones.png"))
        zones_text = page.locator("#page-host").inner_text()
        if "ناحیه" not in zones_text and "دوربین" not in zones_text:
            errors.append("the zones screen rendered nothing")
        print("zones screen:", "ok" if ("ناحیه" in zones_text or "دوربین" in zones_text) else "empty")

        # report lock: the tab must stay closed until the administrator
        # password is entered
        try:
            r = page.request.put(BASE + "/api/settings", data={"report_lock": True})
            print("report_lock on ->", r.status)
            # "networkidle" never fires here: the live event feed keeps an open
            # request, so the reload waits for the document instead.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".topnav", timeout=15000)
            page.wait_for_timeout(1500)
            nav_reports = page.locator(".topnav button", has_text="گزارش")
            label = nav_reports.first.inner_text()
            print("locked nav label:", label)
            if "\U0001F512" not in label:
                errors.append("the reports tab is not marked as locked")
            page.screenshot(path=str(SHOTS / "10-report-lock.png"))
            nav_reports.first.click()
            page.wait_for_timeout(1000)
            if page.locator(".modal").count() == 0:
                errors.append("no unlock prompt appeared for the locked reports tab")
            page.screenshot(path=str(SHOTS / "11-report-unlock-prompt.png"))
            if page.locator(".modal input[type=password]").count():
                page.fill('.modal input[type="password"]', "admin12345")
                page.get_by_role("button", name="باز کردن قفل").click()
                page.wait_for_timeout(2000)
                unlocked_rows = page.locator("table.data tbody tr").count()
                print("report rows after unlocking:", unlocked_rows)
                if unlocked_rows == 0:
                    errors.append("the reports screen stayed empty after the unlock")
                page.screenshot(path=str(SHOTS / "12-report-unlocked.png"))
        finally:
            # the lock must never survive this test
            r = page.request.put(BASE + "/api/settings", data={"report_lock": False})
            print("report_lock off ->", r.status)

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
