# schoen-claude-status - Test Report

`2026-07-10`

| Field | Value |
|-------|-------|
| **Status** | PASS |
| **Mode** | maintain (lint AND coverage - both now hard CI gates) |
| **Tests** | 34 `scripts/verify_*.py`, all passing locally |
| **Git** | `947c78d` (`main`) |

**This run (Codex CLI native statusline preset):** added a safe, idempotent
`~/.codex/config.toml` merge and `install.py --platform codex`. Codex owns its
TUI footer and does not expose Claude Code's command-backed JSON/stdin hook, so
the installer selects the closest built-in fields without pretending the
Claude-only cache/cost/beacon rows can render. The new
`statusline_lib/codex_install.py` has **100% coverage** across section, dotted,
nested-child-table, CRLF, invalid-TOML, inline-table, scanner-guard, and
integrity-guard cases. The nested-child regression matches a live Codex config
that defines `[tui.model_availability_nux]` before the installer adds `[tui]`.
Runtime smoke: installed into an isolated temporary Codex home, verified the
generated native preset, and confirmed a second install reports `already
current`. A live install against an existing nested-child-table Codex config
then succeeded with the same idempotence check. Ruff is clean; aislop is
Healthy (94/100) with the same four pre-existing file-size warnings and no
errors.

**This run (CI-green restore):** main had been RED on all three CI checks
since `b62b612` (2026-06-27), ~10 days before being noticed - unrelated to the
render-perf work that happened to land the same night. Two independent,
pre-existing regressions:

1. **Lint (aislop):** `b62b612` repointed the `@schoen/aislop` devDependency
   from the Gitea-registry-pinned version to a local relative path
   (`file:../../../aislop`) that only resolves on a dev machine with a sibling
   `aislop` checkout at that exact depth. `npm ci --ignore-scripts` silently
   installed a broken/empty package in CI (no sibling dir there), so
   `node_modules/.bin/aislop` never materialized and `npm run lint:aislop`
   failed with `aislop: not found`. Fix: revert `package.json`,
   `package-lock.json`, and `.npmrc` to the pre-`b62b612` registry-pinned
   `@schoen/aislop@0.12.3`.
2. **Coverage (both OS legs):** the `ANTIGRAVITY_AGENT`/
   `ANTIGRAVITY_CONVERSATION_ID` auto-detect fallback branches added to
   `app_dir()` (`base.py`) and `_walker_root_list()` (`walker.py`) back in
   `dccc87e` were never exercised by any verify script - `statusline_lib`
   measured 99% (1492/1499) on *both* Linux and Windows, not the previously
   assumed "100% combined across both CI OS legs" (that framing was already
   stale; these lines were platform-agnostic env-var branches, not an
   OS-specific split). Fix: `scripts/verify_prefs.py` and
   `scripts/verify_walker.py` each gained a fallback-branch test exercising
   both the "only `.claude` exists" and "`.gemini/antigravity-cli` exists"
   arms. `statusline_lib` is back to **100%** (1499/1499) on both legs.

Ruff, aislop (Healthy, 94/100 via the now-working registry-pinned binary,
score unchanged), and coverage are all independently verified green locally
(Windows) prior to this fix's PR.

**Prior run (agent-teams summary line):** `subagentStatusLine` never fires for
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
| aislop | Healthy (94), 0 errors | `npm run lint:aislop` (`aislop ci .`, failBelow 90) |
| pyright | non-blocking | CI runs with `\|\| true`; not run to clean |
| shellcheck | non-blocking | CI runs with `\|\| true`; not run to clean |

0 per-case suppressions beyond the one documented `# noqa: RUF001`
(the calibrated-ETA multiplication-sign glyph). No aislop exclusions or rule
overrides.

## Coverage (hard gate, 100%)

Measured by running all 34 `verify_*.py` under coverage.py and reporting
`statusline_lib/` - the package that holds all logic. CI fails below 100%,
independently on each OS job (Linux and Windows each run the gate on their own
run, not combined - a branch only covered on one leg fails the other).

**Total: 1599 / 1599 statements (100%, independently on both CI OS legs)** -
every module: `__init__` 15, `badge` 82, `base` 56, `beacon` 210,
`burnrate` 146, `codex_install` 100, `compact` 37, `cost` 114, `costfmt` 68,
`diffstat` 7, `nudge` 53, `nudge_install` 37, `pace` 275, `prefs` 31,
`project` 61, `qwen` 53, `sessions` 115, `teams` 67, `walker` 72.

**Scope:** entry-point glue is outside the measured set, by design -
`statusline.py`, `subagent_statusline.py`, `qwen_statusline.py`,
`install.py`, `wrap_nudge.py` are thin shims exercised by the manual render
smoke test. Logic belongs in `statusline_lib`, where the gate sees it.

## Gates and commands

The bar: `ruff check .` + `ruff format --check .` -> 0, `aislop ci .` -> >= 90
(currently 94), and statusline_lib coverage -> 100%.

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
