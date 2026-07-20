"""Per-platform command/path construction for install.py: which interpreter
and wrapper string each CLI's statusLine/subagentStatusLine entry should run,
and where the Pi extension loader lives.

Pure functions -- no file I/O, no printing -- same shape as nudge_install.py
and codex_install.py, so the verify suite can exercise the platform routing
directly and install.py stays the only place that touches disk or reports
progress.
"""

import os

# statusLine only repaints on lead-session events (new prompt, tool call).
# While the lead is idle waiting on a background Agent Teams teammate, nothing
# retriggers it, so the teammates: line (and any other time-based segment)
# freezes mid-run and readers never see it move. Docs: "set refreshInterval to
# also re-run the command on a fixed timer" -- exactly this idle-wait case.
STATUSLINE_REFRESH_SECONDS = 3


def _commands_for_platform(repo, platform="claude"):
    """Return (main_target, subagent_target, main_command, subagent_command)."""
    # On Windows, bare python/python3 resolve to the Microsoft Store alias shim,
    # whose ~750ms per-invocation launch overhead dominated every render. Invoke
    # the python.org build via the `py` launcher directly, skipping BOTH the
    # Store shim AND the bash wrapper -- ~50-90ms faster and far less jittery
    # than `bash statusline-command.sh` (Claude Code wraps the command in
    # `cmd /c` on Windows, so no shell prefix is needed). `py -3` keeps it
    # robust across Python minor upgrades -- no hard-coded interpreter path.
    # On other platforms bash + python3 are already fast, so keep the portable
    # shim (which itself prefers `py` where present -- see statusline-command.sh).
    if os.name == "nt":
        main_target = f"{repo}/statusline.py"
        subagent_target = f"{repo}/subagent_statusline.py"
        if platform == "antigravity":
            # Antigravity CLI doesn't set ANTIGRAVITY_AGENT / ANTIGRAVITY_
            # CONVERSATION_ID for the statusline subprocess, so app_dir()'s
            # env-based auto-detect never fires and everything (state, error
            # log, payload log) silently lands in ~/.claude instead of
            # ~/.gemini/antigravity-cli. Make routing deterministic by
            # putting the platform in the command string itself.
            return (
                main_target,
                subagent_target,
                f"py -3 {main_target} --statusline-platform antigravity",
                f"py -3 {subagent_target} --statusline-platform antigravity",
            )
        return (
            main_target,
            subagent_target,
            f'py -3 "{main_target}"',
            f'py -3 "{subagent_target}"',
        )
    main_target = f"{repo}/statusline-command.sh"
    subagent_target = f"{repo}/subagent-statusline.sh"
    if platform == "antigravity":
        return (
            main_target,
            subagent_target,
            f'bash "{main_target}" --statusline-platform antigravity',
            f'bash "{subagent_target}" --statusline-platform antigravity',
        )
    return (
        main_target,
        subagent_target,
        f'bash "{main_target}"',
        f'bash "{subagent_target}"',
    )


def _qwen_command_for_platform(repo):
    """Return (target, command) for Qwen Code statusline."""
    # Qwen Code uses the same platform-aware invocation strategy as Claude Code.
    target = f"{repo}/qwen_statusline.py"
    if os.name == "nt":
        command = f'py -3 "{target}"'
    else:
        command = f'bash "{repo}/qwen-statusline-command.sh"'
    return target, command


def _pi_loader_path():
    return os.path.expanduser("~/.pi/agent/extensions/agent-statusline/index.ts")


def _pi_loader_contents(repo):
    return f'export {{ default }} from "{repo}/pi-extension/index.ts";\n'
