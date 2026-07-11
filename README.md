# schoen-claude-status

A multi-line [Claude Code](https://claude.com/claude-code) statusline showing
context, session-wide cache hit %, 5-hour and weekly rate-limit usage with
pace projection, total session cost, a live `$/min` burn rate, and (when a
[progress-beacon](https://github.com/mtschoen/skills-progress-beacon)
is active) a live ETA for the current turn - all colored by configurable
thresholds. A Pi extension port lives under `pi-extension/` and reuses the
same visual language against Pi's native session usage data.

What it looks like at a typical terminal width (compact auto-shedding has
dropped the widest embellishments so the line fits):

![statusline example](docs/example.svg)

On a wide terminal everything renders: the four-component cost breakdown
(`input / read / write / output`, each with its cumulative `$`), the TTL
eviction counter, the quota pace numbers, the target-rate arrow with the
pace needle, the subagent cost split, and the diffstat. Shown zoomed out so
the colors read at a glance - click for full size:

![full-width statusline example](docs/example-full.svg)

The same full-width render as text:

```
⠹ [my-laptop] ~/projects/example-app (feature/example:3f2c9a1) [a1da9354] Investigating the flaky test
opus4.8[1m] | 183.7K / 1.00M (18.0%) | 12.1K ($0.06) / 15.41M ($1.54) / 207.4K ($1.29) / 48.2K ($1.21) / 99% hit | ⚠ TTL:1 (~$0.31) | 5h: 6% +0.4h wk: 21% +9.7h | $1.02/min →$1.35 ↓ | ($10.66 + $4.82~) = $15.48 | +543/-113
⏳ 1h02m · 27m api  ·  ⏱ turn 14:32 (8m) · step 14:38 (2m) · ~5m · resolving merge conflict  ·  ~17m calibrated (3.5×)
# API-key session (no rate_limits), STATUSLINE_DAILY_BUDGET=100:
opus4.8[1m] | 183.7K / 1.00M (18.0%) | 15.41M / 207.4K / 99% hit | day: 47% | $1.02/min↓ | $10.66 | +543/-113
● opus4.8 reviewing diff      | 215.4K / 1.00M (21.5%) | 4.21M / 89.3K / 97% hit | $4.82 | 1m23s
✓ sonnet4.6 exploring config  | 18.4K  / 200K  ( 9.2%) | 87.3K / 4.2K  / 95% hit | $0.21
● haiku4.5 running tests       | 5.1K   / 200K  ( 2.6%) | 18.9K / 1.1K  / 84% hit | $0.04 | 8s
```

## What you see

**Line 1** - a render-tick spinner, hostname (muted mauve), the session's
launch directory (`workspace.project_dir`) as a stable anchor - and, when the
session has `cd`'d elsewhere, the current directory appended after it in
desaturated-teal `[brackets]` (so the home stays the visual anchor while the
move reads as secondary): a `./`-prefixed path relative to home when the move
stayed nested inside it, or the absolute path when it broke out above home or
onto another drive, an
optional red `[N sessions]` warning when two or more interactive
Claude Code sessions are running in this cwd, the git ref as `branch:hash`
(the short commit hash tinted tan so it reads apart from the branch), a short
`[session-id]` badge (first hex group of the session UUID, steel blue - handy
for matching a statusline to its transcript file), and the session title.
The spinner advances on every render, so a frozen glyph means the statusline
subprocess has stopped being invoked. An orange `LOCAL` tag appears before the
hostname when `CLAUDE_LOCAL_MODE=1` is set or `~/.claude/.local-mode` exists.
Multi-session detection enumerates `claude` processes whose cwd matches,
excluding `-p` headless subagents plus anything that fails a process-tree
test: descendants of another claude/qwen process (helpers, update checks,
spawned agent runtimes) and orphans whose launching shell is dead are never
counted, so only shell-launched interactive sessions trigger the warning,
and it clears the moment the other session exits. Requires `psutil`; without
it the badge stays off entirely (see [Requirements](#requirements)). When Claude Code supplies a
`session_name` (its auto-generated title for the session) and there's room
for it within `$COLUMNS`, it's appended in muted grey after the badges -
dropped first if the terminal is too narrow, so it never pushes the path off
screen. The session-id badge, by contrast, is tiny and always shown.

**Line 2** - pipe-separated metrics with no inline labels (colors carry the
identity); fields are omitted when their data isn't available:

- **Model** - the active model family as a colored badge, e.g. `opus4.8[1m]`
  (magenta Opus, cyan Sonnet, blue Haiku, green Fable). An unrecognized family
  falls back to the payload's human-readable name (e.g. `Fable 5`), or the raw
  id sans `claude-` prefix when none is given - a mauve `?` only when there's
  nothing else to show. The version is included when the id carries one, and
  the `[1m]` suffix marks the 1M-context runtime tier. Same badge
  the per-agent rows use, so the main line and the agent panel read
  consistently.
- **Context** - `tokens / window (used %)`, e.g. `183.7K / 1.00M (18.0%)`.
  Numerator and percent share the threshold color; denominator is mauve.
- **Cache** - the per-token cost breakdown. Its essential core is
  `reads ($R) / writes ($W) / hit %`, e.g.
  `15.41M ($1.54) / 207.4K ($1.29) / 99% hit` - reads teal, writes orange,
  hit % threshold-colored. On a wide enough terminal this expands to the full
  **four-component** form
  `input ($I) / read ($R) / write ($W) / output ($O) / hit %`, e.g.
  `12.1K ($0.06) / 15.41M ($1.54) / 207.4K ($1.29) / 48.2K ($1.21) / 99% hit`:
  the **input** figure (steel blue) is fresh full-price input tokens and the
  **output** figure (violet) is generated output - the two components the cache
  pair alone omits, so all four together sum to the session token cost. Input
  and output are the first fields to shed under width pressure (see
  [Compact mode](#compact-mode)), leaving the read/write/hit% core. All of it is
  summed across the session transcript and any subagent transcripts (Claude
  Code's stdin payload only carries per-turn cache data, so the script walks the
  JSONL files to get a session-wide number). Each token count is followed by its
  cumulative session cost in parentheses, colored to match its figure's identity.
  Costs are model-accurate (summed per turn at each turn's model rate, including
  subagent transcripts, matching the token totals).
- **TTL evictions** - a loud red `⚠ TTL:N (~$X.XX)` segment, shown only
  when the session had cache evictions. An eviction is any parent-session
  turn after the first that paid a substantial cache-write (>=1000 tokens)
  with zero cache reads
  (TTL expiry, compaction, `--resume` past the TTL, or the tool-reorder
  bug) - the cached prefix was gone and had to be rewritten. `~$X.XX`
  estimates the spend wasted re-warming the prefix (the 1.25x write
  penalty over the 0.1x a warm read would have cost). Subagent transcripts
  are excluded (their first turn is always a full write).
- **Quota** - labeled `5h:` and `wk:`, your 5-hour and 7-day Claude Code
  rate-limit utilization. Each is followed by a **pace projection** in
  absolute hours: `+0.4h` means you're on track to finish the window with 24
  minutes of compute headroom; `-1.3h` means you're projected to hit the cap
  before the window resets. The pace number turns yellow as a close-call
  warning when the green margin shrinks to within 5% of the window length
  (~15 minutes for the 5h window, ~8.4h for the weekly window). Requires
  Anthropic's `rate_limits` payload field - proxy setups (LiteLLM, gateways)
  typically don't surface it, so these fields silently omit on those
  configurations.

  The 5h projection extrapolates the in-window rate (`util / elapsed`). The
  weekly projection is **two window-local signals**, both computed only from
  the current 7-day window (no cross-week history): the **cumulative pace** -
  `util / elapsed` projected to reset, shown as the `±X.Yh` number - and a
  **current-rate forecast** that re-weights recent in-window burn (a trailing
  24h average of per-hour `$` from local JSONL transcripts, calibrated to %/h
  via the window's own util/$). The `wk:` field shows only the `±X.Yh`
  cumulative-pace number; the current-rate forecast renders as a colored
  **needle** on the **burn rate** field below instead (relocated there so
  the single `$/min` field is the one home for the arrow/glyph): **↑** when your
  current rate is hotter than the cumulative pace (eating into the buffer - slow
  down), **↓** when cooler (banking surplus - go nuts), each colored by its own
  red/yellow/green verdict. When *both* signals land within ±4h of finishing
  exactly at reset (and the window is past its warm-up), the arrow is replaced by
  a green **☯** - you're riding the wave to 100% right at the cutoff. A day-1
  prior shrinks both signals toward "on track" early in the window, so a fresh
  window doesn't read a wild projection off a tiny denominator. The per-hour walk
  is cached to `~/.claude/.statusline-pace-hourly-cache-v1.json` with a 15s TTL.

  The trailing-24h current-rate estimator was chosen empirically: a backtest
  (`scripts/backtest_pace.py`) replays candidate estimators against
  reconstructed historical windows, and trailing-24h was by far the most stable
  (EWMA/slope variants flap or explode on bursty usage). The shipped default
  lives in `statusline_lib/project.py` (`DEFAULT_PARAMS`).
- **Burn rate** - a live `$/min` spend rate over the trailing 5 minutes,
  aggregated across **all** local sessions (cross-machine, via the same walker
  roots the pace walk uses). Funny-money units, like the cost field. It is the
  single home for the pace **needle** (`↑`/`↓`/`☯`): on a subscription the needle
  is the weekly rate-limit forecast (relocated off `wk:`, which keeps its `+Hh`
  cumulative number); on an API-key session with `STATUSLINE_DAILY_BUDGET` set it
  is a burst-robust 24h integral - `↓` when your trailing-24h spend is under the
  daily budget, `↑` when over (red past 1.5x), `☯` when within 5% of it. The rate
  number is neutral grey; the needle carries the verdict color. The 5-min number
  and the 24h needle describe different timescales by design, so they can briefly
  disagree on bursty days.
- **Daily budget (API-key only)** - when there is no `rate_limits` payload and
  `STATUSLINE_DAILY_BUDGET` (funny-money dollars/day) is set, a `day: NN%` field
  shows spend since local midnight as a fraction of the budget. The midnight
  boundary only defines "today" - there is no deadline or projection. This is
  **cross-session today** (every session since midnight), a deliberately different
  scope from the per-session cost figure.
- **Cost** - session spend. Rendered `($parent + $sub~) = $total` when subagents
  ran, else just `$parent`. The parent figure is the harness's authoritative
  `cost.total_cost_usd` (matches `/usage`), but it is PARENT-ONLY - subagents
  run as isolated sessions invisible to the payload. `+ $sub~` is our own
  formula's estimate of subagent spend, walked from the agent transcripts; the
  trailing `~` is the estimate marker, grey when our formula tracks the harness
  and tinted by drift direction/severity otherwise. `= $total` is their sum,
  carrying its own higher color bands (see table) so a combined burn that
  neither figure shows on its own still flags.
- **Lines** - a `+A/-B` session diffstat (green added, red removed) straight
  from the payload's `cost.total_lines_added` / `total_lines_removed`, e.g.
  `+543/-113`. Counts use the same thousands-compaction as the token figures
  (`+1.2K/-340`). It's a productivity signal, not money, so unlike the cost
  fields it **survives `STATUSLINE_HIDE_COST`**; it's gated only by width
  (drops fairly early in compact mode). Omitted until something has changed.

**Line 3** - session timing, then the weekly-quota exhaustion clock (only past
90%), then the live turn beacon. Any part may be absent: the timing shows
whenever the payload carries durations (almost always), the exhaustion clock
only when the weekly quota is over 90% _and_ projected to run out before reset,
the beacon only while a turn is in flight, so line 3 appears more often than
before. Example:

```
⏳ 1h02m · 27m api  ·  ⏱ turn 14:32 (8m) · step 14:38 (2m) · ~5m · resolving merge conflict  ·  ~17m calibrated (3.5×)
```

- **Session timing** (`⏳ 1h02m · 27m api`) - wall-clock time the session has
  existed (`cost.total_duration_ms`) and time spent in model calls
  (`cost.total_api_duration_ms`), so the pair shows how compute-bound the
  session has been. Muted grey - ambient context, not a warning. The `⏳`
  hourglass is deliberately distinct from the beacon's `⏱` turn timer.

- **Weekly-quota exhaustion clock** (`wk 100% ~6:02am`) - the local clock time
  the weekly quota is projected to hit 100% at the _current_ burn rate. Shown
  only past 90% utilization _and_ when that forecast lands before the window
  resets (if you are on pace to make it to reset you will not run out, so it
  stays hidden). The weekday is prefixed only when it is not today
  (`wk 100% ~Tue 6:02am`). Tinted with the same color as line 2's `wk:` quota %,
  so past 90% it reads solid red - running out this early means the quota burned
  too fast.

The remainder of the line is the **progress-beacon** (only when one is active),
field-by-field:

- `⏱` - icon marker; a live beacon exists for the current turn.
- `turn HH:MM (Nm)` - wall-clock time the `begin` beacon was emitted (the
  start of the current turn) and minutes elapsed since. A *turn* is one
  human prompt + the agent's full response to it; only your prompts open
  a new turn. Internal thinking, tool calls, and `AskUserQuestion`
  round-trips all stay within the same turn.
- `step HH:MM (Nm)` - wall-clock time of the most recent `report` beacon
  within this turn, and minutes since. The "I'm still working" anchor -
  if `step` is many minutes old, the agent has gone heads-down without
  checking in. Omitted before the first report fires.
- `~Nm` - the agent's own estimate of wall-clock time remaining until
  the **end of this turn** (not per-step). Read as "the agent thinks
  it's ~5 minutes from done."
- `summary` - the agent's one-line description of current work,
  truncated to 60 chars.
- `~Nm calibrated (X.Yx)` - the agent's `~Nm` multiplied by a bias
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

- `⏱ no begin · ~Nm · summary` (red) - a `report` fired without a
  preceding `begin` for this turn. The figure may still be useful but
  the wall-clock anchor is missing; the agent should emit a `begin`
  in its next message.
- `⏱ stale Nm` (red) - no beacon has been refreshed in 5+ minutes
  during a live turn. The agent has gone heads-down on its own promise
  to check in.

When the agent emits an `end` beacon, line 3 disappears until the next
turn opens a new beacon lifecycle.

## Color thresholds

Context thresholds gate on raw token counts (the underlying limits - 33K
compact buffer, 250K wrap-nudge caution line - are themselves token
quantities, not fractions, so the gating compares tokens directly):

| field          | green       | yellow      | orange      | red             |
|----------------|-------------|-------------|-------------|-----------------|
| context (200K) | < 100K      | 100–147K    | -           | ≥ 147K          |
| context (1M)   | < 250K      | 250–500K    | 500–947K    | ≥ 947K          |
| cache hit %    | ≥ 90%       | 75–90%      | -           | < 75%           |
| 5h / wk %      | < 75%       | 75–90%      | -           | ≥ 90%           |
| cost (each)    | < $25       | $25–$50     | -           | ≥ $50           |
| cost (= $ sum) | < $35       | $35–$70     | -           | ≥ $70           |
| pace ±X.Yh     | > 5% margin | 0–5% margin | -           | < 0             |

Continuous numeric fields (quota `5h`/`wk` %, `day:` %, cache-hit %, the pace
`±X.Yh` delta, the burn-rate `$/min`, and the API-key budget needle arrow) render
as a smooth green->yellow->red gradient rather than discrete steps. The table
above lists the anchor thresholds; the color fades continuously between them.
Context and per-session cost keep their discrete bands (they carry a distinct
orange mid-band).

Context red is computed as `(window_size − 33K compact buffer) − 20K margin`,
giving ~1–2 turns of headroom before auto-compact fires. Set
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) to override the compact point,
and the red band tracks it.

Context yellow is **token-anchored on 1M models** (250K, the wrap-nudge caution
line - the 1M tier bills flat, so this is a context-hygiene cue, not a pricing
boundary) and **fraction-anchored otherwise** (50%, where model accuracy starts
to degrade - a fill-fraction signal, not a token target). The 1M case keeps the
caution zone visually distinct.

1M models also get an **orange mid-band at 500K** to break up the otherwise
huge yellow span between the caution line and auto-compact - a halfway
cue that the session is genuinely full.

The cost thresholds reflect a personal per-session shape; tweak the
constants in the script if your scale differs.

## Install

```sh
git clone https://github.com/mtschoen/schoen-claude-status.git ~/schoen-claude-status
```

Then configure the statusline for the target CLI. The installer merges the
platform's JSON or TOML configuration while preserving unrelated settings:

```sh
# macOS / Linux / Git Bash (Claude Code by default)
~/schoen-claude-status/install.sh

# Windows cmd / PowerShell (Claude Code by default)
%USERPROFILE%\schoen-claude-status\install.bat

# For Antigravity CLI:
~/schoen-claude-status/install.sh --platform antigravity

# For Pi:
~/schoen-claude-status/install.sh --platform pi

# For Codex CLI (native footer preset):
~/schoen-claude-status/install.sh --platform codex
```

It's idempotent - re-run any time and it refreshes only the settings it owns
(including checkout paths on command-backed platforms). Pass `--dry-run` to
preview the merged configuration without writing.

Options:
- `--platform claude`       (default) Installs to `~/.claude/settings.json`
- `--platform antigravity`  Installs to `~/.gemini/antigravity-cli/settings.json`
- `--platform qwen`         Installs to `~/.qwen/settings.json`
- `--platform both`         Installs to both Claude and Qwen
- `--platform pi`           Installs Pi extension loader to `~/.pi/agent/extensions/agent-statusline/index.ts`
- `--platform codex`        Installs a native footer preset to `~/.codex/config.toml`

If you'd rather edit settings by hand, the equivalent block
is:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/schoen-claude-status/statusline-command.sh",
    "refreshInterval": 3
  },
  "subagentStatusLine": {
    "type": "command",
    "command": "bash ~/schoen-claude-status/subagent-statusline.sh"
  }
}
```

Two scripts are needed because Claude Code dispatches `statusLine` to the lead
session and `subagentStatusLine` to each row in the agent panel - wiring only
one leaves the other half of the UI on the default rendering. Omitting
`subagentStatusLine` keeps Claude Code's default `name · description · token
count` rendering in the agent panel.

`statusLine.refreshInterval` (`install.py` writes `3`) repaints the lead's
footer every N seconds in addition to the normal event-driven updates. Without
it, the footer only redraws on lead-session activity (a new prompt, a tool
call) - so while you're idle waiting on a background Agent Teams teammate
(see [Coverage gap: Agent Teams](#per-agent-status-lines) above), the
`teammates:` line and any other time-based segment freeze at whatever they
showed on the last redraw and never move. This is a harness-level setting, not
script content, so unlike editing the `.py`/`.sh` files themselves, **it
requires a session restart (or a fresh `claude` session) to take effect.**

### Codex CLI

Codex CLI owns its TUI footer rather than invoking a command-style statusline.
Run the installer with `--platform codex` to merge the closest native preset
into `~/.codex/config.toml` while preserving unrelated TOML settings and
comments:

```toml
[tui]
status_line = [
  "run-state",
  "current-dir",
  "git-branch",
  "pull-request-number",
  "model-with-reasoning",
  "context-used",
  "five-hour-limit",
  "weekly-limit",
  "used-tokens",
  "total-input-tokens",
  "total-output-tokens",
  "branch-changes",
  "permissions",
  "approval-mode",
  "fast-mode",
  "thread-title",
  "task-progress",
]
status_line_use_colors = true
```

This maps the information Codex exposes natively: activity, cwd/branch/PR/diff,
model and reasoning, context, quota, total/input/output tokens, permissions,
approval and fast-mode state, thread title, and plan progress. The verbose
thread UUID is deliberately omitted to leave more room for live telemetry.

Codex currently has no cached-token item, caller-defined shorthand, custom ANSI
spans, external-command item, or multiline extension point. Its color toggle
uses colors derived from the active Codex `/theme`; it cannot apply this
project's Claude cache-read/cache-write/threshold palette. The Claude and Qwen
adapters share the cache-read, hit-rate, and color-ramp formatters (Qwen has no
priced cache-write side, so its cache column omits a write figure Claude's
doesn't) so those semantics are ready for Codex as soon as it exposes either
cached-token data or a command-backed footer item. Until then, the Claude-specific
cache/TTL/cost/burn-rate, transcript walker, teammate summary, and calibrated
progress-beacon rows cannot be carried over. Re-run `/statusline` inside Codex
to interactively adjust the installed field order.

### Pi extension

Pi does not run command-style `statusLine` scripts. Instead, it loads a
TypeScript extension that replaces the footer with a multi-line render. The
repo-owned entry point is `pi-extension/index.ts`; install it globally by adding
a loader at `~/.pi/agent/extensions/agent-statusline/index.ts` with an absolute
path to your checkout:

```sh
# Or run installer:
~/schoen-claude-status/install.sh --platform pi
```

to generate this file automatically from the checkout:

```ts
export { default } from "/home/you/schoen-claude-status/pi-extension/index.ts";
// Windows example: export { default } from "C:/Users/you/schoen-claude-status/pi-extension/index.ts";
```

The current Pi port renders the same core signals from Pi's native session
objects: host/cwd/git/session id, model badge, context occupancy, session-wide
input/cache-read/cache-write/output cost split, cache hit %, TTL rewrite count,
API response rate-limit headers when providers expose them, daily-budget and
live burn-rate signals, total cost, diffstat, and session/turn timing. Claude
Code-specific signals that Pi does not currently expose as the same payload
(`rate_limits` weekly quota projections, subagent transcript split, and
progress-beacon calibration) are omitted until Pi surfaces equivalent data or a
separate walker is added.

### Per-agent status lines

When `subagentStatusLine` is configured, each row in the agent panel below
the prompt is replaced with `<status icon> <model badge> <description> |
context | cache | cost | elapsed`, computed by walking that agent's own
JSONL transcript at
`~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl`.
The status icon is colored (green ✓ complete, red ✗ failed, yellow ●
running, mauve ○ queued); the model badge is magenta for Opus, cyan for
Sonnet, blue for Haiku, green for Fable. Elapsed is dropped once the row
reaches a terminal state.

The same color thresholds apply per agent. The quota field is omitted -
it's account-global, identical for every agent, so it would just clutter
the panel. Cost is derived from per-Mtok rates (the per-task payload doesn't
carry `cost.total_cost_usd`); accuracy is within a few percent of `/usage`
for non-Opus-1M turns.

**Coverage gap: Agent Teams.** `subagentStatusLine` only fires for classic
Task-tool subagents (`type: local_agent` in the payload's `tasks[]`). It is
never invoked for [Agent Teams](https://code.claude.com/docs/en/agent-teams)
teammates (`run_in_background: true` + a `name`, under
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) - confirmed empirically 2026-07-02: a
live, addressable teammate never touched
`.subagent-statusline-input.log` even once, despite Claude Code updating that
teammate's own transcript in real time. There is no documented hook for that
panel row, so it can't be intercepted. As a workaround, the **main**
statusline's line 3 renders a `teammates: <icon> name badge $cost, ...`
summary instead, reading `~/.claude/teams/<name>/config.json` and each
teammate's transcript JSONL directly (`statusline_lib/teams.py`). It won't
replace the default per-row rendering in the panel, but it gives live
visibility into teammate cost/activity from a mechanism that's actually
invoked reliably.

### Requirements

- `bash`, `python3` (any 3.x), `git` - present on most machines that already
  run Claude Code.
- Claude Code v2.1+ (for the rich JSON payload - earlier versions only sent
  `model` / `session_id` / `cwd`).
- Subagent statusline requires a Claude Code version that ships
  `subagentStatusLine` (see [docs](https://code.claude.com/docs/en/statusline#subagent-status-lines)).
- Optional: `pip install psutil` enables the `[N sessions]` warning on
  line 1. Without it the warning is suppressed; everything else works
  identically.

### Burn rate & daily budget

The `$/min` burn rate renders in every session. The pace needle's source is
**purely data-driven**: if the payload carries `rate_limits` quota data it's a
subscription (needle = weekly forecast); if not, it's treated as an API-key
session (needle = daily-budget integral, only when a budget is configured).

Set a daily target with `STATUSLINE_DAILY_BUDGET` (funny-money dollars per day,
e.g. `export STATUSLINE_DAILY_BUDGET=100`). It is ignored on subscription
sessions, where the rate limits already define the budget.

The `$/min` number itself is colored by how it compares to a target rate,
**independent of the needle** (the number tracks raw instantaneous speed; the
needle tracks quota/budget pacing). With `r = rate / target`: teal below `0.5x`
(cruising), green `0.5-1.5x` (the on-target band), a green->yellow->red gradient
`1.5-4x`, and red at/above `4x`.

The target is resolved in this order:

1. **Explicit override** - `STATUSLINE_TARGET_RATE` (funny-money dollars per
   minute) always wins when set; `0`/negative/invalid disables the coloring and
   the arrow, and the number stays neutral grey.
2. **Subscription sessions** (rate-limit quota present, env var unset) derive an
   **adaptive weekly-sustainable rate**: the remaining weekly quota (in
   funny-money dollars, calibrated from the window's own utilization/$ ratio)
   spread over the time left until the weekly reset. It rises when you are under
   pace (buffer banked) and falls when over pace. Because this is your *all-week*
   sustainable pace, it sits well below the instantaneous `$1/min` - an active
   burst will usually read "over," which is the point. Falls back to the flat
   default early in the week when utilization is too low (`< 1%`) to calibrate.
3. **Everything else** (API-key / no quota) uses a flat default of `1.0`.

The live rate is followed by a green `→$T` arrow showing the resolved target
rate, so the full burn-rate field reads `$0.85/min →$1.00 ↓` (or e.g.
`$1.20/min →$0.34 ↑` on a subscription). Compact mode and a disabled target both
suppress the arrow.

### Live control (no restart)

The status line subprocess inherits its environment once, when Claude Code
launches, so editing the `env` block in `settings.json` only takes effect on the
next restart. For *live* changes, every `STATUSLINE_*` setting can also be set in
`~/.claude/.statusline-prefs.json`, which the status line reads fresh on every
render. Resolution order is **prefs file > `settings.json` env > built-in
default**.

Manage the prefs file with `statusline_ctl.py` rather than editing it by hand:

```
python statusline_ctl.py list                 # every setting + effective value
python statusline_ctl.py set cost off          # hide all $ figures, live
python statusline_ctl.py set compact always     # force maximum compaction
python statusline_ctl.py set target-rate auto    # derive the →$ target
python statusline_ctl.py set beacon off          # hide the progress-beacon ETA
python statusline_ctl.py reset target-rate        # fall back to env/default
```

Friendly keys map to the env vars: `cost` (`STATUSLINE_HIDE_COST`, inverted),
`compact` (`STATUSLINE_COMPACT`), `target-rate` (`STATUSLINE_TARGET_RATE`;
`<$/min>`, `auto`, or `off`), `daily-budget` (`STATUSLINE_DAILY_BUDGET`),
`verbose-pace` (`STATUSLINE_VERBOSE_PACE`), and `beacon` (`STATUSLINE_BEACON`,
gates the beacon-ETA render). In Claude Code, the `/statusline` skill wraps this
CLI so natural-language requests ("hide the cost", "pin the target to $0.50") map
to the right command.

### Compact mode

`STATUSLINE_COMPACT` controls how aggressively line 2 sheds embellishments:

| value | behavior |
|-------|----------|
| `auto` (default) | measures the rendered width against `$COLUMNS` (set by Claude Code >= 2.1.153; falls back to the full line on older versions where it is unset) and progressively drops embellishments until line 2 fits |
| `0` | forces the full line (all embellishments on) |
| `1` | forces maximum compaction (all embellishments off) |

In `auto` mode, embellishments are dropped in this order until the line fits:
the full-breakdown **output** figure, then the **input** figure (the two
widest-only cost components), then the cache `$` parens, then the target-rate
arrow, then cache hit%, then the quota pace numbers, then the TTL wasted-$
estimate. The narrowest windows then enter a super-minimal tier that drops the
whole live `$/min` field, then the context percentage, and finally the context
window-size denominator. The cache read/write token counts, TTL count, session
cost, context used-token count, and model badge are never dropped - so the cache
field's essential core (read / write / hit%) is the last of it to go.

The widest layout therefore shows all four cost components -
`input (cost) / read (cost) / write (cost) / output (cost) / hit%` - which sum
to the session's token cost (the `input` and `output` figures are the two the
cache pair alone omits: fresh uncached input and generated output). They are the
first to shed, so narrower terminals fall back to the familiar
`read / write / hit%`.

### Hiding cost (`STATUSLINE_HIDE_COST`)

Set `STATUSLINE_HIDE_COST` to `1` (or `true`/`on`/`yes`) to suppress **every
dollar-denominated figure** on line 2: the session cost, the `$/min` burn rate
and its target arrow, the API-key `day:` budget, the cache `$` parens, and the
TTL wasted-$ estimate. Everything non-dollar stays - the cache read/write token
counts and hit%, the TTL eviction **count**, the context field, and the quota
`5h:`/`wk:` percentages with their `+Hh` time-to-limit projections:

```
opus4.8] | 165.3K / 1.00M (16.5%) | 6.79M / 250.6K / 96% hit | 5h: 22% +9.9h wk: 30% +143.5h
```

The quota line is the point: it keeps the useful "how much runway is left"
signal as a percentage and a projected number of hours, without ever attaching a
dollar amount to a session you might have to discard and restart. Unset (the
default) shows money as before. It composes with `STATUSLINE_COMPACT` - the
money switch is an override, so hidden figures never reappear just because the
terminal is wide.

## Why this and not [other-statusline]?

Several great Claude Code statuslines already exist; this is just mine, made
public in case it's useful. What it emphasizes:

- **Pace projection in absolute hours**, not burn-rate %, so the headroom
  signal reads as time rather than ratio.
- **Session-wide cache hit %** by walking the transcript and any subagent
  JSONLs (Claude Code's stdin payload only carries per-turn cache data).
- **Stdlib-only Python** with no `jq` / node dependency - a thin bash shim
  forwards to `statusline.py` + the `statusline_lib` package; no install step
  beyond `git clone` (optional `psutil` / `orjson` only add a feature / speed).
- **Layered layout** - location row stays uncluttered on line 1, metrics
  on line 2, live turn ETA on line 3 only when a beacon is active.

If you want progress bars, themes, or powerline glyphs,
[ccstatusline](https://github.com/sirmalloc/ccstatusline) is great. If you
want pace tracking with burn-rate % deltas,
[claude-pace](https://github.com/Astro-Han/claude-pace) is great.

## Wrap nudge

The Opus 1M-context tier bills at a **flat per-token rate** - there is no
surcharge above 200K (confirmed against current Anthropic docs and against
`~/.claude.json` billing across 28 Opus[1m] sessions). So the wrap nudge is
about **context hygiene**, not cost avoidance: long sessions accumulate
cache-read tokens on every turn (real spend, just not penalized) and model
recall degrades as the window fills. This repo ships an optional
[`UserPromptSubmit` hook](https://code.claude.com/docs/en/hooks) that injects a
**one-shot**, gentle reminder to the agent - "you're around NK of context,
consider offering `/wrap` before ~300K" - the first time a session crosses
**250K**. The message carries the live occupancy so the agent can frame it
concretely.

It does **not** run `/wrap` or interrupt anything; it only nudges the agent to
*offer* a wrap at the next natural stopping point. Wrap stays user-initiated.

**How it works.** A `UserPromptSubmit` hook's payload can't see context-window
occupancy, but the statusline's payload can. So `statusline.py` writes the live
occupancy to a per-session file under `~/.claude/state/` on each render, and the
`wrap_nudge.py` hook reads that file (no transcript walk) when you submit a
prompt. A per-session marker file makes it fire at most once. Both files are
keyed by session id, so concurrent sessions never cross signals.

`install.sh` / `install.bat` register the hook alongside the two statuslines,
preserving every other hook already in your `settings.json`. The registered
command wraps the script so it can never exit 2 (the one code that would BLOCK
prompt submission): stderr is appended to `~/.claude/wrap_nudge_hook.log` and
the exit code is forced to 0, so a broken hook degrades to a log line, never a
wedged prompt. The equivalent manual entry on macOS / Linux is:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3 ~/schoen-claude-status/wrap_nudge.py 2>>\"$HOME/.claude/wrap_nudge_hook.log\" || true" } ] }
    ]
  }
}
```

On Windows, Claude Code runs hooks via PowerShell (not cmd), so the command
uses PowerShell-compatible syntax: `py -3 "...\wrap_nudge.py"
2>>"$HOME\.claude\wrap_nudge_hook.log"; exit 0`. Prefer the installer there -
it writes the right form for the platform.

Set `CLAUDE_STATE_DIR` to relocate the state and marker files (the test suite
uses this to avoid touching real state). The threshold and message text live in
`statusline_lib/nudge.py`.

## Logs

The script truncate-writes the latest stdin payload to
`~/.claude/.statusline-input.log` and any Python errors to
`~/.claude/.statusline-error.log`. Useful for diagnosing layout issues or
seeing what fields a future Claude Code version starts sending.

## License

MIT - see [LICENSE](LICENSE).
