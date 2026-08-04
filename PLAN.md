# agent-statusline - Plan

## Inbox

- [ ] Kimi line 1: optional dim `ui Xms` render-timing suffix. The data is
      already recorded per render (record_render runs for kimi;
      ~/.kimi-code/state/render-timer-*.json stays current) -- only the
      format_render_suffix call is missing (its caller lives in the
      Claude-only line-3 assembly). Pref-gate like STATUSLINE_BEACON.
      Declined 2026-07-28, captured for later.
- [ ] Kimi line 1 context duplicates kimi's built-in footer line 2 (the
      right-side context status renders even when a custom command owns
      line 1). Dropping format_context from the kimi adapter reclaims
      ~15-20 columns at zero info cost -- or keep ours (token counts +
      threshold colors vs the built-in's coarser format). Declined
      2026-07-28, captured for later.
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
      accumulates.

## Done

- Cold-start check dedupe CLOSED (2026-08-03): the e22f841 split landed
  mid-flight and left verify_render_budget.py still carrying (and running)
  the two checks that moved to verify_cold_start.py --
  check_first_render_ever_stays_fast +
  check_bias_factor_cold_cache_stays_fast, plus their _run_cold_render /
  _COLD_START_FAST_BUDGET_SECONDS helpers and a private _build_fixture_home
  copy. Removed all of it from verify_render_budget.py; it now imports
  _REPO and build_fixture_home from _render_fixture_helpers (the same way
  verify_cold_start.py already did), so the suite runs each cold-start
  check exactly once. verify_beacon_walker.py's cross-reference comment
  updated to the new home.
- Kimi Code CLI platform CLOSED (2026-07-28): new "kimi" harness wired the
  same way as qwen's wave-3 fold. `statusline_lib/kimi.py` renders kimi's
  SINGLE-line statusline from its camelCase payload (model, cwd, gitBranch,
  permissionMode, planMode, contextUsage, contextTokens, maxContextTokens,
  sessionId, version -- contract verified against kimi-code commit
  67dd03149, "status_line config" #2255): the TUI renders only the first
  stdout line, kills the command after 300ms, and throttles to 1 run/s, so
  the render path is payload-only + in-process helpers (no git subprocess --
  gitBranch comes from the payload -- no transcript walk, no cost columns;
  the payload carries no cost/cache/rate-limit data at all). Composition:
  `<spinner> [host] cwd (branch) [sid8] | model | usedK / windowK (P%) |
  non-default permission mode (red yolo / yellow other) | PLAN`. Same
  _safe_str/_safe_int type-confusion discipline as the qwen adapter (shared
  by import). `kimi_statusline.py` thin shim + `kimi-statusline-command.sh`
  mirror qwen's entry glue (no .bat: qwen's own .bat is unreferenced
  in-repo); `statusline.py` main() gained the `platform_name() == "kimi"`
  branch (exactly one line written, sessionId returned for the render
  timer); base.py app_dir() routes kimi -> ~/.kimi-code;
  platform_commands._kimi_command_for_platform + kimi_install.py's
  `[status_line] command` tui.toml merge (codex-style text surgery, reusing
  codex_install's _table_bounds/_newline/_parse_config, tomllib re-parse
  integrity guard, items key left alone) + install.py `--platform kimi`.
  install.py's codex/kimi TOML flows were folded into one shared
  `_install_toml_platform` to keep install.py under aislop's 400-line
  file-size gate (395 lines); codex's already-current report now also
  prints its detail line. Verify coverage: verify_kimi_adapter.py,
  verify_kimi_statusline_entry.py (TID251-exempt subprocess harness like
  qwen's), verify_kimi_install.py (both OS arms via os.name patching +
  install.py wiring against a fake HOME), plus a kimi case in
  verify_prefs.py's app_dir checks. 100% statusline_lib coverage held.
  Follow-up (same day): the Windows command form was switched from
  `py -3 "<path>"` to UNQUOTED `py -3 <path>` after live verification
  against kimi-code's actual spawn semantics (Node
  spawn(cmd.exe, ["/d","/s","/c", command]) -- libuv quote-wraps +
  backslash-escapes, cmd /s strips only the outer pair, py gets a garbage
  path, exit 2). `_kimi_space_path_warning` flags a space-containing repo
  path on nt (unquoted form can't survive one), printed by install.py.

- Bias-factor async-refresher migration + slow-render phase breakdown CLOSED
  (2026-07-26, root-caused from a real 5.8s slow-render log entry): beacon.py's
  `_bias_factor_cached` (the calibrated-ETA lookup) was the last inline
  `_walker_subcommand` call left on the render path -- every sibling lookup
  (git ref, beacons-latest, session-count, pace/spend) had already been
  migrated to the stale-while-revalidate + detached-refresher pattern, but
  this one still blocked on a real `beacons-history` subprocess (2s cap) on a
  60s TTL miss. Migrated onto the same pattern: `_bias_factor_cached` now only
  reads the cache (serving stale/neutral on a miss) and spawns
  `refresh_bias_factor_cache` via `maybe_spawn_refresh`; confirmed by sweeping
  every `run_captured`/`_walker_subcommand` call site in statusline_lib --
  all now live exclusively inside `refresh_*` functions reachable only from
  the detached child. Also added `statusline_lib.rendertimer.PhaseTimer`
  (per-render checkpoint accumulator) and `refresh.spawn_timings()`
  (per-spawn-call-site timing, reset per render): a render crossing
  `_SLOW_RENDER_SECONDS` now gets a breakdown appended to the log line
  (`walk=`, `gitref=`, `beacon=[bias-refresh-spawned]`, `spawns=NxTs`)
  instead of a bare total, so the next spike is diagnosable from the log
  alone. New/extended verify coverage: verify_beacon_walker.py's bias-cache
  suite rewritten for the SWR contract, verify_refresh_spawner.py covers the
  new dispatch entry + spawn-timing instrumentation, and a new
  verify_phase_timer.py covers PhaseTimer/start_phase_timer/
  current_phase_timer directly (split out of verify_render_timer.py once
  the combined file crossed aislop's file-size gate). 100% statusline_lib
  coverage held; aislop stayed at the pre-change 93/100 baseline after
  fixing two self-introduced regressions along the way (main() function
  length, a chained-dict-get pattern in a new test).
- Render-perf ratchet step 3, remainder CLOSED (2026-07-19, claude/budget-
  ratchet): the async-refresher split covered pace/spend in 2026-07-16; the
  residual per-render work it left inline -- git ref, beacons-latest,
  session count -- moved onto the identical stale-while-revalidate +
  detached-refresher pattern. New `statusline_lib/gitref.py` (moved out of
  statusline.py, which is coverage-exempt entry glue, so the 100%-coverage
  gate now actually holds `_git_command`/`_git_ref_raw_cached` to account);
  `beacon_cache._beacons_latest_cached` and `sessions.count_active_sessions`
  converted the same way (`refresh_beacon_latest_cache`,
  `refresh_session_count_cache`). `statusline_lib/refresh.py` generalized
  to accept string cache keys (cwd, session id) alongside the existing
  numeric window timestamps, dispatch table (`_REFRESHER_MODULES`) replacing
  the growing if/elif chain aislop flagged as repetitive dispatch.
  `ttlcache.read_raw_cache` added alongside `read_ttl_cache` so single-value
  callers can serve a stale entry instead of discarding it. Motivating
  measurement: an uncached psutil process-tree scan (session count) cost
  ~120ms on this machine (~600 processes) -- far past the target and the
  actual class of problem the pattern exists to prevent, just at a shorter
  timescale than the 2026-07-16 incident. `_CORE_BUDGET_MS` ratcheted
  100 -> 10 (the Pi bridge's per-keypress budget): measured median across
  30 repeated runs of the real conformance check on this machine is ~1-6ms
  (25-sample batch: min 1.09ms, median 2.02ms, p90 4.60ms, max 5.95ms),
  30/30 repeated runs passing at the 10ms budget with 2-5x margin. 100%
  statusline_lib coverage held (new gitref.py fully covered, including the
  real `_git_command` success/failure/timeout branches); aislop/ruff clean.


- install.py nudge-hook dedup bug RECONCILED (2026-07-19, no code change):
  the inbox item described a singular `_find_nudge_hook`/`_upsert_nudge_hook`
  pair that returned only the first `UserPromptSubmit` match; the live code
  had already moved to a plural, dedup-aware `_find_nudge_hooks` /
  `_merge_nudge_hook` (statusline_lib/nudge_install.py) back in the wave-2
  extraction (bdb7dc5), which updates one match in place, removes any
  further matches, and prunes emptied matcher groups --
  scripts/verify_install_nudge_merge.py's `_check_stale_duplicate_removed`
  already covers exactly this duplicate scenario and was reconfirmed green.
  This entry was stale; closing it without a code change.
- install.py shrunk below aislop's 400-line file-size threshold CLOSED
  (2026-07-19): 473 -> 389 lines by extracting pure, no-I/O logic into two
  new statusline_lib modules -- `platform_commands.py`
  (`_commands_for_platform`, `_qwen_command_for_platform`, the Pi loader
  path/contents, `STATUSLINE_REFRESH_SECONDS`) and `qwen_install.py` (the
  Qwen `ui.statusLine` merge) -- following the existing nudge_install.py /
  claude_family_install.py / codex_install.py pattern. New 100%-covered
  tests in scripts/verify_install_qwen_pi.py, plus an added
  posix-claude-branch check in scripts/verify_install_platform_routing.py.
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
