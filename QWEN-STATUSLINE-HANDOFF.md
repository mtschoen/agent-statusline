# Handoff: Qwen Statusline Port - Web Search Needed

## What We've Done

Successfully ported the Claude Code statusline to Qwen Code:

**Created/Modified files:**
- `qwen_statusline.py` — Qwen statusline entry point
- `qwen-statusline-command.sh` / `qwen-statusline-command.bat` — shell shims
- `statusline_lib/cost.py` — added Qwen formatters and model badges
- `statusline_lib/__init__.py` — exported new functions
- `statusline_lib/sessions.py` — added qwen/qwen.exe to process detection
- `install.py` — added --platform qwen and --platform both support
- `~/.qwen/settings.json` — installed statusLine config

**Working statusline output:**
```
qwen3.6·35b | 68.4K / 524.3K (13.0%) | 1.78M / 660K / 73% | ↑2.44M ↓35.2K | 46req 372.3s | (thk10.1K) | +558/-14
```

**Clean, Claude-compatible format:**
- Cache: `read / write / hit%` (Claude style)
- Tokens: `↑input ↓output` (Claude arrows)
- Thinking tokens: `(thk 10.1K)`
- API stats: `46req 372.3s`
- File changes: `+558/-14`

## What's Left

**Primary blocker: Web search for Qwen pricing details**

We need to answer: **How does Qwen's pricing work for cache?** This determines whether the cache column makes sense for Qwen at all.

Specifically, search for:
1. **Does Qwen API have different pricing for cache reads vs cache writes?** (Claude has ~10x price difference)
2. **What does Qwen's `cached` token field represent?** Cache reads? Writes? Both?
3. **Does Qwen have a cache TTL mechanism** like Claude?
4. **What are Qwen's token pricing tiers?**

## Context: The Data

Qwen's API payload gives us these token fields:
```json
{
  "tokens": {
    "prompt": 2440000,     // total prompt tokens (includes cached reads)
    "completion": 35200,   // output tokens
    "cached": 1780000,     // subset of prompt — likely cache reads
    "thoughts": 10100      // reasoning/thinking tokens
  }
}
```

The cache column currently shows `1.78M / 660K / 73%` where:
- read = `cached` (1.78M)
- write = `prompt - cached` (660K) — i.e., non-cached prompt
- hit% = `cached / prompt` (73%)

This is semantically different from Claude's cache column. Claude's `cache_read` and `cache_creation` are explicitly priced differently, so the hit rate signals cost. If Qwen prices all prompt tokens the same regardless of caching, then the cache column is misleading for its purpose (cost prediction).

## What the Agent Should Do

1. **Web search** for Qwen API pricing details (cache, token rates, TTL)
2. **Determine**: Does the cache column accurately predict cost for Qwen?
3. **If yes**: Keep current implementation
4. **If no**: Either repurpose the cache column or remove it
5. **If Qwen has no cache distinction**: Consider showing just hit% without the read/write breakdown (since "write" isn't really a cache write)
