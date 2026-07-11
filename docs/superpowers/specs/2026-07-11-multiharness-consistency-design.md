# Multi-harness consistency design (2026-07-11)

Goal: one statusline codebase serving many agent harnesses with consistent
information, visuals, and semantics — and a structure where adding a harness
is cheap.

## Harness capability tiers (surveyed 2026-07-11)

- **Full custom render** (we control the pixels): Claude Code, Antigravity
  CLI (same JSON-payload protocol), Qwen Code (similar protocol, different
  payload shape), Pi (TypeScript extension API).
- **Preset-only**: Codex (`[tui].status_line` = ordered list of built-in item
  ids in `config.toml`; no custom command hook — upstream issue #20244).
- **No hook yet**: OpenCode (user fork in progress — its hook should emit the
  Claude-shaped payload), Hermes (upstream #683), Antigravity IDE proper.

## Design

### Phase 1 — glue dedup (no behavior change)

Shared helpers in `statusline_lib` (all coverage-gated): error logger,
payload-log writer, local-mode detection, hostname, spinner frames, ctx-sum
formula; public name for `beacon._find_session_jsonl`. One shared
interpreter-probe shim sourced by the 5 shell wrappers. One JSON-settings
merge helper in `install.py` collapsing `_install_claude` /
`_install_antigravity` (~95% identical) and simplifying `_install_qwen`.

### Phase 2 — canonical session model

`statusline_lib/model.py`: harness-neutral structure of everything a
statusline can know (host, cwd, branch, model, ctx, cache, cost, quota,
sessions, timing, beacon). Per-harness adapters fill what their payload
supports; the shared renderer renders only filled fields. Fixes the Qwen
cache-semantics mismatch by construction (its adapter claims no priced cache
writes). The model is the documented contract for the OpenCode fork hook and
any future Hermes plugin: emit Claude-shaped JSON → reuse the Claude adapter.

### Phase 3 — Pi convergence (event-driven cached bridge)

Pi renders on every keypress, so per-render Python spawns (~130-270ms
measured warm) are unacceptable there. `pi-extension` becomes a thin bridge:
keeps the last rendered string in memory and returns it instantly on render
(spinner frame overlaid in TS); on turn events (`turn_start`,
`after_provider_response`, `turn_end`) it spawns the shared Python renderer
asynchronously (debounced) with canonical-model JSON and swaps the cache.
Deletes ~500 lines of drift-prone parallel TS (independent palette,
thresholds, cost fudge factors).

Codex stays preset-only until upstream ships a command hook.

## Execution waves

- **Wave 1 (parallel worktrees, independent files):**
  - A `wt/agy-fix` — diagnose+fix Antigravity: wired in settings but logs
    stale since Jun 23 while agy is in active use; NoneType/.strip and
    payload-shape errors suspected.
  - B `wt/lib-bugs` — PLAN.md bugs 2 (badge yellow unreachable), 3+4
    (beacon), 6 (pace weekly_sustainable_rate), 7 (base ramp_color_for) +
    live costfmt.py:66 str-vs-int crash in format_cache.
  - C `wt/qwen-polish` — PLAN.md bug 1 (thk spacing), 5 (pace parse-order
    dup-drop), qwen cache-column semantics (research + repurpose/drop).
  - D `wt/codex-preset` — refresh preset against current Codex item
    vocabulary; harden/verify the TOML merge.
- **Wave 2:** Phase 1 glue dedup (touches all entry scripts — must land
  after wave 1 merges).
- **Wave 3:** Phase 2 canonical model, then Phase 3 Pi bridge.

Constraints for all waves: TDD; ruff format+check; `aislop ci .`; 100% line
coverage on `statusline_lib/` via the verify suite; agents do not edit
PLAN.md or TEST-REPORT.md (controller updates them at merge).
