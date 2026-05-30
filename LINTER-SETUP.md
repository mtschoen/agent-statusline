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

## AI-slop gate (aislop)

**aislop** (https://github.com/scanaislop/aislop) is a language-agnostic AI-slop
quality gate — deterministic (no LLM), 40+ rules, scored 0–100. It flags agent slop:
narrative/trivial comments, swallowed/broad exceptions, dead code, unused/hallucinated
imports, etc. It complements (does not replace) the per-language linters above and is
intended to run **per-edit + PR-gated** on all repos with supported languages.

This repo is **Python** — aislop supports Python, so the full integration applies.

> **Caveats for Python repos using this toolchain (2026-05-29, aislop 0.9.4):**
>
> - **`ai-slop/unused-import` fires on `from __future__ import annotations`.**
>   aislop flags the future-flag as an unused import — Ruff/Pyflakes specifically exempt
>   `__future__` imports; aislop 0.9.4 does not. **Do NOT remove it** — removing the
>   line changes annotation-evaluation semantics (PEP 563).
> - **FastAPI `Body(default={})`** triggers `python-mutable-default` — not a bug in a
>   FastAPI context; exclude or note as a known FP.
> - **No per-rule config in 0.9.4.** `.aislop/config.yml` supports `ci.failBelow`,
>   `exclude` globs, `extends`, and whole-engine on/off, but NOT per-rule
>   disable/severity. If `__future__` imports dominate this repo's findings and the
>   score looks misleadingly low, hold off gating until per-rule config lands.

### Per-edit (① on-save)

```bash
aislop hook install --claude --project
```

Install the Claude Code per-edit hook. **Pin the binary version** — never `@latest`
in a hook; it does a network version check on every edit.

> **What this repo actually wires (and why not the installer):** the
> `hook install` route also writes `.claude/AISLOP.md`, appends `@AISLOP.md` to
> `CLAUDE.md`, and registers a FileChanged watcher — and its `hook claude`
> callback is baseline/regression-driven (it stays silent until a baseline is
> captured). Instead, this repo adds a second `PostToolUse` arm in
> `.claude/settings.json` alongside the ruff/shellcheck one: on each `.py` edit
> it runs the **local, lockfile-pinned** binary
> `"${CLAUDE_PROJECT_DIR:-.}/node_modules/.bin/aislop" scan ... --json`
> (whole-repo, honoring `.aislop/config.yml` excludes) and feeds back only the
> diagnostics for the edited file via `additionalContext`. aislop has no
> single-file scan mode (`scan` takes a directory; `--include` is additive), so
> the scan is whole-repo and filtered by the edited file's basename.
>
> **Supply-chain hardening — no `npx --yes` against the live registry.** aislop
> is vendored as a `devDependency` in `package.json` and pinned by integrity
> hash in `package-lock.json`; install with `npm ci --ignore-scripts` (the
> `--ignore-scripts` blocks dependency lifecycle scripts). The hook and CI both
> invoke the resolved local binary, never `npx --yes aislop@x` — which fetched
> from the registry without integrity verification and executed it on every run.
> This also removes the per-edit npx-resolution latency. `package.json` is
> `private: true`, dev-tooling only (the statusline app itself is Python); a
> `lint:aislop` script keeps the dep accounted-for (knip flags an unreferenced
> tool dep as unused). Pinning is necessary but not sufficient on its own —
> `npm audit` in CI and dependency review remain the other layers.

### PR / CI gate (③)

```yaml
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Install aislop (lockfile-pinned, no install scripts)
        run: npm ci --ignore-scripts
      - name: AI-slop gate (aislop)
        run: npm run lint:aislop      # -> node_modules/.bin/aislop ci .
```

This scores the **whole repo** and exits 1 when the score falls below `ci.failBelow`.
There is no diff/changed-files mode — the gate is "don't regress the whole-repo score."
Install the pinned local binary via `npm ci` (verifies `package-lock.json`
integrity hashes) rather than `npx --yes aislop@x`, which fetches from the live
registry unverified on every run. On Gitea, do **not** use the GitHub composite
action `scanaislop/aislop@vX` (GitHub-only). Always pin a version, never `@latest`.

### Config (`.aislop/config.yml`)

```yaml
ci:
  failBelow: 80          # git-wizard's baseline; lower if FPs dominate
exclude:
  - "tests/**"           # optional — adjust to taste
# engines: { ai-slop: off }  # uncomment to disable if __future__ FPs tank score
```

Supported keys: `ci.failBelow`, `exclude`, `extends`, whole-engine on/off
(`format`, `lint`, `code-quality`, `ai-slop`, `security`, `architecture`).
Per-rule config is not available in 0.9.4.

### Rollout

Clean up first, then gate — do not ratchet from a noisy baseline. Run
`npx aislop@0.9.4 scan . -d` to see all findings, fix real slop, tune `exclude`
for irreducible FPs, then set `failBelow` to a score you can maintain. Reference:
git-wizard's gate is `failBelow: 80`.

> **Shell scripts are out of aislop's scope.** The `.sh` files in this repo are not
> a supported language; aislop silently skips them. The shell linters (`shellcheck`,
> `shfmt`) in the tiers above remain the gate for those files.

Full detail: `C:\Users\mtsch\.claude\notes\idioms_linters.md` (AI-slop gate section).
