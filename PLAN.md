# schoen-claude-status — Plan

## Inbox

- [ ] Optional native-walker integration: when `~/.claude/walker` (or
      `walker.exe`) exists and is executable, `_walk_pace_buckets` should
      subprocess it and use the returned `(trailing_usd, window_usd)`,
      falling back silently to the existing parallel Python walker on any
      failure. The native walker itself lives in a separate repo —
      `~/claude-walker` — with side-by-side Rust / Go / C++ / Zig
      implementations + conformance corpus + bench harness. Wait until
      the C++ (simdjson) and Go (sonic-go) follow-ups land so the
      "winner" choice is informed before wiring detection.

## Done

- Parallelize `_walk_pace_buckets` (commit 2b5e355): orjson + 8-worker
  ProcessPoolExecutor over per-session groups. 750ms → 248ms median,
  bit-exact match against the original. Cache TTL shortened 60s → 30s.
