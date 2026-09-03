# agent-statusline

## Local pre-commit gate (ruff format + check)

CI's "Lint (Linux)" job runs two hard ruff gates: `ruff format --check .` and
`ruff check .`. The Claude PostToolUse hook (`.claude/settings.json`) runs
`ruff check` + aislop and now auto-applies `ruff format` on each edited `.py`,
but only during Claude sessions - manual edits and other machines bypass it.
The committed git hook at `hooks/pre-commit` is the authoritative gate: it
re-runs both CI ruff commands and blocks the commit on any finding, so an
unformatted file can never reach CI again (it slipped through twice before:
PR #7 burnrate.py, then verify_cache_cost_split.py - both format-only).

`core.hooksPath` is per-clone local config and is NOT auto-installed. Wire it up
once per machine after cloning:

    git config core.hooksPath hooks

Verify with `git config core.hooksPath` (should print `hooks`). The hook resolves
`ruff` from PATH, falling back to `python -m ruff` / `python3 -m ruff`.

## Quality gate: aislop

This project uses **aislop** as a deterministic quality gate for AI-written code
(narrative comments, swallowed exceptions, `as any`, dead stubs, oversized
functions, etc.) across TS/JS, Python, Go, Rust, Ruby, PHP, Java, and C#.

`aislop` is installed globally on this machine (pinned to the fork
`mtschoen/aislop`, which adds C#/roslynator support). Call the installed binary
directly - do NOT use `npx aislop`, which pulls upstream from npm with no C#
support:

- **Before declaring work complete**, run `aislop scan .` and address findings.
- **Before committing**, run `aislop scan --staged` (staged files only).
- `aislop fix` auto-clears mechanical issues (formatting, unused imports, dead
  code); `aislop fix --claude` hands the rest back with full context.
- `aislop ci .` is the gate - exits non-zero if the score drops below the
  threshold in `.aislop/config.yml`. Treat a failing gate like a failing test.
- **Restructure first; suppress only a proven false positive.** When a finding
  is real, fix the code. When the rule is provably wrong about this repo (read
  the rule in `node_modules/@schoen/aislop/dist/` before deciding), use the
  narrowest mechanism that exists: an inline
  `# aislop-ignore-next-line <rule> -- <one-line reason>` directive on the one
  offending line, never a repo-wide `rules:` entry and never a lowered
  `failBelow`. Every such site must state why. The tree currently has exactly
  two, both `hallucinated-import` on `import statusline` (see
  `.aislop/config.yml` and issue #23); `aislop scan .` prints the suppression
  count, so a third one showing up is visible.
- The pinned fork gate scores 96 against a floor of 90, with six
  `complexity/file-too-large` findings. Check `aislop ci .` before pushing
  anything that adds a file or grows one past 400 lines.

To refresh the pinned binary after new commits land on the fork branch:
`pnpm add -g --allow-build=aislop "github:mtschoen/aislop#schoen/main"`

**New npm advisories can fail the CI gate even when local aislop passes
(2026-08-03, PR #29):** the CI container's security engine reads `npm audit`
against the lockfile, while the local engine may not surface the same
findings. A fresh advisory on any transitive dep of `@schoen/aislop` then
drops the CI score below `failBelow` with no local warning (observed: 91 ->
70). Before pushing, run BOTH `aislop ci .` (the installed fork CLI used for
local checks; CI builds the commit in `.aislop/fork-commit`) and `npm audit`;
fix new findings via the `overrides` playbook in `package.json` (issue #23):
bump each entry to the lowest published version outside the advisory range,
then `npm install` to refresh the lockfile.

## kimi-code: the source checkout runs ahead of releases

Before declaring a kimi-code capability missing (from public docs, the
changelog, or strings in the installed binary), check the user's source
checkout at `~/kimi-code` — it routinely carries merged-but-unreleased
features. 2026-07-28: `status_line` support (commit 67dd03149, PR #2255)
was in the checkout but in no release <= 0.29.2, and the public changelog
showed nothing. The installed binary at `~/.kimi-code/bin/kimi.exe` trails
the checkout by one or more releases.

## Coverage gate: 100% on statusline_lib


The verify suite (`scripts/verify_*.py`) is held at **100% line coverage of
`statusline_lib/`** (reached 2026-06-10). CI runs the suite under coverage on
Linux AND Windows and fails below 100% - treat an uncovered line like a
failing test. Platform branches must be covered on BOTH OSes: patch `os.name`
in the test to force the foreign arm. Entry-point glue (statusline.py,
subagent_statusline.py, qwen_statusline.py, kimi_statusline.py, install.py,
wrap_nudge.py, statusline_client.py, statusline_client_support.py,
statusline_server.py) is outside the measured scope - keep logic in
`statusline_lib`, glue thin.

Measure locally (bash):

    python -m coverage erase
    for t in scripts/verify_*.py; do python -m coverage run -a "$t"; done
    python -m coverage report -m --include="statusline_lib/*" --fail-under=100

Current numbers: `TEST-REPORT.md`. No pragmas or exclusions: dead code gets
deleted, "unreachable" lines get restructured until the guard is live - the
same restructure-first policy as the aislop gate.

Tests must build their own fixtures (temp dirs, `os.utime`-pinned mtimes
against a synthetic window start) and never lean on live `~/.claude` data:
coverage that comes from the dev machine's real transcripts evaporates on a
clean CI runner (13 lines failed the gate's first run exactly this way).

## Render-budget invariant (no long sync calls in the render path)

Five production incidents share one disease: a mechanism meant to keep
renders fast instead let something run unbounded. 2026-07-02 (SMB per-file
stats plus walker timeout stalls, 20s renders), 2026-07-10 (psutil attribute
expansion, 11s renders), 2026-07-11 (beacons-history over an SMB root, 5s
timeout stalls), and 2026-07-16 (pace and spend transcript walks over an SMB
extra root, roughly 5.5s renders) were all a synchronous call blocking inside
a render; because Claude Code kills and replaces the render subprocess at its
refresh interval (roughly 3s), no render ever finished, the TTL cache could
never be rewritten, and the statusline froze at the session's first
pre-token render, `0 / 1.00M`. 2026-09-02 was a different disease from the
same family: not a slow render but unbounded process creation. Six sessions
plus CI drove renders to between 7 and 19 seconds against a 3 second refresh
interval; the harness killed only each overdue render's shell wrapper, so the
Python process survived as an orphan, and the mechanism built to keep renders
fast (a detached child process spawned per stale cache entry) multiplied
until the machine held roughly 1,000 Python processes and no idle CPU.

The render path is now a thin client, not a full interpreter doing the work.
`statusline_client.py` reads the harness payload from stdin, sends one UDP
datagram to the port named in `state_dir()/server.json`, waits at most 150ms
for one reply, and prints it verbatim. On a timeout, a dead port, a missing
server file, or a code-version mismatch it prints a fallback (the last
recorded render, or a payload-only line if none exists) and single-flight
spawns a replacement server without waiting on it. Holding the spawn lock is
not on its own a licence to spawn: the client re-reads `server.json` once it
holds the lock and abandons the spawn when the file has changed since the
snapshot it decided on and now names a server built from this checkout, which
is how a client delayed past another client's replacement starts nothing. All
computation lives in one resident server per `app_dir()`:
`statusline_lib/server.py` binds a
random localhost UDP port, serves each render inline on its receive loop
from in-memory per-session, per-cwd, and machine-wide state tables, and runs
everything that can block (git, psutil, HTTP, the walker) on a worker pool
bounded at four threads that the receive loop never waits on. Pool jobs carry
no deadline of their own; they are bounded by their own subprocess and HTTP
timeouts, still enforced by `scripts/verify_render_budget.py` across
`statusline_lib`, plus the four-thread ceiling.

`scripts/verify_render_budget.py` enforces the invariant mechanically: the
client files (`statusline_client.py` and the `statusline_client_support.py`
it reaches through a lazy import) import nothing outside the standard
library except one function-local `process_safe.spawn_detached` import
inside `_spawn`, reached only on the fallback path after a line has
already been printed; the client makes exactly one socket send and one
receive on its hot path; every subprocess call reachable from
`statusline_lib` still carries an explicit `timeout=` no greater than 2s,
with `Popen` and `time.sleep` banned there; and a live server round trip (a
real server on a random port, a real client subprocess, the median of nine
runs, the better of three attempts) must beat a 200ms budget, overridable via
`STATUSLINE_TEST_CLIENT_BUDGET_MS` and measured at roughly 40ms on the
development machine. `scripts/verify_server_concurrency.py` holds the
ceiling at one server and four workers under fifty concurrent renders. If a
new data source can't fit inside a worker-pool job's own timeouts, it
doesn't belong in the server either - cache it, delegate it to the walker, or
precompute it from a hook.

The stale-while-revalidate rule is unchanged in shape, only in mechanism: a
cache reader still serves whatever it has, stale included, and still hands
recomputation elsewhere rather than blocking on it. "Elsewhere" is now
`server_jobs.request_refresh` and the worker pool, not a detached child
process. Git ref (`statusline_lib/gitref.py`), the beacons-latest walker
lookup (`statusline_lib/beacon_cache.py`), the session-count psutil scan, the
pace hourly walk, the burn-rate spend rescan, and the calibrated-ETA
bias-factor lookup all serve their cache stale-or-absent and submit
recomputation as a pool job keyed by `(kind, str(argument))`, which keeps
refreshes scoped per cwd and per window rather than starving every working
directory but one. A new walk-priced or otherwise non-trivial data source
should go through the same job, not grow its own inline TTL cache or its own
thread.

Platform routing is pinned once, at spawn: a spawned server's own argv
carries no `--statusline-platform` flag, so `ensure_server` pins the render's
resolved platform into `STATUSLINE_PLATFORM` in the spawned server's
environment. That is what keeps a Kimi or Qwen client from starting a server
that writes its state where that harness never reads it, and one pin per
server covers every render and every refresh that server goes on to serve.

Performance tiers: a client render is a fixed cost, roughly 65ms of
interpreter and import plus at most 150ms of socket wait before it falls
back. A server render is single-digit milliseconds from payload to string,
since it pays no interpreter startup: the receive thread reads memory, warm
caches, small state files, and the session's own appended transcript bytes
(`server_state.read_appended`). Every other transcript read is a pool job
served stale-while-revalidate -- the beacon anchor scan, a teammate's cost
walk and a subagent row's walk all come out of
`statusline_lib/transcript_summaries.py`, which answers from a process-wide
table keyed by `(kind, path)` and validated against the file's
`(size, mtime_ns)`, and hands recomputation to the pool.
Spawn-per-render harnesses that still shell out to the client (Claude Code,
Qwen, Antigravity) pay the client's fixed cost on top of whatever their own
process launch costs; Kimi's 300ms kill window and Claude Code's roughly 3s
refresh interval both have comfortable headroom above it.

## Debugging the compact-mode width gate

`statusline_lib/compact.py` auto-sheds line-2 fields only when the rendered width
exceeds `$COLUMNS`. When auto-shrink looks broken, check the width source before
the logic:

- A `Bash`/shell subprocess does NOT inherit `COLUMNS`, so an `echo $COLUMNS`
  from a tool call reads empty - that is NOT the value the statusline sees.
- Claude Code (>= 2.1.153, confirmed on 2.1.160) still sets `COLUMNS` to the
  terminal width before invoking the client, but the client itself does not
  render: it forwards its own `COLUMNS` as a field on the request it sends
  the resident server, which applies it for that one render. So the process
  that reads `COLUMNS` is the client and the process that renders is the
  server. The raw stdin payload is logged per-render to
  `~/.claude/.statusline-input.log` (`server_render.write_input_log`), which
  is the record of what the client forwarded.
- So "shrinking never happens" usually just means the terminal is wider than
  line 2 (e.g. 316 cols) - drag the window narrow, or force it with
  `STATUSLINE_COMPACT=always`, to see fields drop in `DROP_ORDER`.

## Live prefs override env (debugging "my setting isn't taking effect")

Every `STATUSLINE_*` setting resolves as: `~/.claude/.statusline-prefs.json`
(written by `statusline_ctl.py`) > `settings.json` `env` block > built-in
default. The prefs file is read fresh on every render; the env block is only
inherited at Claude Code launch. Two consequences when debugging:

- An env edit in `settings.json` does nothing until restart AND can still be
  silently shadowed by a forgotten prefs override. Check
  `python statusline_ctl.py list` first - it shows every key's effective
  source.
- To change behavior live (no restart), go through `statusline_ctl.py set` /
  `reset`, not the env block.

## subagentStatusLine does not cover Agent Teams teammates

`subagentStatusLine` is documented for classic Task-tool subagents only
(`/en/sub-agents`). Agent Teams teammates (`run_in_background: true` + a
`name`, spawned under `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) are a separate
Claude Code feature with no equivalent per-row rendering hook - confirmed
empirically 2026-07-02 by dispatching a real named background agent and
diffing `.subagent-statusline-input.log`: the payload updated for a blocking
foreground subagent but never once for the teammate, even while its own
transcript JSONL kept growing. Don't spend time trying to make
`subagent_statusline.py` pick up teammate rows - Claude Code just doesn't
invoke it for them.

The workaround lives on the **main** statusline instead:
`statusline_lib/teams.py` polls `~/.claude/teams/<name>/config.json` (which
Claude Code writes live) plus each teammate's own transcript JSONL, and
`format_teammates()` renders a `teammates: ...` summary on line 3. Two
non-obvious bits if you touch that module:
- Teammate transcript filenames don't match the config's `agentId`
  (`watchme@session-xxx`) - they're `agent-a<name>-<hash>.jsonl` on disk, so
  lookup is a case-insensitive substring scan on the bare name, same as the
  fallback scan `subagent_statusline.py` already uses for task ids.
- There's no explicit running/idle field in `config.json`; "active" is
  inferred from the transcript's mtime against a 30s threshold, matching
  Claude Code's own idle-row-hide window per the agent-teams docs.
