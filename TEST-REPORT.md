# agent-statusline - Test Report

`2026-08-06`

| Field | Value |
|-------|-------|
| **Status** | PASS (Windows and Linux; the Linux job was red before this run) |
| **Mode** | maintain (lint AND coverage - both hard CI gates) |
| **Tests** | 67 `scripts/verify_*.py`, all passing on Windows and on WSL Ubuntu-24.04 |
| **Git** | `f483c74` (`main`) plus the process_safe nt-arm fix below |
| **Coverage** | 2695/2695 statements (100%), 0 exclusion annotations |
| **Lint** | ruff format 0 / ruff check 0; aislop ci exit 0 (6 pre-existing file-size warnings in untracked WIP files, gate failBelow 90); aislop scan --staged 100/100 Healthy (0 issues) |

**This run (fix the red Linux job in `process_safe`'s nt arm):** the
`Unit tests (Linux)` job had been failing with
`AttributeError: module 'subprocess' has no attribute 'CREATE_NEW_PROCESS_GROUP'`.
`spawn_detached` read that Windows-only constant bare, while everything
around it in `_windows_hidden_kwargs` is `getattr`-guarded precisely so the
nt arm stays runnable on any host; `scripts/verify_process_safe.py` forces
`os.name = "nt"` to exercise that arm, and off Windows the bare read blew
up. Guarded it to match its neighbours.

That alone turns the job green but leaves a worse problem: off Windows
*every* nt assertion was vacuous, because each one reads its constant
through `getattr(..., 0)` and short-circuits on the 0. The Linux run was
executing the branch and checking nothing. `_windows_constants()` now
installs real-valued fakes for the missing names around the two nt checks,
so Linux asserts what Windows asserts. Verified by mutation: deleting the
`CREATE_NEW_PROCESS_GROUP` flag is now caught on Linux
(`FAIL: spawn_detached[nt] creationflags missing CREATE_NEW_PROCESS_GROUP`)
and passed silently before.

Note for future platform branches: do not reach for
`monkeypatch.setattr(os, "name", ...)` as a general tool. `pathlib` reads
`os.name` to choose `Path`'s flavour, so forcing `"posix"` on Windows makes
the next `resolve()` raise `UnsupportedOperation`. It is survivable here
only because this script forces `"nt"`, and only on Linux.

**Previous run (kimi working-tree git badge + refresh-child platform pin):**
the kimi line gains kimi-code's built-in footer's `+A -B ↑x ↓y` badge
inside the branch parens (`(main +2 -2 ↑58)`). The status_line payload
never carries these counters (confirmed against the released 0.29.2 binary
AND the source checkout's `StatusLinePayload`), so
`statusline_lib/gitref.py`'s SWR cache now persists `added`/`deleted`
(`git diff --numstat HEAD`, binary `-` as 0) and `ahead`/`behind`
(`git rev-list --left-right --count @{upstream}...HEAD`, empty output on
no-upstream/unborn-HEAD degrading to zeros) alongside branch/short_hash;
`git_working_tree_cached` serves them stale-or-zero (pre-badge cache
entries spawn a backfill; wrong-typed counters degrade to 0) and
`statusline_lib/kimi.py::_working_tree_badge` renders them (green/red diff,
yellow sync) with zero inline git on the render path. Fixed a real bug the
smoke test exposed: the detached refresh child resolved app_dir() to
~/.claude (kimi's platform lives in argv, not env), so refreshes spawned
by kimi renders wrote caches their render never read --
`refresh._child_snippet` now pins STATUSLINE_PLATFORM into the child's env
(fixes the session-count badge on kimi/qwen too, same latent gap). New
`scripts/verify_git_working_tree_cache.py` (split out when
verify_git_ref_cache.py outgrew aislop's 400-line gate) plus new cases in
verify_git_ref_cache.py / verify_kimi_adapter.py /
verify_refresh_spawner.py. Smoke-tested end to end: cold render spawns the
refresh, warm render shows `(main +2 -2 ↑58)` against the live schoen-lab
checkout -- byte-identical numbers to kimi's built-in footer.
`statusline_lib` remains at **100%** (2492/2492 statements).

**Previous run (kimi CLI-version badge + cold-start TID251 ignore):** the kimi
adapter's single line gains a dim `vX.Y.Z` badge from the payload's
`version` field (`statusline_lib/kimi.py::_version_badge`, appended after
the PLAN badge). Scalars only: a wrong-typed container (list/dict) drops
rather than stringify into the badge, a payload-carried leading `v` is
stripped so we never render `vv...`, and `bool` is excluded despite being
an `int` subclass. Also fixed doc drift in the same file and in README:
`contextUsage` is a float fraction (0.047 == 4.7%), not an integer percent
(confirmed against a live `~/.kimi-code/.statusline-input.log` payload).
`verify_kimi_adapter.py` gained `_check_version_badge` (absent / blank /
container / leading-v cases) plus a full-payload assertion. The one new
ruff suppression: `scripts/verify_cold_start.py` (untracked WIP file)
joined `pyproject.toml`'s documented TID251 per-file-ignores list -- it is
the same outer-harness shape as its 7 listed siblings (spawns a real
statusline.py subprocess with `input=`/`env=`, a surface
`process_safe.run_captured` deliberately doesn't expose); without it
`ruff check .` reported 1 error against an otherwise-clean gate. The 6
aislop file-size warnings are all pre-existing WIP files (this change adds
none: kimi.py is 158 lines). Smoke-tested against the live captured kimi
payload: single line, exit 0, `v0.29.2` rendered in 256-color 245.
`statusline_lib` remains at **100%** (2442/2442 statements).

**Previous run (bias-factor async-refresher migration + slow-render phase
breakdown):** root-caused from a real production incident -- a 5.8s
slow-render log entry (`~/.claude/.statusline-error.log`) with no per-phase
evidence, requiring a multi-file forensic reconstruction across cache/state
timestamps to diagnose. Root cause: `beacon.py`'s `_bias_factor_cached` (the
calibrated-ETA lookup) was the last inline `_walker_subcommand` call left on
the render path -- every sibling lookup (git ref, beacons-latest,
session-count, pace/spend) had already migrated to the stale-while-revalidate
+ detached-refresher pattern, but this one still paid a real
`beacons-history` subprocess call (2s cap) inline on a 60s TTL miss. Migrated
onto the same pattern: `_bias_factor_cached` now only reads the cache
(serving stale or a neutral `(0, None)` on a true miss) and hands
recomputation to `refresh_bias_factor_cache` via `maybe_spawn_refresh`;
confirmed by sweeping every `run_captured`/`_walker_subcommand` call site in
`statusline_lib` -- all now live exclusively inside `refresh_*` functions
reachable only from the detached child, never the render itself. Also added
`statusline_lib.rendertimer.PhaseTimer` (a per-render checkpoint accumulator,
near-zero cost until a slow render actually reads it) and
`refresh.spawn_timings()` (every `maybe_spawn_refresh` call's kind + elapsed,
reset per render): a render crossing `_SLOW_RENDER_SECONDS` now gets a
breakdown appended to the log line, e.g. `slow render: 5.8s (threshold 5s)
[walk=0.10s, gitref=0.04s, beacon=0.05s[bias-refresh-spawned],
spawns=0.06s[4x]]`, instead of a bare total -- the next spike is diagnosable
from the log alone. Test files rewritten/extended to match:
`verify_beacon_walker.py`'s bias-cache suite (fresh-hit/stale-serve/miss/
alternating-periods, all re-shaped around spawn semantics instead of inline
recompute), `verify_refresh_spawner.py` (new `bias-factor` dispatch entry +
`spawn_timings`/`reset_spawn_timings` instrumentation), and a new
`verify_phase_timer.py` (`PhaseTimer.mark`/`.record`/`.breakdown`,
`start_phase_timer`/`current_phase_timer` -- split out of
`verify_render_timer.py` once this suite's own growth crossed the file-size
gate; that file keeps its separate previous-render/peak-tracking tests).
Also caught and fixed two aislop regressions the change itself introduced
before this run's clean 93/100 (matching the pre-change baseline exactly):
`main()` growing past the function-length gate (fixed by extracting the
spawn-summary glue into `rendertimer.summarize_spawns`/`start_phase_timer`)
and two chained-`.get(..., {})` findings in a new test (split into explicit
steps). Smoke-tested against a real captured production payload
(`~/.claude/.statusline-input.log`): sane 3-line render, exit 0, error log
unchanged.

**Previous run (render-perf ratchet step 3, remainder - warm-core budget
100ms -> 10ms):** the residual per-render work the 2026-07-16 async-refresher
split left inline -- git ref, the beacons-latest walker lookup, and the
session-count psutil scan -- moved onto the same stale-while-revalidate +
detached-refresher pattern (`statusline_lib/refresh.py`) as the pace/spend
walks: a render always serves whatever the cache holds, and a stale/missing
entry spawns a detached child rather than blocking. Motivating measurement:
an uncached session-count scan cost ~120ms on this machine (~600 processes),
git ref ~9ms, beacons-latest ~15ms -- all past the target, uncached. New
`statusline_lib/gitref.py` (41 statements, 100%) replaces statusline.py's
`_git_command`/`_git_ref_raw_cached`/`refresh_git_ref_cache` (statusline.py
is coverage-exempt entry glue, so this move puts real coverage teeth on
those functions for the first time); `beacon_cache.py` and `sessions.py`
gained `refresh_beacon_latest_cache`/`refresh_session_count_cache` alongside
their now-stale-serving cached readers. `refresh.py` generalized its
inflight-key/child-snippet argument handling to accept string cache keys
(cwd, session id) alongside the existing numeric window timestamps, and its
kind-dispatch became a table (`_REFRESHER_MODULES`) rather than a growing
if/elif chain (aislop's repetitive-dispatch finding). `ttlcache.py` gained
`read_raw_cache` (serves a cache entry regardless of TTL age) alongside
`read_ttl_cache` (fresh-or-None) for the single-value SWR callers.
`_CORE_BUDGET_MS` ratcheted 100 -> 10 (the Pi bridge's per-keypress budget):
measured median across 30 repeated runs of the real conformance check is
~1-6ms (25-sample batch: min 1.09ms, median 2.02ms, p90 4.60ms, max 5.95ms),
30/30 runs passing at 10ms with 2-5x margin. Test files rewritten to match
the new stale-serve/spawn contract (`verify_git_ref_cache.py`,
`verify_active_session_count.py`, `verify_beacon_walker.py`'s beacons-latest
section), mirroring `verify_pace_refresh.py`'s existing pattern.
`statusline_lib` remains at **100%** (2178/2178 statements).

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
