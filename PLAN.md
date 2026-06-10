# schoen-claude-status — Plan

## Inbox

- [ ] Qwen cache-column semantics: research Qwen API pricing (is `cached`
      discounted vs non-cached prompt tokens? TTL? tiered rates?). The cache
      column (`read / write / hit%`, statusline_lib/qwen.py) maps
      read=cached, write=prompt-cached, hit%=cached/prompt, which is
      semantically unlike Claude's (Qwen exposes no priced write side). If
      caching isn't priced differently, the column misleads as a cost
      signal: repurpose (hit% only) or drop it. Distilled from
      QWEN-STATUSLINE-HANDOFF.md (deleted 2026-06-10; full port notes in git
      history).

## Done

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
