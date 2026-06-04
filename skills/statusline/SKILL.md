---
name: statusline
description: Use when the user wants to change the schoen-claude-status status line live - hide or show cost figures, switch compact mode, set or derive the burn-rate target, set a daily budget, toggle verbose pace, or turn the progress-beacon ETA on/off - without restarting Claude Code. Triggers include "hide the cost", "make the statusline compact", "pin the target rate", "turn off the beacon ETA", "what statusline settings are set".
---

# Statusline live control

Thin wrapper over `statusline_ctl.py`, the CLI that reads/writes
`~/.claude/.statusline-prefs.json`. The status line reads that file fresh every
render, so changes apply immediately - **no Claude Code restart**. Precedence the
status line applies: prefs file (this CLI) > settings.json `env` > built-in default.

## How to run it

The CLI lives at the schoen-claude-status repo root. Run it via Bash:

```
python ~/schoen-claude-status/statusline_ctl.py <command>
```

(If the repo is elsewhere, use that path. A `statusline-ctl` PATH shim, if the
user has installed one, also works.)

Commands: `list` · `get <key>` · `set <key> <value>` · `reset <key>` · `path`.
`reset` drops the override so the env/default shows again.

## Keys

| key | values | controls |
|---|---|---|
| `cost` | `on`\|`off` | show / hide every `$` figure on line 2 |
| `compact` | `off`\|`auto`\|`always` | line-2 width shrinking |
| `target-rate` | `<$/min>`\|`auto`\|`off` | the `→$` target: pin a number, derive it (weekly-sustainable for subscriptions), or disable |
| `daily-budget` | `<$/day>`\|`off` | API-key daily budget (drives the needle) |
| `verbose-pace` | `on`\|`off` | numeric pace deltas instead of the glyph |
| `beacon` | `on`\|`off` | render the progress-beacon ETA column (rendering only; the agent still emits beacons) |

## Mapping requests to commands

- "hide the cost" / "I don't want to see dollars" → `set cost off`
- "show cost again" → `set cost on` (or `reset cost`)
- "make it compact" / "always compact" → `set compact always`
- "stop auto-shrinking" → `set compact off`
- "pin the target to $0.50" → `set target-rate 0.50`
- "let the target auto-derive" / "use the weekly rate" → `set target-rate auto`
- "no target" → `set target-rate off`
- "turn off the beacon ETA" → `set beacon off`
- "what's set?" / "show statusline settings" → `list`
- "put X back to default" → `reset X`

After a `set`/`reset`, tell the user it is effective now (the next render picks it
up). Use `list` to read current state before changing something ambiguous.

## Notes

- `target-rate auto` vs `off`: `auto` derives (weekly-sustainable on a
  subscription, flat default on API-key); `off` removes the target entirely and
  the `$/min` number goes neutral grey. They are not the same.
- `cost off` keeps non-dollar signals (tokens, hit%, quota %, diffstat) - it only
  drops `$`-denominated figures.
- Values are validated; a bad value exits nonzero and changes nothing. Surface the
  error message to the user rather than retrying blindly.
