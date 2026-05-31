# schoen-claude-status

A multi-line [Claude Code](https://claude.com/claude-code) statusline showing
context, session-wide cache hit %, 5-hour and weekly rate-limit usage with
pace projection, total session cost, and (when a [progress-beacon](https://github.com/mtschoen/skills-progress-beacon)
is active) a live ETA for the current turn — all colored by configurable
thresholds.

![statusline example](docs/example.svg)

```
[hostname] /path/to/cwd (branch)
opus4.8[1m] | 183.7K / 1.00M (18.0%) | 15.41M / 207.4K / 99% hit | 5h: 6% +0.4h wk: 21% +9.7h↓ | $10.66
● opus4.8 reviewing diff      | 215.4K / 1.00M (21.5%) | 4.21M / 89.3K / 97% hit | $4.82 | 1m23s
✓ sonnet4.6 exploring config  | 18.4K  / 200K  ( 9.2%) | 87.3K / 4.2K  / 95% hit | $0.21
● haiku4.5 running tests       | 5.1K   / 200K  ( 2.6%) | 18.9K / 1.1K  / 84% hit | $0.04 | 8s
```

## What you see

**Line 1** — hostname, current working directory, an optional red
`[N sessions]` warning when two or more interactive Claude Code
sessions are running in this cwd, and current git branch (if any).
Detection enumerates `claude` processes whose cwd matches, excluding
`-p` headless subagents, so the warning clears the moment the other
session exits. Requires `psutil`; without it the badge stays off
entirely (see [Requirements](#requirements)).

**Line 2** — pipe-separated metrics with no inline labels (colors carry the
identity); fields are omitted when their data isn't available:

- **Model** — the active model family as a colored badge, e.g. `opus4.8[1m]`
  (magenta Opus, cyan Sonnet, blue Haiku; mauve `?` for an unrecognized
  family). The `major.minor` version is included when the id carries one, and
  the `[1m]` suffix marks the 1M-context runtime tier. Same badge
  the per-agent rows use, so the main line and the agent panel read
  consistently.
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

  The 5h projection extrapolates the in-window rate (`util / elapsed`). The
  weekly projection is **two window-local signals**, both computed only from
  the current 7-day window (no cross-week history): the **cumulative pace** —
  `util / elapsed` projected to reset, shown as the `±X.Yh` number — and a
  **current-rate forecast** that re-weights recent in-window burn (a trailing
  24h average of per-hour `$` from local JSONL transcripts, calibrated to %/h
  via the window's own util/$). The current-rate forecast renders as a colored
  arrow after the number: **↑** when your current rate is hotter than the
  cumulative pace (eating into the buffer — slow down), **↓** when cooler
  (banking surplus — go nuts), each colored by its own red/yellow/green
  verdict. When *both* signals land within ±4h of finishing exactly at reset
  (and the window is past its warm-up), the arrow is replaced by a green **☯**
  — you're riding the wave to 100% right at the cutoff. A day-1 prior shrinks
  both signals toward "on track" early in the window, so a fresh window doesn't
  read a wild projection off a tiny denominator. The per-hour walk is cached to
  `~/.claude/.statusline-pace-hourly-cache-v1.json` with a 15s TTL.

  The trailing-24h current-rate estimator was chosen empirically: a backtest
  (`scripts/backtest_pace.py`) replays candidate estimators against
  reconstructed historical windows, and trailing-24h was by far the most stable
  (EWMA/slope variants flap or explode on bursty usage). The shipped default
  lives in `statusline_lib/project.py` (`DEFAULT_PARAMS`).
- **Cost** — session spend. Rendered `($parent + $sub~) = $total` when subagents
  ran, else just `$parent`. The parent figure is the harness's authoritative
  `cost.total_cost_usd` (matches `/usage`), but it is PARENT-ONLY — subagents
  run as isolated sessions invisible to the payload. `+ $sub~` is our own
  formula's estimate of subagent spend, walked from the agent transcripts; the
  trailing `~` is the estimate marker, grey when our formula tracks the harness
  and tinted by drift direction/severity otherwise. `= $total` is their sum,
  carrying its own higher color bands (see table) so a combined burn that
  neither figure shows on its own still flags.

**Line 3 (beacon)** — appears only when the agent has emitted a live
[progress-beacon](https://github.com/mtschoen/skills-progress-beacon) for
the current turn. Example:

```
⏱ turn 14:32 (8m) · step 14:38 (2m) · ~5m · resolving merge conflict  ·  ~17m calibrated (3.5×)
```

Field-by-field:

- `⏱` — icon marker; a live beacon exists for the current turn.
- `turn HH:MM (Nm)` — wall-clock time the `begin` beacon was emitted (the
  start of the current turn) and minutes elapsed since. A *turn* is one
  human prompt + the agent's full response to it; only your prompts open
  a new turn. Internal thinking, tool calls, and `AskUserQuestion`
  round-trips all stay within the same turn.
- `step HH:MM (Nm)` — wall-clock time of the most recent `report` beacon
  within this turn, and minutes since. The "I'm still working" anchor —
  if `step` is many minutes old, the agent has gone heads-down without
  checking in. Omitted before the first report fires.
- `~Nm` — the agent's own estimate of wall-clock time remaining until
  the **end of this turn** (not per-step). Read as "the agent thinks
  it's ~5 minutes from done."
- `summary` — the agent's one-line description of current work,
  truncated to 60 chars.
- `~Nm calibrated (X.Yx)` — the agent's `~Nm` multiplied by a bias
  factor derived from the fleet's recent (begin, end) beacon pairs (7-day
  median of `active_elapsed / begin_eta`, gated on n ≥ 20 pairs). Only
  rendered when calibration data is available.

The whole line is colored by **drift**, computed from observed wall-clock
against the original begin estimate:
`(elapsed_so_far + current_eta) / original_begin_eta`. Green = nominal
(< 1.5×), yellow = moderate (1.5–2×), red = material (≥ 2× or elapsed
> 30 min). This catches the lowballed-and-kept-lowballing pattern that
the agent's own self-assessment couldn't flag. Two failure-mode
renderings replace the normal layout when something is off:

- `⏱ no begin · ~Nm · summary` (red) — a `report` fired without a
  preceding `begin` for this turn. The figure may still be useful but
  the wall-clock anchor is missing; the agent should emit a `begin`
  in its next message.
- `⏱ stale Nm` (red) — no beacon has been refreshed in 5+ minutes
  during a live turn. The agent has gone heads-down on its own promise
  to check in.

When the agent emits an `end` beacon, line 3 disappears until the next
turn opens a new beacon lifecycle.

## Color thresholds

Context thresholds gate on raw token counts (the underlying limits — 33K
compact buffer, 200K Opus-1M pricing boundary — are themselves token
quantities, not fractions, so the gating compares tokens directly):

| field          | green       | yellow      | orange      | red             |
|----------------|-------------|-------------|-------------|-----------------|
| context (200K) | < 100K      | 100–147K    | —           | ≥ 147K          |
| context (1M)   | < 200K      | 200–500K    | 500–947K    | ≥ 947K          |
| cache hit %    | ≥ 90%       | 75–90%      | —           | < 75%           |
| 5h / wk %      | < 75%       | 75–90%      | —           | ≥ 90%           |
| cost (each)    | < $25       | $25–$50     | —           | ≥ $50           |
| cost (= $ sum) | < $35       | $35–$70     | —           | ≥ $70           |
| pace ±X.Yh     | > 5% margin | 0–5% margin | —           | < 0             |

Context red is computed as `(window_size − 33K compact buffer) − 20K margin`,
giving ~1–2 turns of headroom before auto-compact fires. Set
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) to override the compact point,
and the red band tracks it.

Context yellow is **token-anchored on 1M models** (200K, the boundary where
Opus 1M pricing doubles) and **fraction-anchored otherwise** (50%, where
model accuracy starts to degrade — a fill-fraction signal, not a token
target). The 1M case keeps the higher-cost zone visually distinct.

1M models also get an **orange mid-band at 500K** to break up the otherwise
huge yellow span between the pricing boundary and auto-compact — a halfway
cue that the session is genuinely full, not just past the pricing line.

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
- Optional: `pip install psutil` enables the `[N sessions]` warning on
  line 1. Without it the warning is suppressed; everything else works
  identically.

## Why this and not [other-statusline]?

Several great Claude Code statuslines already exist; this is just mine, made
public in case it's useful. What it emphasizes:

- **Pace projection in absolute hours**, not burn-rate %, so the headroom
  signal reads as time rather than ratio.
- **Session-wide cache hit %** by walking the transcript and any subagent
  JSONLs (Claude Code's stdin payload only carries per-turn cache data).
- **Single file** with no `jq` dependency — Python heredoc inside bash, no
  install step beyond `git clone`.
- **Layered layout** — location row stays uncluttered on line 1, metrics
  on line 2, live turn ETA on line 3 only when a beacon is active.

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
