"""Headed Playwright probe to capture API traffic from a live MSA session.

Useful for reverse-engineering new endpoints: pops a browser, you log in
and click around the SPA, and every /api/ request + response (URLs, params,
JSON bodies) is streamed to scripts/capture.jsonl as JSONL.

It also writes the live cookie jar to the standard MSA cookie file every
3 seconds, so even if you force-kill the process, your refreshed cookie is
already on disk.

Usage:
    python scripts/probe_grades.py
    # ... log in, navigate, etc. ...
    # press Ctrl+C to stop.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# make repo's src importable when run from anywhere
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from myschoolapp_mcp.client import default_cookie_path, load_env_file  # noqa: E402


def main() -> None:
    load_env_file()
    subdomain = os.environ.get("MSA_SUBDOMAIN") or os.environ.get("SCHOOL_SUBDOMAIN")
    if not subdomain:
        raise SystemExit("MSA_SUBDOMAIN not set")

    from playwright.sync_api import sync_playwright

    out_path = Path(__file__).parent / "capture.jsonl"
    out_path.unlink(missing_ok=True)
    out = out_path.open("w", encoding="utf-8", buffering=1)

    def on_response(resp):
        url = resp.url
        if "/api/" not in url:
            return
        record = {
            "url": url,
            "method": resp.request.method,
            "status": resp.status,
            "post_data": resp.request.post_data,
        }
        try:
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                body = resp.text()
                record["body"] = body[:200_000]
                record["body_truncated"] = len(body) > 200_000
        except Exception as e:
            record["body_error"] = str(e)
        out.write(json.dumps(record) + "\n")
        out.flush()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
                "Gecko/20100101 Firefox/147.0"
            )
        )
        context.on("response", on_response)

        page = context.new_page()
        page.goto(f"https://{subdomain}.myschoolapp.com/app?svcid=edu#login")

        print(
            f"\n>> Browser is open. Log in, then click around as needed.\n"
            f">> Captured API calls stream to {out_path}\n"
            ">> Press Ctrl+C in this terminal when done.\n",
            flush=True,
        )

        cookie_path = Path(
            os.environ.get("MSA_COOKIES_FILE") or default_cookie_path()
        )
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        last_saved_count = 0

        def _save_cookies():
            nonlocal last_saved_count
            try:
                cookies = context.cookies()
                if not cookies:
                    return
                cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                cookie_path.write_text(cookie_string, encoding="utf-8")
                if len(cookies) != last_saved_count:
                    print(
                        f"Saved {len(cookies)} cookies to {cookie_path}",
                        flush=True,
                    )
                    last_saved_count = len(cookies)
            except Exception as e:
                print(f"Could not save cookies: {e}", flush=True)

        try:
            # Save cookies every ~3 seconds so we never lose a logged-in
            # session even if the process is force-killed.
            tick = 0
            while True:
                page.wait_for_timeout(1000)
                tick += 1
                if tick % 3 == 0:
                    _save_cookies()
        except KeyboardInterrupt:
            print("\nStopping. Saving cookies + capture...", flush=True)
            _save_cookies()
        finally:
            out.close()
            browser.close()


if __name__ == "__main__":
    main()
