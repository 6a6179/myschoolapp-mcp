# myschoolapp-mcp

An MCP (Model Context Protocol) server for Blackbaud K-12 schools at
`*.myschoolapp.com`. Access assignments, grades, schedules, calendar events,
report listings, and directories through **32 MCP tools**. The server uses
your session cookie to call the website's private JSON APIs, subject to
your account's permissions and the endpoints implemented here.

Works with [Hermes Agent](https://hermes-agent.nousresearch.com/docs/),
[Codex](https://github.com/openai/codex),
[OpenClaw](https://docs.openclaw.ai/),
[Claude Code](https://claude.com/claude-code), and any other MCP-aware
client that supports stdio servers.

The typed school-data tools are read-only. Cookie refresh writes a local
session file; the generic `api_request` tool also supports write methods.

> **Unofficial.** Not affiliated with Blackbaud. Endpoints were
> reverse-engineered from network traffic on one school's deployment;
> some IDs (category IDs, duration IDs, directory IDs) are
> school-specific. If you hit a missing or different endpoint, use the
> `api_request` escape hatch.

## Features

All 32 tools, grouped by purpose:

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
  marking-period and year-to-date grades when available),
  `report_card_templates`, `transcript_templates`, `attendance`,
  `conduct`, `grade_levels`, `school_years` (exact enrolled/available year labels)
- **Groups** — `group_membership` (advisory / athletic / dorm /
  activity / community)
- **Calendar** — `calendar_list`, `calendar_actions`, `calendar_events`
  (read school, group, and athletic events without saving preferences)
- **Inbox / news** — `official_notes`, `official_note_types`,
  `activity_feed`
- **Directory** — `directory_list` (available directory IDs and names),
  `directory_search` (compact rows, capped at a `limit`
  so an empty query can't dump the whole school into your context),
  `directory_info`, `directory_facets`
- **Escape hatch** — `api_request` for anything else. Requests are
  pinned to your school's own HTTPS host, including every redirect.
  Off-site, subdomain, and HTTP destinations are rejected before sending
  a request; loaded session cookies are marked Secure.

## Install

Requires Python 3.10+ and an authenticated school account. The commands
below use a virtual environment on Linux/macOS; on Windows, activate
`.venv\Scripts\Activate.ps1` instead.

```bash
git clone https://github.com/6a6179/myschoolapp-mcp.git
cd myschoolapp-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Only automatic cookie refresh needs a Playwright browser installation:

```bash
python -m playwright install chromium
```

If you import an existing browser cookie instead, skip that step. Browser
system dependencies may also be needed on minimal Linux installations.

## Configure

Copy the template and fill it in:

```bash
cp .env.example .env
chmod 600 .env  # Linux/macOS
```

Edit `.env` locally. Do not paste passwords or session cookies into chat
or commit them to Git. Set `MSA_TIMEZONE` to your **school's** timezone,
not necessarily the timezone of your computer or server.

You need at least:

- `MSA_SUBDOMAIN` — the bit before `.myschoolapp.com`
- `MSA_STUDENT_ID` — your numeric persona user id. Find it in DevTools >
  Network on any `/api/user/profiletabs?showuserid=<this-number>`
  request after logging in to the site.
- A session cookie — see below.

Optional:

- `MSA_PERSONA_ID` — 2 = student (default), 3 = parent.
- `MSA_SCHOOL_YEAR` — auto-derived from the school-local date if unset
  (e.g. `2025 - 2026`).
- `MSA_TIMEZONE` — IANA timezone used for today's date, default date
  ranges, assignment due-date buckets, and school-year inference. Defaults
  to `UTC`; set, for example, `America/New_York` for a school in that
  timezone. `config()` includes the resolved `timezone`. Invalid or
  unavailable timezone names raise a `ValueError` identifying
  `MSA_TIMEZONE`; use a name available in the host's IANA timezone data.
  Explicit date arguments remain unchanged. `schedule()` and
  `daily_announcement()` send the school-local date when no date is given.

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
does the same thing and then replaces the cached HTTP client so subsequent
calls pick up the fresh cookie. The tool uses the newly written file even
when `MSA_COOKIE` was set; a failed refresh retains the prior cached session
and cookie configuration. A later server restart uses the precedence
listed above again, so remove or update a stale `MSA_COOKIE` in your launch
configuration. The export includes only cookies applicable to the school's
HTTPS origin, using Playwright's domain-aware selection.

If 2FA is on, log in to the site in a normal browser, export your
cookies with any standard cookie-export extension, and point
`MSA_COOKIES_FILE` at the result.

#### Automatic re-login on an expired session (opt-in)

Sessions on some deployments die within hours. Every response that
means "session dead" — a JSON 403 with
`ErrorType: INVALID_AUTHORIZATION` or an HTML login page — now carries
`auth_expired: true` plus a `hint`. Set

```bash
MSA_AUTO_REFRESH=true
```

(with `SCHOOL_EMAIL` / `SCHOOL_PASS` present) and the server will run the
Playwright login **once**, swap in the new cookies, and retry the same
request transparently; the result then includes `auto_refreshed: true`.
A cooldown of 60 s between attempts stops a broken login flow from
looping. Failures are reported in `auto_refresh_error` rather than
raised. `config()` shows `auto_refresh` so you can tell which mode is
active. Off by default because it means a tool call can start a
password login without anyone asking.

#### `api_request` write gate

Every typed tool is read-only. `api_request` accepts only
`GET`/`HEAD`/`OPTIONS` unless the server is started with
`MSA_ALLOW_WRITES=true`; other methods raise before any request is
sent. `config()` reports `api_request_writes`.

**Security note:** the cookie file is a full session credential and
`.env` contains a login password if you configured automatic refresh.
The refresh script writes
`cookie.txt` with `0600` permissions (and the containing directory
`0700`); if you create either file by hand, `chmod 600` it yourself.

## Register with an MCP client

Use absolute paths to the installed executable and `.env`. A desktop app
or gateway does not necessarily inherit your activated virtual environment
or start in the repository directory. Replace `/absolute/path/to` below
with your actual installation path.

### Hermes Agent

After configuring authentication:

```bash
hermes mcp add myschoolapp \
  --command /absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp \
  --env MSA_ENV_FILE=/absolute/path/to/myschoolapp-mcp/.env
hermes mcp test myschoolapp
```

The add command prompts for which tools to enable. A successful test
confirms the connection and tool discovery; call `whoami` to check the
school session and `config` to check the resolved year/timezone.

For an already-running Hermes session, send `/reload-mcp` after adding
the server or updating its source/configuration. This reconnects MCP
servers without restarting the gateway. A separate CLI test does not
refresh the running chat's connection.

### Claude Code

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "myschoolapp": {
      "command": "/absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp",
      "env": {
        "MSA_ENV_FILE": "/absolute/path/to/myschoolapp-mcp/.env"
      }
    }
  }
}
```

### Codex

With [Codex CLI](https://github.com/openai/codex) installed and the school's
authentication configured in `.env`, register the stdio server:

```bash
codex mcp add myschoolapp \
  --env MSA_ENV_FILE=/absolute/path/to/myschoolapp-mcp/.env \
  -- /absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp
codex mcp list
```

Alternatively, add the equivalent configuration to `~/.codex/config.toml`:

```toml
[mcp_servers.myschoolapp]
command = "/absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp"

[mcp_servers.myschoolapp.env]
MSA_ENV_FILE = "/absolute/path/to/myschoolapp-mcp/.env"
```

Start a new Codex session and use `/mcp` to inspect the active connection,
then ask it to call `whoami` to verify school access. `codex mcp list`
shows saved configuration, not a successful school login. The Codex IDE
extension shares this configuration; restart the extension after editing
it. See the [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
for more client options.

### OpenClaw

On an OpenClaw version with native MCP client support, register the server
after configuring the school's authentication in `.env`:

```bash
openclaw mcp add myschoolapp \
  --command /absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp \
  --env MSA_ENV_FILE=/absolute/path/to/myschoolapp-mcp/.env
openclaw mcp doctor myschoolapp --probe
```

Alternatively, merge this into `~/.openclaw/openclaw.json`, preserving your
other settings and servers:

```json
{
  "mcp": {
    "servers": {
      "myschoolapp": {
        "command": "/absolute/path/to/myschoolapp-mcp/.venv/bin/myschoolapp-mcp",
        "env": {
          "MSA_ENV_FILE": "/absolute/path/to/myschoolapp-mcp/.env"
        }
      }
    }
  }
}
```

The probe checks server startup and MCP capabilities, not school login;
ask the agent to call `whoami` to verify authenticated access. Restart the
running OpenClaw gateway/agent after configuration changes as needed:
`openclaw mcp reload` only clears caches in its own CLI process, not another
running gateway. The `coding` and `messaging` tool profiles expose MCP
tools; `minimal` or an explicit `bundle-mcp` deny rule can hide them.

These instructions use OpenClaw's native `mcp.servers` registry, not
mcporter's separate configuration. They follow the
[official MCP documentation](https://docs.openclaw.ai/cli/mcp);
OpenClaw-specific end-to-end testing is not part of the verification
snapshot below.

### Environment-file lookup

The server looks for `.env` in this order: `$MSA_ENV_FILE`, current
working directory, `~/.myschoolapp-mcp/.env`, then walking up from the
installed package to the project root. If your client launches it from
an unrelated working directory, set `MSA_ENV_FILE` explicitly as above
or copy your `.env` to `~/.myschoolapp-mcp/.env`. On Windows, point the
client to `.venv/Scripts/myschoolapp-mcp.exe` and use your absolute paths.

## Example requests

Ask your MCP client naturally:

- "Show assignments due today and tomorrow, including their status."
- "Show my current grades; distinguish unavailable grades from zeroes."
- "What school events are on the calendar this week?"
- "List the directories I can search."
- "Which school years are available, and what reports can I list for one?"

Equivalent tool-call examples (not shell commands):

```text
assignments(buckets="DueToday,DueTomorrow")
gradebook()
calendar_events(date_start="2026-09-07", date_end="2026-09-14")
directory_list()
school_years()
```

Use returned directory IDs with `directory_search`, and exact returned
year labels with the historical-year arguments described below.

## School years and directories

`school_years()` returns de-duplicated, exact `SchoolYearLabel` strings
from `grade_levels()`, with the source response metadata. These are the
student's enrolled/available year labels and can include historical,
current, and future years; strings are not parsed or normalized.

Pass `school_year="2025 - 2026"` to `student_terms`, `classes`, `gradebook`,
`attendance`, `conduct`, `group_membership`, `transcript_templates`, or
`report_card_templates` to query that year without changing configuration.
Omitting it keeps the configured/inferred default. `classes`, `gradebook`,
and `group_membership` resolve durations within the requested year. If
that year has no current term, use `student_terms(school_year=...)` and
pass an explicit `duration_id`; the tools do not guess a historical term.
Community membership keeps its school-wide duration of `0` without a term lookup.

`directory_list()` reads `/api/webapp/context` and returns only its
`Directories` entries plus response metadata. Entries retain
`DirectoryID`, `SortOrder`, and `DirectoryName`; use those IDs with
`directory_search`, `directory_info`, and `directory_facets`. It does not
fetch directory members or return the rest of the session context.

## Calendar events

`calendar_events(date_start="2026-09-07", date_end="2026-09-14")` reads
school, group, and athletic events using the currently selected supported
calendar filters. Dates must be exact `YYYY-MM-DD`; they are sent unchanged,
without assuming an inclusive/exclusive end-date adjustment.

For an explicit request-only selection, pass child `CalendarId` values from
`calendar_list()` as `calendar_ids`. `None` uses selected filters; `[]`
returns no events. `include_practice=True` includes practice events.
This calls the UI's events read POST and never persists filter preferences.
It excludes assignment, class-schedule, and admissions-calendar routes.

Compact output preserves local timestamp strings and event links and merges
duplicate event groups. `count` is the compact count; `raw_count` is the
original row count. `full=True` preserves the raw event rows instead.
Upstream failures retain their status/error information.

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

- `assignment_detail` retains failed detail responses under `detail`.
  Rubric failures retain their response wrappers under `rubric.raw` or
  `rubric.results_error`, alongside any successful assignment/rubric
  components. Nonpositive rubric IDs skip rubric requests.
- `gradebook` reports per-section `error` and `hydrate_error` wrappers
  when hydration fails or the requested student cannot be found. It
  retains other classes and marks `hydration_verified` only after finding
  the requested student. Aggregate status is `207` for partial results
  and `502` when all returned sections fail, with `error` and `partial`
  fields. Fallback class-list grades remain unverified. Hydrated current
  zero grades remain `0.00%`; a class-list zero with no display remains
  the existing no-grade placeholder. `SectionGradeYear=0` is used by
  this endpoint for unpublished year-to-date grades and remains null.
  Historical class lists can omit marking-period IDs; those rows cannot be
  hydrated and `graded=False` means no usable gradebook identifiers were
  returned, not proof that the historical class was ungraded. Use report
  cards for published historical results when this endpoint supplies no grades.
- Some tools use school-specific IDs (`categoryId` for official
  notes, `directoryId` for directories). The defaults in this repo
  match Tabor Academy; yours may differ. Use `directory_list()` for
  directory IDs and DevTools for other IDs.
- `calendar_list` returns the user's calendar *definitions* (names,
  colors, filters) — not events, despite taking a date range. That's
  what the underlying endpoint actually does. Use `calendar_events` for
  school/group/athletic events; `schedule` / `assignments` remain separate.
- The `assignments` and `classes` tools strip heavy fields (HTML course
  descriptions, photo metadata, the full historical assignment bucket)
  by default to stay within token budgets. Pass `full=True` for the raw
  response.
- The Microsoft OAuth selectors in `auth.py` can break if Microsoft
  changes the login UI. If it stops working, set `HEADLESS=False` in
  `.env` and watch what happens.
- Typed school-data tools are read-only. `calendar_events` uses the site's
  read POST without creating events or saving preferences. `cookie_refresh`
  signs in and writes a local cookie file. The `api_request` escape hatch
  is limited to read methods unless `MSA_ALLOW_WRITES=true`.
- `assignments` fetches `days_ahead` days forward (default 60), so
  `DueAfterNextWeek` only covers that horizon; the response's `window`
  field shows the exact range. `gradebook` reports a synthetic `status`
  (207 partial / 502 all failed, marked `status_source: synthetic`) that
  is not an HTTP status from the school.

## Development

With the virtual environment activated:

```bash
python -m pip install -e '.[dev]'
python -m ruff check src tests
python -m pytest -q
```

The tests use synthetic fixtures, mocked server responses, HTTPX
`MockTransport`, and a fake Playwright context. Server/auth imports patch
environment-file loading before import; no live session is needed.
`scripts/probe_api.py` opens a headed browser and streams every `/api/`
request the SPA makes to `scripts/capture.jsonl`; that's how these
endpoints were mapped in the first place.

### Verification snapshot

The upgrade in [`d84904b`](https://github.com/6a6179/myschoolapp-mcp/commit/d84904bb637440fdc828fed3c0b3fba90e373925)
was verified against a Tabor Academy student account:

- **449 offline tests passed**, plus Ruff and independent code review.
- **All 32 tools were invoked through real MCP stdio across 52 calls**,
  with no detected failures in that run. The executable was launched
  from outside the repository using explicit configuration.
- Calendar reads left saved calendar preferences unchanged. Login refresh
  was tested with an isolated cookie file and a stale environment-cookie
  override, followed by a successful authenticated read.
- After reloading Hermes, direct chat-tool calls verified authentication,
  configuration, all three new tools (`calendar_events`, `directory_list`,
  `school_years`), assignments, and gradebook.

This is coverage of the tested account and API paths, not a guarantee of
every school's deployment. In particular, the tested historical term
returned classes without usable gradebook marking-period IDs; numerical
historical grades could not be hydrated through that route. Historical
report-card **listing** worked. Empty endpoint results do not establish
that records are absent, and report listing does not download PDFs.

## Credits

Cookie refresh approach adapted from
[6a6179/myschoolapp-shit](https://github.com/6a6179/myschoolapp-shit).

## License

MIT. See [LICENSE](LICENSE).
