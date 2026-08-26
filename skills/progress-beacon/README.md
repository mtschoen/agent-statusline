# skills-progress-beacon

Agent-emitted progress beacons during non-trivial agent turns, plus
a calibrated "when can I close my laptop?" status-line render. Today's
hook wiring and transcript reader are Claude Code-specific (see below);
the beacon format itself is agent-agnostic.

## What it is

The skill (`SKILL.md`) tells the agent to periodically emit a small
machine-readable JSON block in its assistant message text:

```text
<progress-beacon>
{"kind": "begin", "eta_seconds": 720, "summary": "auth refactor"}
</progress-beacon>
```

[agent-walker](https://github.com/mtschoen/agent-walker/) (separate
repo) parses these from the active session transcript on demand; the
binary installs as `agent-walker`. The status line
([agent-statusline](https://github.com/mtschoen/agent-statusline),
separate repo) renders the live
beacon plus a calibrated ETA derived from a 7-day median of
`actual_elapsed / begin_eta` ratios.

Two hooks keep the agent honest (see "Nudge paths and rate limiting"
below): a UserPromptSubmit reminder of the trigger criteria at the
start of each prompt, and a PostToolUse recency nudge when the
transcript shows no recent beacon.

For the user-facing render - what each field on line 3 of the status
line means, and how to read the wall-clock anchors, drift colors, and
error states - see the **Line 3 (beacon)** section of the
[agent-statusline README](https://github.com/mtschoen/agent-statusline#what-you-see).

## Installation (3 components)

1. **The skill itself** - lives in this repository under `skills/progress-beacon/`.
   Copy or symlink it to `~/.agents/skills/progress-beacon/` (or wherever your
   agent harness reads skills from):

   ```bash
   mkdir -p ~/.agents/skills
   cp -r skills/progress-beacon ~/.agents/skills/
   ```

2. **[agent-walker](https://github.com/mtschoen/agent-walker/)** -
   install the production C++ binary:

   ```bash
   cd ~/agent-walker && bash install.sh   # or install.bat on Windows
   ```

   Puts `agent-walker(.exe)` at `~/.local/bin/`. Add that dir to PATH
   if it isn't there. Both hooks below also require `jq` on PATH -
   install it via your platform's package manager (e.g. `brew install jq`,
   `apt install jq`) if it isn't already present. There is no fallback
   if agent-walker isn't installed beyond the degraded hook behaviors
   described below (recency-nudge goes silent, prompt-reminder fires
   unconditionally): there is no other detection path.

3. **[agent-statusline](https://github.com/mtschoen/agent-statusline)
   patches** - already merged on `main`; the helpers `format_beacon`
   and `format_calibrated_eta` activate automatically once the
   `agent-walker` binary is on PATH.

## Hook configuration

Register the hooks in the agent's settings so the prompt reminder fires on
each user prompt and the recency nudge after tool calls:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.agents/skills/progress-beacon/hooks/prompt-reminder.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.agents/skills/progress-beacon/hooks/recency-nudge.sh"
          }
        ]
      }
    ]
  }
}
```

If the `hooks` block already exists, merge carefully - preserve the
other entries. (The `update-config` skill, if available, can perform
this merge for you rather than hand-editing.)

Neither hook is required for the skill's core beacon behavior - that
comes from `SKILL.md` alone. The hooks are an optional backstop reminder
layer; skip this section if you don't want the nudges.

## Nudge paths and rate limiting

`hooks/prompt-reminder.sh` (UserPromptSubmit) injects a one-line
reminder of the trigger criteria on each user prompt, unless the
session's latest visible beacon is a live `begin`/`report` (mid-turn
prompt while the agent is already pacing).

`hooks/recency-nudge.sh` (PostToolUse) has two paths:

- **Stale path:** the latest visible beacon is a live (non-`end`)
  beacon older than 10 minutes. An `end` beacon silences it - the
  lifecycle is closed.
- **Missing path:** no beacon is visible at all and the session is
  older than a 5-minute grace period.

Both paths share a per-session cooldown (5 minutes, stamp file under
`$TMPDIR/progress-beacon-nudge/`), so one stale window produces one
nudge, not a burst on every tool call of a parallel batch.

Both hooks get the latest beacon by shelling out to
`agent-walker beacons-latest --session-id <id>`, which returns
`{"beacon": {"kind": "begin"|"report"|"end", ...}, "age_seconds": N}`
(or nothing, if no beacon is visible yet). That call is wrapped in
`|| true`, so a missing, unreachable, or erroring agent-walker never
crashes either hook. The two hooks degrade differently, though, and
only one of them is a plain no-op:

- `hooks/recency-nudge.sh` treats an empty result as "nothing to
  check" and returns `{}`, exit 0, right away. This one really is a
  silent no-op: the only symptom is a nudge that never fires.
- `hooks/prompt-reminder.sh` uses that same result to decide whether a
  live beacon already exists. An empty result looks identical to "no
  beacon yet," so the hook can't tell the two apart and falls through
  to its default behavior: emitting the reminder unconditionally, on
  every prompt, even mid-turn while a beacon is already active. That's
  fail-open in the noisy direction, not a silent no-op.

There is no other fallback for either hook beyond these two behaviors.

The `jq` calls that parse agent-walker's output are not wrapped the
same way. Both scripts run under `set -euo pipefail`
(`hooks/prompt-reminder.sh` lines 14, 23, 35; `hooks/recency-nudge.sh`
lines 29, 159, 160, 167, 189, 205), so a missing `jq` is a hard hook error
in either hook: the script exits non-zero instead of emitting `{}`,
and Claude Code surfaces that as a failed hook rather than a quiet
no-op. Install `jq` before relying on either hook.

Two facts of life shape those thresholds (learned from the 2026-06
false-nudge incidents):

- Claude Code persists mid-turn assistant messages to the session
  JSONL with multi-minute lag - sometimes never, for text-only
  mid-turn messages. A beacon the agent just emitted can be invisible
  to agent-walker for minutes, so sub-5-minute thresholds nag about
  beacons that were in fact emitted.
- `end` beacons must be visible for the stale path to stand down.
  agent-walker (>= the 2026-06-10 build) treats `eta_seconds` as
  optional on `kind: "end"` (defaulting to 0), because agents
  routinely omit it there; older walker builds silently dropped such
  end beacons, leaving lifecycles permanently open and the stale
  nudge firing forever with a growing minutes counter.

## Beacon format

Required fields: `kind` (`"begin"` | `"report"` | `"end"`),
`eta_seconds` (number), `summary` (string ≤80 chars).

Optional: `beats_left`. All other fields are reserved.

## Layout

- `SKILL.md` - the skill body the agent reads.
- `hooks/prompt-reminder.sh` - UserPromptSubmit trigger-criteria reminder.
- `hooks/recency-nudge.sh` - PostToolUse staleness backstop (rate-limited).
- `evals/` - v1 scaffolding; live grader is a v2 follow-up.
- `workspace/` - gitignored; per-iteration scratch.

## Status

v1 ships in 2026-05. Real-session shake-down is tracked in the author's
cross-project memory, outside this repo.
