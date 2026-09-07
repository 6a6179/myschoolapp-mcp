# myschoolapp-mcp

An MCP (Model Context Protocol) server for the Blackbaud K-12 platform
that lives at `*.myschoolapp.com`. It uses your existing browser session
cookie to call the same private JSON API that the SPA frontend calls,
so anything you can see on the site, your LLM can see.

Built for use with [Claude Code](https://claude.com/claude-code),
[Claude Desktop](https://claude.ai/download), and any other MCP-aware
client.

> **Unofficial.** Not affiliated with Blackbaud. Endpoints were
> reverse-engineered from network traffic on one school's deployment;
> some IDs (category IDs, duration IDs, directory IDs) are
> school-specific. If you hit a missing or different endpoint, use the
> `api_request` escape hatch.

## Features

About 29 typed tools covering the common student/parent surfaces:

- **Core** — `whoami`, `config`, `cookie_refresh`
- **Assignments** — `assignments` (bucketed, compact by default),
  `assignments_in_range`, `missing_assignments`, `assignment_detail`
  (single assignment with downloads / submitted files / rubric),
  `assignment_options`, `assignment_status_labels`
- **Schedule** — `schedule` (compact per-block view by default),
  `daily_announcement`
- **Academics** — `student_terms`, `classes`, `gradebook` (both return
  compact per-class views and auto-resolve the current academic term if
  you don't pass a `duration_id`; gradebook returns both the current
  marking-period and year-to-date grade per class),
  `report_card_templates`, `transcript_templates`, `attendance`,
  `conduct`, `grade_levels`
- **Groups** — `group_membership` (advisory / athletic / dorm /
  activity / community)
- **Calendar** — `calendar_list`, `calendar_actions`
- **Inbox / news** — `official_notes`, `official_note_types`,
  `activity_feed`
- **Directory** — `directory_search` (compact rows, capped at a `limit`
  so an empty query can't dump the whole school into your context),
  `directory_info`, `directory_facets`
- **Escape hatch** — `api_request` for anything else. Requests are
  pinned to your school's own host — absolute URLs pointing anywhere
  else are rejected so the session cookie can't leak.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/6a6179/myschoolapp-mcp.git
cd myschoolapp-mcp
pip install -e .
python -m playwright install chromium  # only needed for auto cookie refresh
```

## Configure

Copy the template and fill it in:

```bash
cp .env.example .env
```

You need at least:

- `MSA_SUBDOMAIN` — the bit before `.myschoolapp.com`
- `MSA_STUDENT_ID` — your numeric persona user id. Find it in DevTools >
  Network on any `/api/user/profiletabs?showuserid=<this-number>`
  request after logging in to the site.
- A session cookie — see below.

Optional:

- `MSA_PERSONA_ID` — 2 = student (default), 3 = parent.
- `MSA_SCHOOL_YEAR` — auto-derived from today's date if unset
  (e.g. `2025 - 2026`).

### Cookie

Three options, checked in this order:

1. `MSA_COOKIE` — raw header string like
   `t=...; ASP.NET_SessionId=...; G_BB_=...`
2. `MSA_COOKIES_FILE` — path to a cookie file (JSON, Netscape
   `cookies.txt`, or raw header)
3. `~/.myschoolapp-mcp/cookie.txt` — the default location written by
   the built-in refresh script

The easiest path is option 3: fill in `SCHOOL_EMAIL` / `SCHOOL_PASS` in
your `.env` and run:

```bash
myschoolapp-mcp-refresh
```

This drives Microsoft OAuth login via Playwright and saves the
resulting cookies. **2FA / MFA accounts are not supported by this
flow.** When the cookie expires (typically every few weeks), just run
it again — or call the `cookie_refresh` tool from your MCP client, which
does the same thing and then drops the cached HTTP client so subsequent
calls pick up the fresh cookie.

If 2FA is on, log in to the site in a normal browser, export your
cookies with any standard cookie-export extension, and point
`MSA_COOKIES_FILE` at the result.

**Security note:** the cookie file is a full session credential and
`.env` contains your actual password. The refresh script writes
`cookie.txt` with `0600` permissions (and the containing directory
`0700`); if you create either file by hand, `chmod 600` it yourself.

## Register with an MCP client

### Claude Code

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "myschoolapp": {
      "command": "myschoolapp-mcp"
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json` (location depends on your OS):

```json
{
  "mcpServers": {
    "myschoolapp": {
      "command": "myschoolapp-mcp"
    }
  }
}
```

The server looks for `.env` in this order: `$MSA_ENV_FILE`, current
working directory, `~/.myschoolapp-mcp/.env`, then walking up from the
installed package to the project root. If your client launches it from
an unrelated working directory, copy your `.env` to
`~/.myschoolapp-mcp/.env`.

## Report cards

`report_card_templates(school_year="2025 - 2026")` selects a school-year
label explicitly. Omit `school_year` (or pass `None`) to keep the existing
default: `MSA_SCHOOL_YEAR`, or the school year derived from today's date.
The same label is used for both requests below.

The tool first calls `/api/Grading/StudentReportCardTemplateList`. Only
an error-free 2xx response with body exactly `[]` falls back to the
website's legacy `/api/datadirect/ParentStudentUserPerformance/` endpoint.
A successful legacy list is filtered to `performance_type == "Report"`;
the `status`, `url`, and other wrapper fields are retained, with
`source: "legacy"` added. Legacy entries retain their original fields
and IDs; those IDs are not modern report-card template IDs.

Nonempty modern results remain unchanged. Errors and non-list responses
from either endpoint are returned unchanged; modern errors never trigger
a fallback. This lists report metadata, not report document contents.

## Caveats

- Some tools return school-specific IDs (`categoryId` for official
  notes, `directoryId` for directories). The defaults in this repo
  match Tabor Academy; yours may differ. Open DevTools and check.
- `calendar_list` returns the user's calendar *definitions* (names,
  colors, filters) — not events, despite taking a date range. That's
  what the underlying endpoint actually does. Use `schedule` /
  `assignments` for day-to-day items.
- The `assignments` and `classes` tools strip heavy fields (HTML course
  descriptions, photo metadata, the full historical assignment bucket)
  by default to stay within token budgets. Pass `full=True` for the raw
  response.
- The Microsoft OAuth selectors in `auth.py` can break if Microsoft
  changes the login UI. If it stops working, set `HEADLESS=False` in
  `.env` and watch what happens.
- Read-only by design. The tools that exist all map to GET endpoints.
  Use `api_request` if you want to POST something, but consider whether
  you really want an LLM submitting forms on your behalf.

## Development

```bash
pip install -e '.[dev]'
ruff check src tests
pytest
```

The tests cover the pure formatting/parsing helpers and the client's
cookie parsing and URL gating — no live session needed.
`scripts/probe_api.py` opens a headed browser and streams every `/api/`
request the SPA makes to `scripts/capture.jsonl`; that's how these
endpoints were mapped in the first place.

## Credits

Cookie refresh approach adapted from
[6a6179/myschoolapp-shit](https://github.com/6a6179/myschoolapp-shit).

## License

MIT. See [LICENSE](LICENSE).
