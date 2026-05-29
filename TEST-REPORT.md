schoen-claude-status test report — 2026-05-29T22:15:01Z
═══════════════════════════════════════════════════════

Status:   PASS
Mode:     close-the-gap (lint gate stand-up)
Tests:    6 verify_*.py scripts — all passing
Git:      64a3291 (SIM103 fix — gate reached 0 findings)

Lint:     ruff 0 findings  ✓  (hard gate — `ruff check .` + `ruff format --check .`)
          pyright: not yet run to clean — CI step is non-blocking (`|| true`)
          shellcheck: not yet run to clean — CI step is non-blocking (`|| true`)
          0 per-case suppressions (`# noqa`)
          5 rule-level ignores, all documented in pyproject.toml:
            SIM105, UP042  — stylistic judgment calls (mirrors fleet gate)
            RUF001/002/003 — ambiguous '×' is a deliberate convention that
                             renders in user-facing status-line output
          1 per-file-ignore group (scripts/**/*.py = test-idiom allowances)

Coverage: NOT INSTRUMENTED — no coverage.py / line-coverage tooling is
          configured in this repo. The 6 scripts/verify_*.py are behavioral
          verification scripts (exercised in CI), not a coverage-measured
          suite. Standing up line coverage is out of scope for the lint
          rollout and is flagged as candidate future work.

───────────────────────────────────────────────────────
Validation gate (the bar):  ruff check .  →  0 findings.
Run locally:  ruff check . && ruff format --check .
Config:       pyproject.toml  ([tool.ruff])
Rollout doc:  LINTER-SETUP.md
On-save:      .claude/settings.json PostToolUse hook (ruff -q per edited .py)
CI:           .gitea/workflows/ci.yml  (lint job — ruff hard, pyright/shell soft)
