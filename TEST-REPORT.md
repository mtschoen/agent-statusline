# schoen-claude-status - Test Report

`2026-07-02`

| Field | Value |
|-------|-------|
| **Status** | PASS |
| **Mode** | maintain (lint AND coverage - both now hard CI gates) |
| **Tests** | 33 `scripts/verify_*.py`, all passing locally |
| **Git** | `9f93eda` (agent-teams summary line + refreshInterval) |

**This run (agent-teams summary line):** `subagentStatusLine` never fires for
Agent Teams teammates (no per-row hook exists for them - confirmed empirically
by dispatching a live named background agent and diffing
`.subagent-statusline-input.log`). New `statusline_lib/teams.py` (67
statements, 100% coverage) works around this on the main statusline instead,
reading `~/.claude/teams/<name>/config.json` plus each teammate's own
transcript directly. `statusline_lib` coverage is unchanged at **100%**
(1476/1476 on this OS run - two pre-existing OS-specific branches in
`base.py`/`walker.py` are covered on the other CI leg, same as before this
change). `install.py` also now writes `refreshInterval: 3` on the `statusLine`
block so the footer keeps repainting while the lead is idle waiting on a
background teammate. Ruff and aislop both clean (aislop 94/100, same
pre-existing file-too-large warnings as before, none newly introduced).

**Prior run (Pi extension port):** statusline_lib coverage is unchanged at
**100%** (1379/1379); the new Pi port lives in `pi-extension/` and is verified
with a Node/Jiti render smoke against the real global loader at
`~/.pi/agent/extensions/agent-statusline/index.ts`. The Pi extension reuses Pi's
native session usage totals for context, cache, costs, burn rate, diffstat, and
session/turn timing rather than translating through Claude Code's stdin payload.
Ruff is clean. Aislop is **Healthy** (99) with one pre-existing style warning:
`statusline_lib/pace.py` exceeds the 400-line reviewability threshold; the new
Pi files are below the threshold and add 0 findings.

**Prior run (Phase 1, pr-crew onboarding):** statusline_lib coverage was
unchanged at **100%** (1341/1341); no behavior or test logic changed. The
change was CI plumbing only - the repo measured 100% but never *posted* it, so
pr-crew's coverage gate read a missing `pr-crew/coverage` status as 0.00% and
filed issue #12. CI now vendors the stdlib-only `ci/post-coverage-status.py`
helper, emits a statusline_lib-scoped `coverage.json` (same scope as the 100%
gate), and POSTs a `pr-crew/coverage` success status on the Linux test job
(`if: always()`, single poster to avoid double-posting the SHA). This resolves
issue #12 and onboards the repo to pr-crew. Entry-point glue (statusline.py et
al.) remains out of the measured scope by design (Phase 2 will revisit that).

**This run (close-the-gap, completed):** statusline_lib line coverage went
76% -> **100%** (1341/1341 statements, all 17 modules) in one parallel
test-writing pass: 8 new verify scripts (badge, beacon render/walker, pace
render/walk, qwen render, walker + walker binary) plus extensions to 11
existing ones. Zero pragmas/exclusions. Two genuinely-dead defensive branches
found by the push were deleted (project.py `denom == 0`, unreachable for
count >= 2 over integer xs) or restructured into live guards (pace.py
weekly_sustainable_rate: the redundant `util >= 100` entry clause removed so
the `remaining_dollars <= 0` spent-quota guard is the real, tested check).
Coverage is now a **CI gate at 100%** on both OS jobs; platform branches are
covered on both OSes by patching `os.name` (the suite's one platform branch,
nudge_install._nudge_command, tests both arms explicitly). Seven suspected
bugs surfaced during the push are queued in PLAN.md Inbox for triage -
reported, deliberately not fixed mid-push.

## Lint (hard gate)

| Tool | Result | Gate |
|------|--------|------|
| ruff | 0 findings | `ruff check .` + `ruff format --check .` |
| aislop | Healthy (99), 0 errors; 1 pre-existing style warning | `npm run lint:aislop` (`aislop ci .`, failBelow 90) |
| pyright | non-blocking | CI runs with `\|\| true`; not run to clean |
| shellcheck | non-blocking | CI runs with `\|\| true`; not run to clean |

0 per-case suppressions beyond the one documented `# noqa: RUF001`
(the calibrated-ETA multiplication-sign glyph). No aislop exclusions or rule
overrides.

## Coverage (hard gate, 100%)

Measured by running all 32 `verify_*.py` under coverage.py and reporting
`statusline_lib/` - the package that holds all logic. CI fails below 100%.

**Total: 1476 / 1476 statements (100%, combined across both CI OS legs)** -
every module: `__init__` 15, `badge` 67, `base` 56, `beacon` 209,
`burnrate` 145, `compact` 37, `cost` 114, `costfmt` 68, `diffstat` 7,
`nudge` 53, `nudge_install` 37, `pace` 269, `prefs` 31, `project` 61,
`qwen` 53, `sessions` 115, `teams` 67, `walker` 72.

**Scope:** entry-point glue is outside the measured set, by design -
`statusline.py`, `subagent_statusline.py`, `qwen_statusline.py`,
`install.py`, `wrap_nudge.py` are thin shims exercised by the manual render
smoke test. Logic belongs in `statusline_lib`, where the gate sees it.

## Gates and commands

The bar: `ruff check .` + `ruff format --check .` -> 0, `aislop ci .` -> >= 90
(currently 100), and statusline_lib coverage -> 100%.

```bash
# First time:
npm ci --ignore-scripts
pip install coverage psutil

# Lint gates:
ruff check . && ruff format --check .
npm run lint:aislop          # aislop ci .

# Coverage gate:
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
```

| | |
|---|---|
| **Config** | `pyproject.toml` (`[tool.ruff]`), `.aislop/config.yml` |
| **CI** | `.gitea/workflows/ci.yml` - ruff + aislop + 100% coverage hard gates; pyright + shellcheck non-blocking |
| **Package** | `statusline_lib/` (base, sessions, walker, cost, costfmt, diffstat, beacon, pace, badge, compact, qwen, nudge, nudge_install, prefs, project, burnrate, teams, `__init__`) |
