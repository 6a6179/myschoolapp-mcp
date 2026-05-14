"""Refresh the myschoolapp session cookie via Microsoft OAuth + Playwright.

Adapted from https://github.com/6a6179/myschoolapp-shit (auth.py).

Run with:
    myschoolapp-mcp-refresh

Or:
    python -m myschoolapp_mcp.auth

Required env (loaded from .env if python-dotenv is installed):
    MSA_SUBDOMAIN (or SCHOOL_SUBDOMAIN) — your school's subdomain
    SCHOOL_EMAIL — Blackbaud / Microsoft login email
    SCHOOL_PASS  — Microsoft login password

Optional:
    HEADLESS=True|False (default False; set True for automation)
    TIMEOUT=30000  (ms)
    MSA_COOKIES_FILE — output path (default ~/.myschoolapp-mcp/cookie.txt)

Note: 2FA / MFA accounts are not supported by this flow.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .client import default_cookie_path, load_env_file


def _cookie_output_path() -> Path:
    override = os.environ.get("MSA_COOKIES_FILE")
    return Path(override) if override else default_cookie_path()


def refresh_cookie() -> Path:
    load_env_file()

    subdomain = os.environ.get("MSA_SUBDOMAIN") or os.environ.get("SCHOOL_SUBDOMAIN")
    email = os.environ.get("SCHOOL_EMAIL")
    password = os.environ.get("SCHOOL_PASS")
    headless = os.environ.get("HEADLESS", "True").lower() == "true"
    timeout_ms = int(os.environ.get("TIMEOUT", "30000"))

    if not subdomain or not email or not password:
        raise RuntimeError(
            "Missing config. Set MSA_SUBDOMAIN (or SCHOOL_SUBDOMAIN), "
            "SCHOOL_EMAIL, SCHOOL_PASS — in your environment or .env file."
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from e

    login_url = f"https://{subdomain}.myschoolapp.com/app?svcid=edu#login"
    cookie_path = _cookie_output_path()
    cookie_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
                "Gecko/20100101 Firefox/147.0"
            )
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        print("--- Refreshing cookie ---", file=sys.stderr)
        page.goto(login_url)

        try:
            page.wait_for_selector('input[type="text"], input[type="email"]')
            page.fill('input[type="text"], input[type="email"]', email)
            page.get_by_text("Next", exact=True).click()
        except Exception as e:
            print(f"Warning at email step: {e}", file=sys.stderr)

        try:
            page.wait_for_selector('input[name="passwd"]', state="visible")
            time.sleep(1)
            page.fill('input[name="passwd"]', password)
            page.click('input[type="submit"]')
        except Exception as e:
            print(f"Failed at password step: {e}", file=sys.stderr)
            browser.close()
            raise

        try:
            page.wait_for_selector('input[id="idSIButton9"]', timeout=5000)
            page.click('input[id="idSIButton9"]')
        except Exception:
            pass  # "Stay signed in" prompt not shown

        try:
            page.wait_for_url("**/app/**")
        except Exception as e:
            print(
                f"Timed out waiting for homepage. Last URL: {page.url}",
                file=sys.stderr,
            )
            browser.close()
            raise RuntimeError("Login flow did not reach the app homepage.") from e

        print("Login success.", file=sys.stderr)

        cookies = context.cookies()
        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        cookie_path.write_text(cookie_string, encoding="utf-8")
        print(f"Saved {len(cookies)} cookies to {cookie_path}", file=sys.stderr)

        browser.close()

    return cookie_path


def main() -> None:
    try:
        refresh_cookie()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
