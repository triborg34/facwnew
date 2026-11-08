"""Service entry point: ``python -m facw.server``"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from facw.core.app import App  # noqa: E402
from facw.core.logging_setup import get_logger  # noqa: E402
from facw.web.server import bootstrap_admin, create_app  # noqa: E402

log = get_logger("server")


def _open_browser_when_ready(url: str, timeout: float = 30.0) -> None:
    """Wait until the console answers, then open the default browser.

    Runs on a daemon thread so start-up is never blocked.  A failure to open a
    browser (headless server, no desktop session) is logged and ignored: the
    service is the product, the browser is a convenience.
    """
    deadline = time.time() + timeout
    probe = url + "/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.35)
    else:
        log.warning("console did not become ready; not opening a browser", extra={"url": url})
        return

    try:
        import webbrowser

        if webbrowser.open(url, new=2):
            log.info("console opened in the default browser", extra={"url": url})
        else:
            log.info("no browser available; open the URL manually", extra={"url": url})
    except Exception as exc:  # noqa: BLE001 - never fatal
        log.warning("could not open a browser", extra={"url": url, "error": str(exc)})


def main() -> None:
    app_state = App()
    bootstrap_admin(app_state)
    api = create_app(app_state)
    port = app_state.settings.get().port
    url = f"http://127.0.0.1:{port}"
    print(f"FACW console:  {url}")

    # FACW_OPEN_BROWSER=0 disables this (handy for servers and for the tests).
    if os.environ.get("FACW_OPEN_BROWSER", "1").strip().lower() not in ("0", "false", "no", "off"):
        threading.Thread(
            target=_open_browser_when_ready, args=(url,), name="open-browser", daemon=True
        ).start()

    uvicorn.run(api, host=os.environ.get("FACW_HOST", "0.0.0.0"), port=port, log_level="warning")


if __name__ == "__main__":
    main()
