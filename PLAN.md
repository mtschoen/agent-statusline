# schoen-claude-status — Plan

## Inbox

- [ ] Qwen entry-point type-confusion hardening: wrong-TYPED payload fields
      (e.g. context_window_size:"x", model.display_name as int) still crash
      paths shared with the Claude adapter (badge.format_model_badge etc.).
      Deferred from the 2026-07-11 qwen polish (null/missing-key class was
      fixed); belongs to the wave-3 canonical-model adapters, which validate
      types at the boundary. Also: metrics.models as a non-dict (JSON array)
      needs one isinstance guard in _model_summaries.
- [ ] Wave-3 canonical-model deliverable (decided 2026-07-11): fold
      qwen_statusline.py into statusline.py as `--statusline-platform qwen`
      — one entry point, per-harness adapters normalize payloads into the
      canonical model. Precedent: antigravity already routes through
      statusline.py via the same flag. Do NOT rename statusline.py
      (it is the generic renderer, and every deployed machine's settings
      embed the literal path).
- [ ] Codex: optionally wire tui.terminal_title (same item vocabulary as
      status_line, second ordered array; doubles the TOML-surgery surface —
      deliberately skipped in the 2026-07-11 preset refresh).

- [ ] Render-perf ratchet step 3 (conformance test:
      scripts/verify_render_budget.py, warm-core budget now 100ms — see
      Done for steps 1+2). Wave-3 async-refresher split: renders NEVER pay a
      TTL-miss walk inline — the render uses the stale cache and kicks a
      detached refresher for the next render (kills the p90/max tail).
      Target: cached-path core < 10ms, which is also the Pi bridge's
      per-keypress budget. Blocking on first/second render (cwd/git basics)
      stays acceptable per the cold budget (8s, realistically ~1s).

## Done

- Render-perf ratchet steps 1+2 CLOSED (2026-07-11, sdd/ratchet-12): TTL disk
  caches (2.5s TTL, atomic tmp+os.replace writes, keyed per-cwd/per-session
  so concurrent renders never clobber each other) for (1) `_git_ref`'s two
  git subprocess calls (statusline.py `_git_ref_raw_cached`) and (2) the
  beacons-latest walker lookup (new `statusline_lib/beacon_cache.py`,
  `_beacons_latest_cached` — split into its own module to keep beacon.py
  under the complexity gate's line-count threshold). Measured on this
  machine's fixture environment (scripts/verify_render_budget.py's
  warm-core check, median of 9 in-process renders): pre-cache 48-51ms,
  post-cache 2-3ms (8 of 9 renders hit the warm cache after the first
  miss) — well under the <50ms target. `_CORE_BUDGET_MS` lowered
  350 -> 100 with real headroom. aislop/ruff/100%-coverage all clean
  (verified against the pre-change baseline score to confirm no
  regression). Step 3 (async-refresher split) remains in the inbox.

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
