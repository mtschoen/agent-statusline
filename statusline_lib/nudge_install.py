"""Install-time wiring for the wrap nudge hook: how the settings.json merge
recognizes, inserts, updates, and dedupes our UserPromptSubmit entry.

Separate from ``nudge.py`` (the runtime state-file contract between the
statusline and the hook): nothing here runs per prompt -- it runs once, when
install.py rewrites Claude Code settings.

Pure dict-in/dict-out helpers -- no file I/O -- so the verify suite can
exercise the merge against in-memory settings.
"""

import os

# Stable identity stamp for our hook entry, appended to the command string as
# a shell comment (`#` starts a comment in both POSIX sh and PowerShell, the
# two shells Claude Code runs hook commands through, so it never affects
# execution). The installer recognizes its own entry by this sentinel rather
# than by the script's filename, so the script can be renamed or moved freely
# without orphaning entries written by earlier installs (which is what
# happened with the nudge_200k.py -> wrap_nudge.py rename). Treat the sentinel
# text itself as frozen: changing it recreates the very problem it solves.
_NUDGE_SENTINEL = "#managed-by:schoen-claude-status/wrap-nudge"


def _nudge_command(repo, platform="claude"):
    """Shell-aware command for the UserPromptSubmit wrap nudge hook.

    Wrapped so the hook can never exit 2 -- the one code that BLOCKS prompt
    submission. A missing target or interpreter error has its stderr appended to
    the log file and the command forces a 0 exit, so a broken
    hook degrades to a log line, never a wedged prompt (and never an error
    injected into context).

    Runs hook commands through a shell: PowerShell on Windows (NOT
    cmd.exe -- so %VAR% / ver>nul / || are wrong there; verified empirically by
    the stray-`nul`-file artifact), POSIX sh elsewhere. `$HOME` resolves in both
    PowerShell and sh. `; exit 0` forces non-blocking under PowerShell (works in
    pwsh 7 and Windows PowerShell 5.1, neither of which needs `||`)."""
    target = f"{repo}/wrap_nudge.py"
    app_subdir = ".gemini\\antigravity-cli" if platform == "antigravity" else ".claude"
    app_subdir_unix = (
        ".gemini/antigravity-cli" if platform == "antigravity" else ".claude"
    )
    if os.name == "nt":
        command = f'py -3 "{target}" 2>>"$HOME\\{app_subdir}\\wrap_nudge_hook.log"; exit 0 {_NUDGE_SENTINEL}'
    else:
        command = f'python3 "{target}" 2>>"$HOME/{app_subdir_unix}/wrap_nudge_hook.log" || true {_NUDGE_SENTINEL}'
    return target, command


def _nudge_markers(target):
    """Substrings that identify our hook entry among any other UserPromptSubmit
    hooks the user has configured. The sentinel is the durable identity; the
    current script basename is matched as well so an entry written by an
    install that predates the sentinel is migrated in place instead of
    duplicated. The basename is derived fresh from `target` on every run --
    deliberately NOT a maintained list of historical filenames."""
    return (_NUDGE_SENTINEL, os.path.basename(target))


def _find_nudge_hooks(settings, markers):
    """Return every (group, hook) pair recognized as our nudge entry, in
    registration order."""
    found = []
    for group in (settings.get("hooks") or {}).get("UserPromptSubmit") or []:
        for hook in group.get("hooks") or []:
            hook_command = hook.get("command") or ""
            if any(marker in hook_command for marker in markers):
                found.append((group, hook))
    return found


def _nudge_hook_current(settings, markers, command):
    """True iff exactly one nudge hook is present (no stale leftovers from an
    older install) and it already has exactly `command`."""
    matches = _find_nudge_hooks(settings, markers)
    return len(matches) == 1 and matches[0][1].get("command") == command


def _merge_nudge_hook(settings, markers, command):
    """Insert or update the nudge hook, preserving every other hook entry.
    Updates the first match in place, removes any further matches (stale
    entries from an older install, or accidental duplicates), and drops
    matcher groups that removal left empty."""
    matches = _find_nudge_hooks(settings, markers)
    if not matches:
        groups = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
        groups.append({"hooks": [{"type": "command", "command": command}]})
        return
    matches[0][1]["type"] = "command"
    matches[0][1]["command"] = command
    for group, hook in matches[1:]:
        group["hooks"].remove(hook)
    emptied_ids = {id(group) for group, _ in matches[1:] if not group.get("hooks")}
    if emptied_ids:
        groups = settings["hooks"]["UserPromptSubmit"]
        groups[:] = [group for group in groups if id(group) not in emptied_ids]
