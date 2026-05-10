# schoen-claude-status

A two-line [Claude Code](https://claude.com/claude-code) statusline showing
context, session-wide cache hit %, 5-hour and weekly rate-limit usage with
pace projection, and total session cost — all colored by configurable
thresholds.

![statusline example](docs/example.svg)

```
[hostname] /path/to/cwd (branch)
183.7K / 1.00M (18.0%) | 15.41M / 207.4K / 99% hit | 5h: 6% +0.4h wk: 21% +9.7h | $10.66
● opus reviewing diff      | 215.4K / 1.00M (21.5%) | 4.21M / 89.3K / 97% hit | $4.82 | 1m23s
✓ sonnet exploring config  | 18.4K  / 200K  ( 9.2%) | 87.3K / 4.2K  / 95% hit | $0.21
● haiku running tests      | 5.1K   / 200K  ( 2.6%) | 18.9K / 1.1K  / 84% hit | $0.04 | 8s
```

## What you see

**Line 1** — hostname, current working directory, current git branch (if any).

**Line 2** — pipe-separated metrics with no inline labels (colors carry the
identity); fields are omitted when their data isn't available:

- **Context** — `tokens / window (used %)`, e.g. `183.7K / 1.00M (18.0%)`.
  Numerator and percent share the threshold color; denominator is mauve.
- **Cache** — `reads / writes / hit %`, e.g. `15.41M / 207.4K / 99% hit`.
  Reads are teal, writes are orange, hit % is threshold-colored. Summed
  across the session transcript and any subagent transcripts (Claude Code's
  stdin payload only carries per-turn cache data, so the script walks the
  JSONL files to get a session-wide number).
- **Quota** — labeled `5h:` and `wk:`, your 5-hour and 7-day Claude Code
  rate-limit utilization. Each is followed by a **pace projection** in
  absolute hours: `+0.4h` means you're on track to finish the window with 24
  minutes of compute headroom; `-1.3h` means you're projected to hit the cap
  before the window resets. The pace number turns yellow as a close-call
  warning when the green margin shrinks to within 5% of the window length
  (~15 minutes for the 5h window, ~8.4h for the weekly window). Requires
  Anthropic's `rate_limits` payload field — proxy setups (LiteLLM, gateways)
  typically don't surface it, so these fields silently omit on those
  configurations.

  The 5h projection extrapolates the in-window rate (`util / elapsed`); the
  weekly projection uses a **trailing-168h burn rate** derived from local
  JSONL transcripts and calibrated to %/$ via the current window's
  (util, in-window-$). This stabilizes the wk projection on day 1 of a fresh
  window where the in-window denominator is too small to be meaningful. The
  walk is cached to `~/.claude/.statusline-pace-cache.json` with a 5-minute
  TTL.
- **Cost** — total session spend, e.g. `$10.66`. From `cost.total_cost_usd`
  on Claude Code's payload (matches `/usage`, includes subagent spend).

## Color thresholds

Context thresholds gate on raw token counts (the underlying limits — 33K
compact buffer, 200K Opus-1M pricing boundary — are themselves token
quantities, not fractions, so the gating compares tokens directly):

| field          | green       | yellow      | red             |
|----------------|-------------|-------------|-----------------|
| context (200K) | < 100K      | 100–147K    | ≥ 147K          |
| context (1M)   | < 200K      | 200–947K    | ≥ 947K          |
| cache hit %    | ≥ 90%       | 75–90%      | < 75%           |
| 5h / wk %      | < 75%       | 75–90%      | ≥ 90%           |
| cost           | < $25       | $25–$50     | ≥ $50           |
| pace ±X.Yh     | > 5% margin | 0–5% margin | < 0             |

Context red is computed as `(window_size − 33K compact buffer) − 20K margin`,
giving ~1–2 turns of headroom before auto-compact fires. Set
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) to override the compact point,
and the red band tracks it.

Context yellow is **token-anchored on 1M models** (200K, the boundary where
Opus 1M pricing doubles) and **fraction-anchored otherwise** (50%, where
model accuracy starts to degrade — a fill-fraction signal, not a token
target). The 1M case keeps the higher-cost zone visually distinct.

The cost thresholds reflect a personal per-session shape; tweak the
constants in the script if your scale differs.

## Install

```sh
git clone https://github.com/mtschoen/schoen-claude-status.git ~/schoen-claude-status
```

Then wire both the lead and per-agent statuslines into `~/.claude/settings.json`.
The repo ships an installer that does the JSON merge for you, preserving every
other key:

```sh
# macOS / Linux / Git Bash
~/schoen-claude-status/install.sh

# Windows (cmd / PowerShell)
%USERPROFILE%\schoen-claude-status\install.bat
```

It's idempotent — re-run any time and it'll just refresh the two `command`
strings to point at the current checkout. Pass `--dry-run` to preview the
merged JSON without writing.

If you'd rather edit `~/.claude/settings.json` by hand, the equivalent block
is:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/schoen-claude-status/statusline-command.sh"
  },
  "subagentStatusLine": {
    "type": "command",
    "command": "bash ~/schoen-claude-status/subagent-statusline.sh"
  }
}
```

Two scripts are needed because Claude Code dispatches `statusLine` to the lead
session and `subagentStatusLine` to each row in the agent panel — wiring only
one leaves the other half of the UI on the default rendering. The next render
picks up the change with no restart needed. Omitting `subagentStatusLine`
keeps Claude Code's default `name · description · token count` rendering in
the agent panel.

### Per-agent status lines

When `subagentStatusLine` is configured, each row in the agent panel below
the prompt is replaced with `<status icon> <model badge> <description> |
context | cache | cost | elapsed`, computed by walking that agent's own
JSONL transcript at
`~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl`.
The status icon is colored (green ✓ complete, red ✗ failed, yellow ●
running, mauve ○ queued); the model badge is magenta for Opus, cyan for
Sonnet, blue for Haiku. Elapsed is dropped once the row reaches a terminal
state.

The same color thresholds apply per agent. The quota field is omitted —
it's account-global, identical for every agent, so it would just clutter
the panel. Cost is derived from per-Mtok rates (the per-task payload doesn't
carry `cost.total_cost_usd`); accuracy is within a few percent of `/usage`
for non-Opus-1M turns.

### Requirements

- `bash`, `python3` (any 3.x), `git` — present on most machines that already
  run Claude Code.
- Claude Code v2.1+ (for the rich JSON payload — earlier versions only sent
  `model` / `session_id` / `cwd`).
- Subagent statusline requires a Claude Code version that ships
  `subagentStatusLine` (see [docs](https://code.claude.com/docs/en/statusline#subagent-status-lines)).

## Why this and not [other-statusline]?

Several great Claude Code statuslines already exist; this is just mine, made
public in case it's useful. What it emphasizes:

- **Pace projection in absolute hours**, not burn-rate %, so the headroom
  signal reads as time rather than ratio.
- **Session-wide cache hit %** by walking the transcript and any subagent
  JSONLs (Claude Code's stdin payload only carries per-turn cache data).
- **Single file** with no `jq` dependency — Python heredoc inside bash, no
  install step beyond `git clone`.
- **Two-line layout** so the location row stays uncluttered.

If you want progress bars, themes, or powerline glyphs,
[ccstatusline](https://github.com/sirmalloc/ccstatusline) is great. If you
want pace tracking with burn-rate % deltas,
[claude-pace](https://github.com/Astro-Han/claude-pace) is great.

## Logs

The script truncate-writes the latest stdin payload to
`~/.claude/.statusline-input.log` and any Python errors to
`~/.claude/.statusline-error.log`. Useful for diagnosing layout issues or
seeing what fields a future Claude Code version starts sending.

## License

MIT — see [LICENSE](LICENSE).
