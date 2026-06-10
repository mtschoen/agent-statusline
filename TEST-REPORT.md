# schoen-claude-status - Test Report

`2026-06-10`

| Field | Value |
|-------|-------|
| **Status** | PASS |
| **Mode** | maintain (lint); coverage informational |
| **Tests** | 23 `scripts/verify_*.py`, all passing |
| **Git** | `06e24ce` (main, working tree includes the nudge_install move) |

**Mode detail:** lint is *maintain* (held at the 0-findings / 100-score hard
bar; this run restored it after commit 8b0ad4a briefly dropped aislop to 78).
Line coverage remains *best-effort* / informational (baseline below is the
2026-06-02 one-shot measurement, not re-run).

**This run:** restored the gate by restructuring, not suppressing. The nudge
hook merge helpers (`_NUDGE_SENTINEL`, `_nudge_command`, `_nudge_markers`,
`_find_nudge_hooks`, `_nudge_hook_current`, `_merge_nudge_hook`) moved from
install.py into the new `statusline_lib/nudge_install.py`, so
`verify_install_nudge_merge.py` imports a recognized local package like every
sibling script - clearing the `ai-slop/hallucinated-import` false positive on
the repo-local `import install` (aislop resolves local package dirs with
`__init__.py` but not single-file modules). The chained `.get(..., {})`
lookup was split into explicit steps (`python-chained-dict-get`), and the
ruff format drift was reformatted away. `nudge_install.py` is fully exercised
by `verify_install_nudge_merge.py` but is not in the coverage table below
(predates it).

## Lint (hard gate)

| Tool | Result | Gate |
|------|--------|------|
| ruff | 0 findings | `ruff check .` + `ruff format --check .` |
| aislop | 100 / 100 | `aislop ci .` (failBelow 90) |
| pyright | non-blocking | CI runs with `\|\| true`; not run to clean |
| shellcheck | non-blocking | CI runs with `\|\| true`; not run to clean |

aislop reached 100 by **fixing, not suppressing**. This branch additionally cleared
pre-existing debt (Task 5.0):

- The two best-effort `except` blocks in `qwen_statusline.py` and the two
  report-and-abort `OSError` handlers in `install.py` now carry failure-mode
  comments (the documented way to clear `ai-slop/swallowed-exception`).
- `statusline.py`'s `_render_line2` took a `_Line2` `NamedTuple` options object to
  clear `complexity/too-many-params`.
- 0 ruff ignores beyond the one documented per-case `# noqa: RUF001` (the
  calibrated-ETA `x`). No aislop exclusions.

## Coverage (informational baseline, not a CI gate)

Newly instrumented for this report (one-shot measurement). Measured by running the
17 `verify_*.py` under coverage.py 7.13.5 and reporting `statusline_lib/` (the core
logic the suite exercises).

**Total: 797 / 1153 statements (69%)**

This feature's modules are well covered:

| Module | Coverage | Note |
|--------|----------|------|
| `compact.py` | 94% | new; misses 40-41 (invalid / `<=0` `$COLUMNS` branches) |
| `cost.py` | 94% | cache `$`-split + `format_ttl` + per-turn cost walk |
| `burnrate.py` | 80% | target arrow covered; misses are pre-existing needle paths |
| `pace.py` | 65% | `show_pace` covered; misses are pre-existing pace internals |
| `__init__.py` | 100% | |
| `base.py` | 88% | |
| `nudge.py` | 92% | |
| `project.py` | 92% | |
| `sessions.py` | 89% | |

Pre-existing low-coverage debt (not introduced by this feature; flagged as future
work, deliberately not closed under this feature branch per the owner's call):

| Module | Coverage | Note |
|--------|----------|------|
| `qwen.py` | 11% | Qwen formatters extracted in Phase 1; never had tests |
| `badge.py` | 25% | model-badge rendering |
| `walker.py` | 45% | |
| `beacon.py` | 48% | |

Not covered by the automated suite (entry-point glue; exercised only by the manual
statusline render / smoke test, consistent with the project's existing posture):
`statusline.py` (`main`, `_render_line2`), `qwen_statusline.py`, `install.py`,
`subagent_statusline.py`.

Known gap in code touched by this branch but left for later (owner's call):
`compact.py:40-41`, the `_columns()` invalid / `<=0` `$COLUMNS` fallback.

## Gates and commands

The bar: `ruff check .` -> 0, and `aislop ci .` -> >= 90 (currently 100). Line
coverage is **not** a gate (informational baseline only).

```bash
# First time:
npm ci --ignore-scripts

# Lint gates:
ruff check . && ruff format --check .
npm run lint:aislop          # aislop ci .

# Coverage (informational):
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t"; done
python -m coverage report -m --include="statusline_lib/*"
```

| | |
|---|---|
| **Config** | `pyproject.toml` (`[tool.ruff]`), `.aislop/config.yml` |
| **CI** | `.gitea/workflows/ci.yml` - ruff + aislop hard gates; pyright + shellcheck non-blocking |
| **Package** | `statusline_lib/` (base, sessions, walker, cost, beacon, pace, badge, compact, qwen, nudge, nudge_install, project, `__init__`) |
