"""Install-time wiring for the SessionStart ensure-server hook: how the
settings.json merge recognizes, inserts, updates, and dedupes our
SessionStart entry.

The hook asks the client whether a resident server is alive and, if not,
spawns one in the background. It reads no stdin and prints nothing: every
render is served by that resident process, so the hook's whole job is having
one running before the session's first render arrives.

Mirrors statusline_lib/nudge_install.py name for name: same sentinel-based
identity, same basename migration for pre-sentinel entries, same
never-exit-non-zero wrapping. The only real differences are the sentinel
text, the target script, and the hook event (SessionStart here,
UserPromptSubmit there).

Pure dict-in/dict-out helpers -- no file I/O -- so the verify suite can
exercise the merge against in-memory settings.
"""

import os

# Stable identity stamp for our hook entry, appended to the command string as
# a shell comment (`#` starts a comment in both POSIX sh and PowerShell, the
# two shells Claude Code runs hook commands through, so it never affects
# execution). See _NUDGE_SENTINEL in nudge_install.py for the full rationale
# on why identity is keyed off this constant rather than the script name.
_SERVER_SENTINEL = "#managed-by:agent-statusline/ensure-server"


def _server_hook_command(repo, platform="claude"):
    """Shell-aware command for the SessionStart ensure-server hook.

    Runs the client's liveness-and-version check only: no stdin is read and
    nothing is printed, so the hook costs one interpreter start and either
    finds a live server or spawns one in the background.
    """
    target = f"{repo}/statusline_client.py"
    app_subdirectory = (
        ".gemini/antigravity-cli" if platform == "antigravity" else ".claude"
    )
    if os.name == "nt":
        command = (
            f'py -3 "{target}" --ensure-server'
            f' 2>>"$HOME\\{app_subdirectory.replace("/", chr(92))}\\server_hook.log";'
            f" exit 0 {_SERVER_SENTINEL}"
        )
    else:
        command = (
            f'python3 "{target}" --ensure-server'
            f' 2>>"$HOME/{app_subdirectory}/server_hook.log" || true {_SERVER_SENTINEL}'
        )
    return target, command


def _server_hook_markers(target):
    """Substrings that identify our hook entry among any other SessionStart
    hooks the user has configured. Same basename-plus-sentinel strategy as
    _nudge_markers in nudge_install.py."""
    return (_SERVER_SENTINEL, os.path.basename(target))


def _find_server_hooks(settings, markers):
    """Return every (group, hook) pair recognized as our server entry, in
    registration order."""
    found = []
    for group in (settings.get("hooks") or {}).get("SessionStart") or []:
        for hook in group.get("hooks") or []:
            hook_command = hook.get("command") or ""
            if any(marker in hook_command for marker in markers):
                found.append((group, hook))
    return found


def _server_hook_current(settings, markers, command):
    """True iff exactly one server hook is present (no stale leftovers from
    an older install) and it already has exactly `command`."""
    matches = _find_server_hooks(settings, markers)
    return len(matches) == 1 and matches[0][1].get("command") == command


def _merge_server_hook(settings, markers, command):
    """Insert or update the server hook, preserving every other hook entry.
    Updates the first match in place, removes any further matches (stale
    entries from an older install, or accidental duplicates), and drops
    matcher groups that removal left empty."""
    matches = _find_server_hooks(settings, markers)
    if not matches:
        groups = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
        groups.append({"hooks": [{"type": "command", "command": command}]})
        return
    matches[0][1]["type"] = "command"
    matches[0][1]["command"] = command
    for group, hook in matches[1:]:
        group["hooks"].remove(hook)
    emptied_ids = {id(group) for group, _ in matches[1:] if not group.get("hooks")}
    if emptied_ids:
        groups = settings["hooks"]["SessionStart"]
        groups[:] = [group for group in groups if id(group) not in emptied_ids]
