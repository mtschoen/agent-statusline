# schoen-claude-status - Test Report

`2026-06-20`

| Field | Value |
|-------|-------|
| **Status** | PASS |
| **Mode** | maintain (lint AND coverage - both now hard CI gates) |
| **Tests** | 31 `scripts/verify_*.py`, all passing (Linux + Windows in CI) |
| **Git** | `47893ac` (main; working tree adds the pr-crew/coverage status post) |

**This run (Phase 1, pr-crew onboarding):** statusline_lib coverage is
unchanged at **100%** (1341/1341); no behavior or test logic changed. The
change is CI plumbing only - the repo measured 100% but never *posted* it, so
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
| aislop | 100 / 100, 0 findings | `npm run lint:aislop` (`aislop ci .`, failBelow 90) |
| pyright | non-blocking | CI runs with `\|\| true`; not run to clean |
| shellcheck | non-blocking | CI runs with `\|\| true`; not run to clean |

0 per-case suppressions beyond the one documented `# noqa: RUF001`
(the calibrated-ETA multiplication-sign glyph). No aislop exclusions or rule
overrides.

## Coverage (hard gate, 100%)

Measured by running all 31 `verify_*.py` under coverage.py and reporting
`statusline_lib/` - the package that holds all logic. CI fails below 100%.

**Total: 1341 / 1341 statements (100%)** - every module:
`__init__` 14, `badge` 67, `base` 44, `beacon` 206, `burnrate` 145,
`compact` 37, `cost` 105, `costfmt` 68, `diffstat` 7, `nudge` 52,
`nudge_install` 35, `pace` 240, `prefs` 29, `project` 61, `qwen` 53,
`sessions` 114, `walker` 64.

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
| **Package** | `statusline_lib/` (base, sessions, walker, cost, costfmt, diffstat, beacon, pace, badge, compact, qwen, nudge, nudge_install, prefs, project, burnrate, `__init__`) |
