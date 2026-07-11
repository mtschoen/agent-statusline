# schoen-claude-status

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

To refresh the pinned binary after new commits land on the fork branch:
`pnpm add -g --allow-build=aislop "github:mtschoen/aislop#feat/csharp-support"`

## Coverage gate: 100% on statusline_lib

The verify suite (`scripts/verify_*.py`) is held at **100% line coverage of
`statusline_lib/`** (reached 2026-06-10). CI runs the suite under coverage on
Linux AND Windows and fails below 100% - treat an uncovered line like a
failing test. Platform branches must be covered on BOTH OSes: patch `os.name`
in the test to force the foreign arm. Entry-point glue (statusline.py,
subagent_statusline.py, qwen_statusline.py, install.py, wrap_nudge.py) is
outside the measured scope - keep logic in `statusline_lib`, glue thin.

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

Three production incidents shared one disease — a synchronous call inside a
render that can block for seconds (2026-07-02 SMB stats + walker stalls, 20s;
2026-07-10 psutil attr expansion, 11s; 2026-07-11 beacons-history over SMB,
5s timeout stalls). `scripts/verify_render_budget.py` enforces the invariant
mechanically: every subprocess call reachable from a render carries an
explicit `timeout=` <= 2s (wrapper defaults included), `Popen`/`time.sleep`
are banned there, and a cold-cache end-to-end render against a synthetic
corpus must beat an 8s wall-clock budget. If a new data source can't fit the
cap, it doesn't belong in the render path — cache it, delegate it to the
walker, or precompute it from a hook.

Performance-conformance tiers (same script enforces the first two; budgets
ratchet down per the PLAN.md render-perf item):
- warm core (payload -> string, in-process, caches warm): <= 350ms today,
  targeting <50ms after git/beacon caching, <10ms after the async-refresher
  split. The <10ms cached path is also the Pi bridge's per-keypress budget.
- cold end-to-end (fixture corpus, empty caches): <= 8s (realistically ~1s;
  first/second render may block on basics like cwd/git).
- spawn-per-render harnesses (Claude/Qwen/agy) additionally pay ~100ms of
  interpreter+import outside our control — wall-clock budgets live on top of
  that floor, and Claude Code refreshes at most every ~300ms anyway.

## Debugging the compact-mode width gate

`statusline_lib/compact.py` auto-sheds line-2 fields only when the rendered width
exceeds `$COLUMNS`. When auto-shrink looks broken, check the width source before
the logic:

- A `Bash`/shell subprocess does NOT inherit `COLUMNS`, so an `echo $COLUMNS`
  from a tool call reads empty - that is NOT the value the statusline sees.
- The live statusline subprocess DOES get it: Claude Code (>= 2.1.153, confirmed
  on 2.1.160) sets `COLUMNS` to the terminal width before invoking the command.
  Ground truth is logged per-render to `~/.claude/.statusline-cols-debug.log`
  (and the raw stdin payload to `~/.claude/.statusline-input.log`).
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
