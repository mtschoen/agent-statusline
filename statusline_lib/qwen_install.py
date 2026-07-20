"""Merge logic for Qwen Code's settings.json: a single ui.statusLine key,
unlike the statusLine + subagentStatusLine + nudge-hook trio the Claude-family
platforms configure (see claude_family_install.py).

Pure dict-in/dict-out helpers -- no file I/O, no printing -- same shape as
nudge_install.py, claude_family_install.py, and codex_install.py, so the
verify suite can exercise the merge against in-memory settings and install.py
stays the sole place that does I/O and reports progress to the user.
"""


def _qwen_settings_current(settings, command):
    """True iff Qwen ui.statusLine already matches `command`."""
    ui = settings.get("ui") or {}
    status_line = ui.get("statusLine") or {}
    return (
        status_line.get("type") == "command" and status_line.get("command") == command
    )


def _merge_qwen_statusline(settings, command):
    """Insert or update ui.statusLine, preserving other ui keys."""
    ui = settings.setdefault("ui", {})
    ui["statusLine"] = {"type": "command", "command": command}
