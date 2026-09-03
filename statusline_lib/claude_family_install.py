"""Shared merge logic for the Claude Code and Antigravity CLI platforms:
both configure statusLine + subagentStatusLine + the wrap-nudge hook +
the SessionStart ensure-server hook through the identical settings.json
shape, differing only in the settings path and the commands themselves
(which already carry any platform-specific routing, e.g.
--statusline-platform). Qwen (a single ui.statusLine key) and Codex (native
TOML preset) have different settings shapes and keep their own install.py
functions.

The server-hook assembly and its printable description live here rather
than in install.py so install.py's line count doesn't grow -- see
build_server_hook and describe_statusline_family below.

Pure dict-in/dict-out helpers -- no file I/O, no printing -- same shape as
nudge_install.py, server_install.py, and codex_install.py, so the verify
suite can exercise the merge against in-memory settings and install.py stays
the sole place that does file I/O.
"""

import os

from statusline_lib.nudge_install import _merge_nudge_hook, _nudge_hook_current
from statusline_lib.platform_commands import STATUSLINE_REFRESH_SECONDS
from statusline_lib.server_install import (
    _merge_server_hook,
    _server_hook_command,
    _server_hook_current,
    _server_hook_markers,
)


def missing_required_scripts(*paths):
    """Return the subset of `paths` that don't exist on disk, preserving
    order; empty if every path is present."""
    return tuple(path for path in paths if not os.path.exists(path))


# The client/server entry points every resident-server install depends on,
# alongside the per-platform statusLine/subagentStatusLine commands. Named
# here (rather than inline in install.py) so install.py's line count stays
# put -- see resident_server_script_targets below.
_RESIDENT_SERVER_SCRIPTS = (
    "statusline_client.py",
    "statusline_server.py",
    "statusline_client_support.py",
)


def resident_server_script_targets(repo):
    """The resident-server entry points under `repo`, for
    missing_required_scripts to check alongside the per-platform targets."""
    return tuple(os.path.join(repo, name) for name in _RESIDENT_SERVER_SCRIPTS)


def desired_statusline_entries(main_command, subagent_command, refresh_seconds):
    """Return (desired_statusline, desired_subagent) dicts for settings.json."""
    return (
        {
            "type": "command",
            "command": main_command,
            "refreshInterval": refresh_seconds,
        },
        {"type": "command", "command": subagent_command},
    )


def build_server_hook(repo, platform="claude"):
    """(command, markers) for the SessionStart ensure-server hook, assembled
    together so install.py carries one call instead of two."""
    target, command = _server_hook_command(repo, platform=platform)
    return command, _server_hook_markers(target)


def describe_statusline_family(
    main_command, subagent_command, nudge_command, server_command
):
    """Human-readable lines describing the four managed settings entries,
    shared by install.py's already-current, dry-run, and updated reports."""
    return (
        f"  statusLine:         {main_command}  (refresh {STATUSLINE_REFRESH_SECONDS}s)",
        f"  subagentStatusLine: {subagent_command}",
        f"  UserPromptSubmit:   {nudge_command}",
        f"  SessionStart:       {server_command}",
    )


def statusline_family_already_current(
    settings,
    desired_statusline,
    desired_subagent,
    nudge_markers,
    nudge_command,
    server_markers=None,
    server_command=None,
):
    """True iff `settings` already has the desired statusLine,
    subagentStatusLine, nudge hook, and (when given) server hook entries.
    `server_markers`/`server_command` default to None for callers that only
    manage the first three entries."""
    current = (
        settings.get("statusLine") == desired_statusline
        and settings.get("subagentStatusLine") == desired_subagent
        and _nudge_hook_current(settings, nudge_markers, nudge_command)
    )
    if server_markers is not None:
        current = current and _server_hook_current(
            settings, server_markers, server_command
        )
    return current


def merge_statusline_family_settings(
    settings,
    desired_statusline,
    desired_subagent,
    nudge_markers,
    nudge_command,
    server_markers=None,
    server_command=None,
):
    """Mutate `settings` in place: install statusLine + subagentStatusLine +
    the nudge hook + (when given) the server hook, preserving every other
    key."""
    settings["statusLine"] = desired_statusline
    settings["subagentStatusLine"] = desired_subagent
    _merge_nudge_hook(settings, nudge_markers, nudge_command)
    if server_markers is not None:
        _merge_server_hook(settings, server_markers, server_command)
