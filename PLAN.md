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
- [ ] Parked from the 2026-07-11 final whole-branch review (cosmetic, no
      urgency): split base.py's entry-script glue (hostname/spinner_frame/
      safe_write/log_traceback/is_local_mode) into entryglue.py IF more glue
      accumulates; further shrink install.py below aislop's 400-line
      file-size threshold (473 after the wave-2 extraction).

- [ ] Render-perf ratchet step 3, remainder (conformance test:
      scripts/verify_render_budget.py, warm-core budget still 100ms): the
      async-refresher split landed 2026-07-16 for the two walk-priced
      sources (see Done); what remains is ratcheting `_CORE_BUDGET_MS`
      100 -> 10 once the residual per-render work (git ref, beacons,
      session count) fits. The <10ms cached path is also the Pi bridge's
      per-keypress budget. Blocking on first/second render (cwd/git basics)
      stays acceptable per the cold budget (8s, realistically ~1s).
- [ ] install.py dedup bug: `_find_nudge_hook` returns only the FIRST
  `UserPromptSubmit` entry whose command contains `wrap_nudge.py`. If two ever
  coexist (e.g. an old plain-format entry plus the current logging+`exit 0`
  one), `_upsert_nudge_hook` updates one and leaves the duplicate, so the nudge
  fires twice per prompt. Fix: scan ALL `UserPromptSubmit` groups, keep/update a
  single matching hook, and remove the extras. Found in schoen's live
  settings.json on 2026-06-08 (manually deduped there).

## Done

- Async-refresher split (render-perf step 3, walk-priced sources) CLOSED
  (2026-07-16, fixing the frozen-statusline incident on llamabox): renders
  never pay a TTL-miss transcript walk inline. `statusline_lib/refresh.py`
  spawns a detached recompute child (inflight-marker debounced, claim
  released on spawn failure); `pace._pace_hourly_cached` and
  `burnrate._window_spend_cached` serve stale entries while it runs and
  degrade honestly ([] / 0.0 / nearest trailing-window grid cell) on a
  true miss. Both caches went multi-entry v2 (v1 files abandoned in
  place); readers drop non-dict entries so torn/mixed-format files can't
  crash a render. Measured: cold render 5.9s -> 0.18s, warm 0.09s. New
  verify scripts: verify_pace_refresh / verify_spend_refresh /
  verify_refresh_spawner (the last runs the real child snippet in a real
  interpreter against a fixture corpus). Root cause of the incident: with
  an SMB extra root the inline walk cost ~5.5s, Claude Code replaces the
  render at refreshInterval (~3s), a killed render never reaches its cache
  write, so the cache could never warm and the line froze at the session's
  first pre-token render (`0 / 1.00M`).

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
