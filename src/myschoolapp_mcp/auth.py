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
    HEADLESS=True|False (default True; set False to watch the login)
    TIMEOUT=30000  (ms)
    MSA_COOKIES_FILE — output path (default ~/.myschoolapp-mcp/cookie.txt)

Note: 2FA / MFA accounts are not supported by this flow.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

from .client import default_cookie_path, load_env_file


def _cookie_output_path() -> Path:
    override = os.environ.get("MSA_COOKIES_FILE")
    return Path(override) if override else default_cookie_path()


def _write_private(path: Path, text: str) -> None:
    """Write the cookie file with 0600 perms — it's a full session credential."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)  # O_CREAT mode only applies to new files


def refresh_cookie() -> Path:
    load_env_file()

    subdomain = os.environ.get("MSA_SUBDOMAIN") or os.environ.get("SCHOOL_SUBDOMAIN")
    email = os.environ.get("SCHOOL_EMAIL")
    password = os.environ.get("SCHOOL_PASS")
    headless = os.environ.get("HEADLESS", "True").lower() == "true"
    try:
        timeout_ms = int(os.environ.get("TIMEOUT", "30000"))
    except ValueError:
        raise RuntimeError(
            "TIMEOUT must be an integer number of milliseconds."
        ) from None

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
    with contextlib.suppress(OSError):
        os.chmod(cookie_path.parent, 0o700)

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

        email_step_error: str | None = None
        try:
            page.wait_for_selector('input[type="text"], input[type="email"]')
            page.fill('input[type="text"], input[type="email"]', email)
            page.get_by_text("Next", exact=True).click()
        except Exception as e:
            # Sometimes Microsoft skips straight to the password prompt, so
            # this isn't necessarily fatal — but remember it in case the
            # password step fails too.
            email_step_error = str(e)
            print(f"Warning at email step: {e}", file=sys.stderr)

        try:
            page.wait_for_selector('input[name="passwd"]', state="visible")
            time.sleep(1)
            page.fill('input[name="passwd"]', password)
            page.click('input[type="submit"]')
        except Exception as e:
            msg = f"Failed at password step: {e}"
            if email_step_error:
                msg += f" (the email step had already failed: {email_step_error})"
            print(msg, file=sys.stderr)
            browser.close()
            raise RuntimeError(msg) from e

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

        # Let the browser apply domain/path/secure cookie matching for the
        # school origin; identity-provider cookies must stay in the browser.
        cookies = context.cookies([f"https://{subdomain}.myschoolapp.com"])
        if not cookies:
            browser.close()
            raise RuntimeError("No cookies applicable to the school origin were found.")
        print("Login success.", file=sys.stderr)
        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        _write_private(cookie_path, cookie_string)
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
