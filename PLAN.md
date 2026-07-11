# schoen-claude-status — Plan

## Inbox

- [ ] Qwen entry-point type-confusion hardening: wrong-TYPED payload fields
      (e.g. context_window_size:"x", model.display_name as int) still crash
      paths shared with the Claude adapter (badge.format_model_badge etc.).
      Deferred from the 2026-07-11 qwen polish (null/missing-key class was
      fixed); belongs to the wave-3 canonical-model adapters, which validate
      types at the boundary. Also: metrics.models as a non-dict (JSON array)
      needs one isinstance guard in _model_summaries.
- [ ] Codex: optionally wire tui.terminal_title (same item vocabulary as
      status_line, second ordered array; doubles the TOML-surgery surface —
      deliberately skipped in the 2026-07-11 preset refresh).

## Done

- 2026-06-10 triage batch CLOSED (2026-07-11, wave-1 subagent fan-out; all
  TDD'd, 100% coverage held): (1) thk spacing 447ef0d; (2) badge threshold
  ordering 3e57d76; (3)+(4) beacon eta coercion + per-period bias cache
  e777735 (+TTL-expiry test 199eb0e); (5) pace seen_ids poisoning 66dc911;
  (6) weekly_sustainable_rate guard 1a38f5b; (7) ramp_color_for degenerate
  warn==danger -> neutral midpoint c38b6bc. Bonus: the live format_cache
  cost-string crash from the production error log was confirmed already
  fixed by 5a41d8d (never existed in committed history) and is now
  regression-locked by fe37636.

- Qwen cache-column semantics RESOLVED (2026-07-11, 56625c7): Alibaba Model
  Studio docs confirm implicit-cache hits bill at ~20% of standard input
  price and there is no priced write side. Column now renders truthful
  `cached / hit%`; the fake write figure (Claude CACHE_WRITE styling) is
  gone; dead helper cachefmt.format_cache_counts removed.

- Quality gate back to green (2026-06-10): moved the nudge-hook merge
  helpers from install.py into statusline_lib/nudge_install.py so the
  verify script imports a recognized local package, clearing the
  ai-slop/hallucinated-import false positive on repo-local `import
  install` (aislop only resolves package dirs with `__init__.py`, not
  single-file modules). Also split the chained `.get(..., {})` lookup
  and ran ruff format. aislop 100/100, ruff clean, all 23 verify
  scripts pass.

- Optional native-walker integration (commit cc548d7): C++ (simdjson)
  was the bench winner at ~95ms cold, so detection was wired against
  the canonical `~/claude-walker/cpp/build/...` paths.
  `$CLAUDE_WALKER_BIN` override + PATH lookup. install.py prints which
  mode is active. Cache TTL also dropped 30s → 15s.
- Parallelize `_walk_pace_buckets` (commit 2b5e355): orjson + 8-worker
  ProcessPoolExecutor over per-session groups. 750ms → 248ms median,
  bit-exact match against the original. Cache TTL shortened 60s → 30s.
