# schoen-claude-status

## Quality gate: aislop

This project uses **aislop** as a deterministic quality gate for AI-written code
(narrative comments, swallowed exceptions, `as any`, dead stubs, oversized
functions, etc.) across TS/JS, Python, Go, Rust, Ruby, PHP, Java, and C#.

`aislop` is installed globally on this machine (pinned to the fork
`mtschoen/aislop`, which adds C#/roslynator support). Call the installed binary
directly - do NOT use `npx aislop`, which pulls upstream from npm with no C#
support:

- **Before declaring work complete**, run `aislop scan .` and address findings.
- **Before committing**, run `aislop scan --staged` (staged files only).
- `aislop fix` auto-clears mechanical issues (formatting, unused imports, dead
  code); `aislop fix --claude` hands the rest back with full context.
- `aislop ci .` is the gate - exits non-zero if the score drops below the
  threshold in `.aislop/config.yml`. Treat a failing gate like a failing test.

To refresh the pinned binary after new commits land on the fork branch:
`pnpm add -g --allow-build=aislop "github:mtschoen/aislop#feat/csharp-support"`

## Debugging the compact-mode width gate

`statusline_lib/compact.py` auto-sheds line-2 fields only when the rendered width
exceeds `$COLUMNS`. When auto-shrink looks broken, check the width source before
the logic:

- A `Bash`/shell subprocess does NOT inherit `COLUMNS`, so an `echo $COLUMNS`
  from a tool call reads empty - that is NOT the value the statusline sees.
- The live statusline subprocess DOES get it: Claude Code (>= 2.1.153, confirmed
  on 2.1.160) sets `COLUMNS` to the terminal width before invoking the command.
  Ground truth is logged per-render to `~/.claude/.statusline-cols-debug.log`
  (and the raw stdin payload to `~/.claude/.statusline-input.log`).
- So "shrinking never happens" usually just means the terminal is wider than
  line 2 (e.g. 316 cols) - drag the window narrow, or force it with
  `STATUSLINE_COMPACT=always`, to see fields drop in `DROP_ORDER`.

## Live prefs override env (debugging "my setting isn't taking effect")

Every `STATUSLINE_*` setting resolves as: `~/.claude/.statusline-prefs.json`
(written by `statusline_ctl.py`) > `settings.json` `env` block > built-in
default. The prefs file is read fresh on every render; the env block is only
inherited at Claude Code launch. Two consequences when debugging:

- An env edit in `settings.json` does nothing until restart AND can still be
  silently shadowed by a forgotten prefs override. Check
  `python statusline_ctl.py list` first - it shows every key's effective
  source.
- To change behavior live (no restart), go through `statusline_ctl.py set` /
  `reset`, not the env block.
