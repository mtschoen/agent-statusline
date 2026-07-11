"""Shared merge logic for the Claude Code and Antigravity CLI platforms:
both configure statusLine + subagentStatusLine + the wrap-nudge hook through
the identical settings.json shape, differing only in the settings path and
the commands themselves (which already carry any platform-specific routing,
e.g. --statusline-platform). Qwen (a single ui.statusLine key) and Codex
(native TOML preset) have different settings shapes and keep their own
install.py functions.

Pure dict-in/dict-out helpers -- no file I/O, no printing -- same shape as
nudge_install.py and codex_install.py, so the verify suite can exercise the
merge against in-memory settings and install.py stays the sole place that
does I/O and reports progress to the user.
"""

import os

from statusline_lib.nudge_install import _merge_nudge_hook, _nudge_hook_current


def missing_required_scripts(*paths):
    """Return the subset of `paths` that don't exist on disk, preserving
    order; empty if every path is present."""
    return tuple(path for path in paths if not os.path.exists(path))


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


def statusline_family_already_current(
    settings, desired_statusline, desired_subagent, nudge_markers, nudge_command
):
    """True iff `settings` already has the desired statusLine,
    subagentStatusLine, and nudge hook entries."""
    return (
        settings.get("statusLine") == desired_statusline
        and settings.get("subagentStatusLine") == desired_subagent
        and _nudge_hook_current(settings, nudge_markers, nudge_command)
    )


def merge_statusline_family_settings(
    settings, desired_statusline, desired_subagent, nudge_markers, nudge_command
):
    """Mutate `settings` in place: install statusLine + subagentStatusLine +
    the nudge hook, preserving every other key."""
    settings["statusLine"] = desired_statusline
    settings["subagentStatusLine"] = desired_subagent
    _merge_nudge_hook(settings, nudge_markers, nudge_command)
