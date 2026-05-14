"""Authenticated HTTP client for myschoolapp.com."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


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

        cookie_dict = _load_cookies(cookies)

        self._client = httpx.Client(
            base_url=self.base_url,
            cookies=cookie_dict,
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

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") and not path.startswith("http"):
            path = "/" + path

        resp = self._client.request(
            method.upper(),
            path,
            params=params,
            json=json_body,
            data=data,
            headers=extra_headers,
        )

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
        else:
            text = resp.text
            if len(text) > 50_000:
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


def default_cookie_path() -> Path:
    """Default location for the cookie file produced by the refresh script."""
    return Path.home() / ".myschoolapp-mcp" / "cookie.txt"


def load_env_file() -> Path | None:
    """Find and load .env from several reasonable locations.

    Order: $MSA_ENV_FILE, cwd, ~/.myschoolapp-mcp/.env, then walk up from
    this file's location until we find a .env or the project root (the
    nearest directory containing pyproject.toml).

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
    try:
        candidates.append(Path.cwd() / ".env")
    except (FileNotFoundError, OSError):
        pass
    candidates.append(Path.home() / ".myschoolapp-mcp" / ".env")
    here = Path(__file__).resolve()
    for parent in here.parents:
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
        return _parse_cookie_header(cookies)

    raw = os.environ.get("MSA_COOKIE")
    if raw:
        return _parse_cookie_header(raw)

    path_str = os.environ.get("MSA_COOKIES_FILE")
    path = Path(path_str) if path_str else default_cookie_path()
    if path.exists():
        return _load_cookies_from_file(path)

    raise RuntimeError(
        "No cookies provided. Either:\n"
        "  - Set MSA_COOKIE (raw 'k=v; k2=v2' header), or\n"
        "  - Set MSA_COOKIES_FILE to a cookies file (JSON / Netscape / raw header), or\n"
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
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            # Browser-export format: [{"name": "...", "value": "...", ...}, ...]
            return {entry["name"]: entry["value"] for entry in data if "name" in entry}
        raise RuntimeError(f"Unsupported cookie JSON shape: {type(data).__name__}")

    if "\t" in text:
        # Netscape cookies.txt: domain/flag/path/secure/expiry/name/value
        out: dict[str, str] = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
        return out

    # Raw Cookie header format: "name=value; name2=value2; ..."
    # (matches the cookie.txt output of the refresh script)
    return _parse_cookie_header(text)
