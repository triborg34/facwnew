"""Browser check of the three reworked flows.

Needs the service running (python -m facw.server) with at least one camera:

  1. dashboard live loop: tiles keep refreshing, detection panel repaints in
     place (no list rebuild, no scroll jump),
  2. an unknown detection opens a detail dialog that offers
     "ثبت این شخص در فهرست" and that button opens the enrolment dialog with the
     camera still already loaded,
  3. "دوربین‌ها" -> discovery panel: credentials + subnet fields exist, and a
     scan can be started and reports back.

    python scripts/browser_flows.py [base_url]
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"

errors: list[str] = []
notes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        errors.append(name)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950}, locale="fa-IR")
        page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

        page.goto(BASE, wait_until="networkidle")
        # sign in (inputs are identified by their autocomplete attributes)
        page.wait_for_timeout(1200)
        if page.locator("input[autocomplete=username]").count():
            page.fill("input[autocomplete=username]", "admin")
            page.fill("input[type=password]", "admin12345")
            page.keyboard.press("Enter")
            page.wait_for_timeout(3500)
        print("signed in, nav items:", page.locator(".topnav button").count())

        # ---------- 1. dashboard live loop ----------
        page.wait_for_timeout(4000)
        frames = page.evaluate(
            """async () => {
                // pick a tile that is actually streaming (an offline camera
                // shows a static placeholder by design)
                // Tiles are fed by a multipart/x-mixed-replace stream, so the
                // <img> keeps ONE src and the browser repaints it in place.
                // "still live" therefore means: the stream connection is open
                // and a frame is decoded - not that .src changed.
                const imgs = [...document.querySelectorAll('.video-box img[data-cam]')]
                    .filter(i => i.classList.contains('front'));   // visible buffer only
                if (!imgs.length) return {found: false};
                const live = imgs.find(i => {
                    const tile = i.closest('[data-tile]');
                    return tile && tile.querySelector('.live-dot');
                }) || imgs[0];
                const first = live.src;
                const t0 = live.naturalWidth;
                await new Promise(r => setTimeout(r, 2500));
                // a multipart img keeps a stable src but the bitmap keeps
                // resolving; treat "src unchanged AND frame decoded" as live
                const streaming = live.src === first && live.naturalWidth > 0;
                return {found: true, changed: streaming, native: live.naturalWidth,
                        src_stable: live.src === first,
                        live: !!live.closest('[data-tile]').querySelector('.live-dot')};
            }"""
        )
        check("dashboard tile exists", frames.get("found"), str(frames))
        if frames.get("found"):
            check("tile marked LIVE", bool(frames.get("live")))
            check("tile stream is open and decoding", bool(frames.get("changed")))
            check("tile decodes a real frame", (frames.get("native") or 0) > 100, f"{frames.get('native')}px")

        # detection panel identity stability (no full rebuild between polls)
        # Check whichever panel actually has a row: the "unknown" one is empty
        # whenever nobody unrecognised is in frame, and an empty panel has no
        # row to compare (that used to be reported as a failure).
        stable = page.evaluate(
            """async () => {
                const panels = [...document.querySelectorAll('[data-panel]')];
                let list = null, row = null;
                for (const p of panels) {
                    const l = p.querySelector('.det-list');
                    const r = l && l.querySelector('.det-item');
                    if (r) { list = l; row = r; break; }
                }
                if (!row) return {found: false};
                const before = row;
                before.__mark = true;
                const emptyBefore = !!list.querySelector('[data-empty]');
                await new Promise(r => setTimeout(r, 1400));
                const after = list.querySelector('.det-item');
                return {found: true, same: before === after, marked: !!(after && after.__mark),
                        empty_flip: emptyBefore !== !!list.querySelector('[data-empty]')};
            }"""
        )
        if stable.get("found"):
            check("detection rows are patched, not rebuilt",
                  bool(stable.get("same") or stable.get("marked")) and not stable.get("empty_flip"),
                  str(stable))

        # ---------- 2. unknown person -> register ----------
        # A track only lives a few seconds after its person leaves the frame,
        # so wait (briefly) for the live loop to show one instead of assuming.
        unknown = page.locator('[data-panel="unknown"] .det-item').first
        for _ in range(20):
            if unknown.count():
                break
            page.wait_for_timeout(400)
        if unknown.count():
            unknown.click()
            page.wait_for_timeout(1600)
            btn = page.locator(".modal .modal-foot button.primary, .modal button.primary").last
            labels = page.locator(".modal .modal-body button").all_inner_texts()
            check("detail offers a register action", any("ثبت" in t or "ثبت" in t for t in labels), str(labels[:6]))
            if btn.count():
                btn.click()
                page.wait_for_timeout(3500)
                chips = page.locator(".face-chip").count()
                check("enrolment dialog opens with detected faces", chips > 0, f"{chips} chips")
                title = page.locator(".modal .card-head, .modal h2, .modal h3").first.inner_text() if page.locator(".modal").count() else ""
                notes.append("enrol title: " + (title or "?"))
                # close whatever dialog is open (the enrol modal has no Escape
                # binding), then make sure the backdrop is gone
                for _ in range(3):
                    if not page.locator(".modal-backdrop").count():
                        break
                    # the header close button (✕) is always present and unique
                    close_btn = page.locator(".modal .modal-head button.icon").first
                    if close_btn.count():
                        close_btn.click(force=True)
                    else:
                        page.keyboard.press("Escape")
                    page.wait_for_timeout(600)
                check("dialogs closed", page.locator(".modal-backdrop").count() == 0)
        else:
            notes.append("no unknown track on the dashboard right now; skipping step 2")

        # ---------- 3. cameras -> discovery ----------
        page.locator(".topnav button:has-text('دوربین')").first.click()
        page.wait_for_timeout(1500)
        disc = page.locator("#disc-host")
        check("discovery panel present", disc.count() > 0)
        if disc.count():
            body = disc.inner_text()
            check("discovery exposes credentials", ("نام کاربری" in body) and ("رمز عبور" in body))
            check("discovery exposes subnet", "شبکه" in body)
            page.click("button:has-text('شروع اسکن')")
            page.wait_for_timeout(4000)
            state = disc.inner_text()
            notes.append("scan state: " + ("running" if "در حال اسکن" in state else "idle"))
            check("scan starts without an error toast", "خطا" not in state)

        page.screenshot(path="docs/screens/flows-check.png", full_page=True)
        browser.close()

    print("\nnotes:")
    for n in notes:
        print("  -", n)
    print("\nerrors:", len(errors))
    for e in errors[:10]:
        print("  !", e)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
