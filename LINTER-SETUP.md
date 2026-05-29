# Recommended linting setup for schoen-claude-status — fleet survey 2026-05-29

One-line: stand up a three-tier "lint as you write → validate → CI" setup for
this repo's Python (+ shell) so style/bugs are caught at the keystroke, gated
before "done", and enforced at merge.

## Current state
- **Languages:** Python (the statusline/cost library + tests) and a few shell
  scripts (`*.sh`).
- **Existing linter/formatter config:** none — no `[tool.ruff]`/`[tool.mypy]`/
  `[tool.pyright]` in `pyproject.toml`, no `.editorconfig`, no
  `.pre-commit-config.yaml`.
- **CI:** present (`.gitea/workflows/`) but **no lint step** today.
- **Claude Code on-save hook:** none.
- **Baseline:** `ruff check .` (default `E,F` rules) reports **2 findings**. A
  curated broad `select` (below) will surface a few more (mechanical). `shellcheck`
  / `shfmt` are not installed here — install to lint the `.sh` scripts.

## The three tiers
1. **On-save** (fast, per-file, Claude Code `PostToolUse` hook) — instant feedback as code is written.
2. **Validate** (the go-to linter; what you run on demand and what `/maintaining-full-coverage` gates on) — full-repo, all rules, "0 findings is the bar."
3. **CI** — automates tier ② (+ coverage) so regressions block at merge.

| Tier | Python | Shell |
|---|---|---|
| ① On-save | `ruff format` + `ruff check --fix` (same tool as ②) | `shfmt -w <file>` |
| ② Validate | `ruff check` (broad `select`); types: `pyright` | `shellcheck <file>` |
| ③ CI | `ruff check .` + `ruff format --check` + `pyright` | `shellcheck **/*.sh` |

**Why:** ruff is 10–100× faster than flake8/pylint and replaces
flake8+black+isort+pyupgrade+pydocstyle in one binary — so ① and ② are the *same
tool* in two modes. `pyright` is the 2026 default type-checker (2–5× faster than
mypy, ~98% spec conformance). `shellcheck` is the universal shell linter;
`shfmt` the formatter.

## Suggested `pyproject.toml` (mirrors projdash's gate)
```toml
[project.optional-dependencies]
dev = [ "ruff>=0.6", "pyright" ]   # add

[tool.ruff]
target-version = "py312"   # match your floor

[tool.ruff.lint]
select = ["F","I","B","UP","SIM","RET","PIE","C4","W","RUF"]
# E (E402/E501) and ARG left unselected; SIM105/UP042 are judgment calls
ignore = ["SIM105", "UP042"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["RUF012","RUF043","SIM117","B017","SIM115"]  # test idioms
```

## On-save hook (Claude Code `PostToolUse`) — drop into `.claude/settings.json`
Runs `ruff` on each edited `.py` and feeds findings back so they're fixed immediately:
```json
{"hooks":{"PostToolUse":[{"matcher":"Write|Edit","hooks":[{"type":"command","command":"f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty'); case \"$f\" in *.py) o=$(ruff check \"$f\" 2>/dev/null); [ -n \"$o\" ] && jq -n --arg c \"ruff:\\n$o\" '{hookSpecificOutput:{hookEventName:\"PostToolUse\",additionalContext:$c}}';; esac; exit 0"}]}]}}
```
(Optionally add a `*.sh) shellcheck "$f"` arm to the same `case`.)

## CI step (Gitea Actions) — add to the existing workflow
```yaml
      - name: Lint (ruff)
        run: |
          pip install ruff pyright
          ruff check .
          ruff format --check .
          pyright || true   # tighten to hard-fail once types are clean
      - name: Lint (shell)
        run: shellcheck $(git ls-files '*.sh')
```

## Rollout (your call on how aggressive)
1. **Mechanical sweep:** `ruff check . --fix` + `ruff format .` — one commit, zero semantic change.
2. **Hand-fix** the few real findings (the baseline is only ~2 + a handful from the broad select).
3. **Bake the gate:** add the `select` to `pyproject.toml`, wire the CI step, drop in the on-save hook.

projdash did exactly this in three stacked PRs — **#113** (autofix sweep), **#115**
(real fixes), **#116** (bake the gate) — as the worked example. Auto-fix-and-PR vs
manual is your choice; for a repo this size a single combined PR is probably fine.
