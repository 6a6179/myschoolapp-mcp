"""Authenticated HTTP client for myschoolapp.com."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_EXPIRED_COOKIE_HINT = (
    "Got an HTML page instead of JSON — the session cookie is probably "
    "expired or invalid. Run the cookie_refresh tool (or "
    "`myschoolapp-mcp-refresh`) and retry."
)


class MyschoolappClient:
    def __init__(
        self,
        subdomain: str | None = None,
        cookies: dict[str, str] | str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.subdomain = subdomain or os.environ.get("MSA_SUBDOMAIN")
        if not self.subdomain:
            raise RuntimeError(
                "Missing subdomain. Set MSA_SUBDOMAIN env var (e.g. 'myschool' "
                "for myschool.myschoolapp.com)."
            )
        self.base_url = f"https://{self.subdomain}.myschoolapp.com"
        self._host = f"{self.subdomain}.myschoolapp.com".lower()

        cookie_dict = _load_cookies(cookies)

        # Scope every cookie to the school host. A plain dict jar in httpx
        # is domain-unscoped and would happily be sent to any absolute URL.
        jar = httpx.Cookies()
        for name, value in cookie_dict.items():
            jar.set(name, value, domain=self._host)

        self._client = httpx.Client(
            base_url=self.base_url,
            cookies=jar,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 myschoolapp-mcp/0.1"
                ),
                "Referer": f"{self.base_url}/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def _resolve_path(self, path: str) -> str:
        return resolve_request_path(path, self._host)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        path = self._resolve_path(path)

        try:
            resp = self._client.request(
                method.upper(),
                path,
                params=params,
                json=json_body,
                data=data,
                headers=extra_headers,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Request to {path} failed: {type(e).__name__}: {e}"
            ) from e

        out: dict[str, Any] = {
            "status": resp.status_code,
            "url": str(resp.url),
        }
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                out["body"] = resp.json()
            except json.JSONDecodeError:
                out["body"] = resp.text[:50_000]
                out["error"] = True
                out["message"] = "Response claimed to be JSON but failed to parse."
        else:
            text = resp.text
            head = text[:300].lstrip().lower()
            is_html = "text/html" in ct or head.startswith(("<!doctype", "<html"))
            if is_html and resp.status_code < 400:
                # A 200 HTML page from an /api/ endpoint means we got bounced
                # to the login page: the session is dead. Don't bury that in
                # 50 KB of markup.
                out["body"] = text[:2_000]
                out["error"] = True
                out["hint"] = _EXPIRED_COOKIE_HINT
            elif len(text) > 50_000:
                out["body"] = text[:50_000]
                out["truncated"] = True
            else:
                out["body"] = text
            out["content_type"] = ct
        if resp.status_code >= 400:
            out["error"] = True
        return out

    def close(self) -> None:
        self._client.close()


def resolve_request_path(path: str, allowed_host: str) -> str:
    """Normalize a request path, refusing to leak the session off-site.

    Relative paths pass through (with a leading slash added). Absolute and
    protocol-relative URLs are only allowed when they point at the school's
    own host over https — anything else (other hosts, plain http) would
    expose the session cookie.
    """
    path = path.strip()
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        if parts.scheme not in ("", "https") or (
            parts.netloc.lower() != allowed_host
        ):
            raise ValueError(
                f"Refusing to send the session cookie to {path!r}: only "
                f"https://{allowed_host} is allowed. Use a path like '/api/...'."
            )
        return path
    if not path.startswith("/"):
        return "/" + path
    return path


def default_cookie_path() -> Path:
    """Default location for the cookie file produced by the refresh script."""
    return Path.home() / ".myschoolapp-mcp" / "cookie.txt"


def load_env_file() -> Path | None:
    """Find and load .env from several reasonable locations.

    Order: $MSA_ENV_FILE, cwd, ~/.myschoolapp-mcp/.env, then walk up from
    this file's location looking for a project root (a directory containing
    pyproject.toml). The walk is capped at a few levels so a pip-installed
    package doesn't scan all the way to /.

    Returns the loaded path, or None if python-dotenv is unavailable or no
    .env file was found.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    candidates: list[Path] = []
    override = os.environ.get("MSA_ENV_FILE")
    if override:
        candidates.append(Path(override))
    with contextlib.suppress(OSError):
        candidates.append(Path.cwd() / ".env")
    candidates.append(Path.home() / ".myschoolapp-mcp" / ".env")
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:4]:
        candidates.append(parent / ".env")
        if (parent / "pyproject.toml").exists():
            break

    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if p.exists():
            load_dotenv(p, override=False)
            return p
    return None


def _load_cookies(cookies: dict[str, str] | str | None) -> dict[str, str]:
    if isinstance(cookies, dict):
        return cookies
    if isinstance(cookies, str):
        parsed = _parse_cookie_header(cookies)
        if not parsed:
            raise RuntimeError("Cookie string contained no name=value pairs.")
        return parsed

    raw = os.environ.get("MSA_COOKIE")
    if raw:
        parsed = _parse_cookie_header(raw)
        if not parsed:
            raise RuntimeError("MSA_COOKIE contained no name=value pairs.")
        return parsed

    path_str = os.environ.get("MSA_COOKIES_FILE")
    path = Path(path_str) if path_str else default_cookie_path()
    if path.exists():
        return _load_cookies_from_file(path)

    raise RuntimeError(
        "No cookies provided. Either:\n"
        "  - Set MSA_COOKIE (raw 'k=v; k2=v2' header), or\n"
        "  - Set MSA_COOKIES_FILE to a cookies file "
        "(JSON / Netscape / raw header), or\n"
        "  - Run `myschoolapp-mcp-refresh` to generate "
        f"{default_cookie_path()}."
    )


def _parse_cookie_header(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def _load_cookies_from_file(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Cookie file {path} is empty.")

    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Cookie file {path} looks like JSON but failed to parse: {e}"
            ) from e
        if isinstance(data, dict):
            out = {str(k): str(v) for k, v in data.items()}
        elif isinstance(data, list):
            # Browser-export format: [{"name": "...", "value": "...", ...}, ...]
            out = {
                str(entry["name"]): str(entry["value"])
                for entry in data
                if isinstance(entry, dict) and "name" in entry and "value" in entry
            }
        else:
            raise RuntimeError(
                f"Unsupported cookie JSON shape in {path}: {type(data).__name__}"
            )
    elif "\t" in text:
        # Netscape cookies.txt: domain/flag/path/secure/expiry/name/value
        out = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
    else:
        # Raw Cookie header format: "name=value; name2=value2; ..."
        # (matches the cookie.txt output of the refresh script)
        out = _parse_cookie_header(text)

    if not out:
        raise RuntimeError(f"No cookies could be parsed from {path}.")
    return out
