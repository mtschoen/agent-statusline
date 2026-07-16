# schoen-claude-status - Test Report

`2026-07-16`

| Field | Value |
|-------|-------|
| **Status** | PASS |
| **Mode** | maintain (lint AND coverage - both hard CI gates) |
| **Tests** | 58 `scripts/verify_*.py`, all passing locally |
| **Git** | `e4320e5` (`main`, plus this change at time of writing) |
| **Coverage** | 2089/2089 statements (100%), 0 exclusion annotations |
| **Lint** | ruff format 0 / ruff check 0; aislop 91/100 Healthy (0 errors, 6 pre-existing file-size warnings, gate failBelow 90); 0 suppressions |

**This run (stale-while-revalidate transcript caches - frozen-statusline
fix):** the pace hourly walk and the burn-rate spend rescan no longer run
inline in a render. With `/mnt/chonkers/.claude/projects` (6.6K JSONLs over
CIFS) in the walker roots, a TTL-miss render cost ~5.5s; Claude Code
replaces the render subprocess at its ~3s refresh interval, so no render
finished, the cache could never be rewritten, and the live statusline froze
at the session's first pre-token render (`0 / 1.00M`). New
`statusline_lib/refresh.py` (57 statements, 100%) spawns a detached
recompute child via the vendored `process_safe.spawn_detached`, debounced
by an inflight marker (claims pruned after 120s, released on spawn
failure); `pace._pace_hourly_cached` and `burnrate._window_spend_cached`
serve stale entries while it runs and degrade honestly on a true miss ([] /
0.0 / the nearest trailing-window grid cell within 60s). Both cache files
moved to multi-entry v2 formats (v1 abandoned in place); readers drop
non-dict entries, so torn or mixed-format files read as absent instead of
crashing (`verify_hide_cost` caught exactly that against a live
mid-deploy cache). Three new verify scripts (pace_refresh, spend_refresh,
refresh_spawner - the last runs the real child snippet in a real
interpreter against a fixture corpus); the superseded inline-contract
checks were removed from verify_pace_walk and verify_burn_rate. Live smoke
on llamabox: cold render 5.9s -> 0.18s, warm 0.09s, detached children
wrote both caches within ~10s and the burn-rate/pace fields returned.
`statusline_lib` remains at **100%** (2089/2089 statements).

**This run (render-timer port + perf-ratchet steps 1+2, two reviewed
branches merged):** ported the Pi footer's render-timing instrumentation
(`ui <dur> peak <peak>`, commit `0323dbc`) to the spawn-per-render Python
harnesses, and landed the PLAN.md render-perf ratchet's first two steps —
TTL disk caches (2.5s) for `_git_ref` and the beacons-latest walker call
(new `statusline_lib/beacon_cache.py`, 31 statements), dropping the enforced
warm-core budget in `scripts/verify_render_budget.py` from 350ms to 100ms
(measured fixture median: 48-51ms before, 2-3ms after). New
`statusline_lib/rendertimer.py` (52 statements, 100% coverage) mirrors Pi's PREVIOUS-render semantics via a small
per-session state file under `~/.claude/state`: `format_render_suffix` reads
the prior render's duration + session peak (appended to the last output
line), `record_render` persists the just-finished render's elapsed time +
updated peak at process exit. Both `statusline.py` and `qwen_statusline.py`
reuse their existing `time.monotonic()` measurement (no second clock); Qwen's
payload carries no session id, so it collapses onto a shared state key. Peak
tracking falls out of per-session file keying -- a new session id has no
prior file, so no explicit reset step was needed. `STATUSLINE_RENDER_TIMING=0`
disables it, same env var and default-on semantics as Pi. New
`scripts/verify_render_timer.py` covers the env gate, read/record round-trip,
peak tracking, session isolation, the no-session-id fallback, corrupt/absent
state, OSError-swallowing on write, and end-to-end subprocess renders of both
`statusline.py` and `qwen_statusline.py` (first render shows no suffix,
second shows the first's timing; the disabled-gate path shows no suffix and
writes no state at all). `scripts/verify_render_budget.py` is green at the
new 100ms budget on the merged tree (no subprocess/sleep in the render path).
Ruff and aislop both clean; `statusline_lib` remains at **100%**
(1871/1871 statements).

**This run (richer Codex preset + shared cache formatting):** the native Codex
preset now trades the verbose thread UUID for PR number, input/output token
totals, permissions, approval mode, and fast-mode state. Upstream Codex 0.144.0
still exposes neither cached-token telemetry nor caller-defined labels/colors,
so cache hit/miss cannot yet render in its native footer. The reusable part is
now centralized in `statusline_lib/cachefmt.py`: Claude and Qwen share cache
count coloring, hit-rate math, and the high-is-good threshold ramp rather than
maintaining harness-specific copies. The isolated Codex install smoke wrote the
richer preset, a second install was idempotent, and the shared Claude/Qwen cache
render smoke preserved their existing colored output. Ruff is clean; aislop is
Healthy (94/100) with the same four pre-existing file-size warnings and no
errors. All 34 verify scripts pass and `statusline_lib` remains at **100%**
(1611/1611 statements), including all 11 statements in the new shared module.

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

Measured by running all 49 `verify_*.py` under coverage.py and reporting
`statusline_lib/` - the package that holds all logic. CI fails below 100%,
independently on each OS job (Linux and Windows each run the gate on their own
run, not combined - a branch only covered on one leg fails the other).

**Total: 1871 / 1871 statements (100%, verified this run on Windows)** -
every module: `__init__` 17, `agy` 60, `badge` 110, `base` 66, `beacon` 223,
`beacon_cache` 31, `burnrate` 146, `cachefmt` 9, `codex_install` 100,
`compact` 41, `cost` 114, `costfmt` 68, `diffstat` 7, `nudge` 53,
`nudge_install` 37, `pace` 278, `prefs` 31, `project` 61, `qwen` 52,
`rendertimer` 52, `sessions` 183, `teams` 67, `walker` 65.

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
