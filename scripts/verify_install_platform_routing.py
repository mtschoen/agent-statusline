"""Verify install.py's `_commands_for_platform` deterministically routes the
Antigravity CLI statusline to ~/.gemini/antigravity-cli.

Root cause of the "Antigravity statusline throws tons of errors" report: the
configured command relied on Antigravity CLI setting ANTIGRAVITY_AGENT /
ANTIGRAVITY_CONVERSATION_ID for the statusline subprocess so
statusline_lib.base.app_dir() could auto-detect the platform. Antigravity CLI
does not set those (confirmed empirically: ~/.gemini/antigravity-cli's
.statusline-input.log stayed empty while the CLI was in active use, and every
render's state/logs landed in ~/.claude instead). The fix makes the
`--statusline-platform antigravity` flag part of the installed command
string itself, so routing no longer depends on the host CLI's env-var
behavior. See scripts/verify_prefs.py's `_check_app_dir_argv_override` for
the app_dir() half of this contract.

Both the Windows (`py -3 ...`) and POSIX (`bash ...`) command forms are
covered -- CI runs this suite on both hosts.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from install import _commands_for_platform


def _check_antigravity_windows_command_has_platform_flag(failures):
    real_name = os.name
    try:
        os.name = "nt"
        _main_t, _sub_t, main_cmd, sub_cmd = _commands_for_platform(
            "/repo", platform="antigravity"
        )
    finally:
        os.name = real_name

    if "--statusline-platform antigravity" not in main_cmd:
        failures.append(
            f"nt antigravity main command missing platform flag: {main_cmd!r}"
        )
    if "--statusline-platform antigravity" not in sub_cmd:
        failures.append(
            f"nt antigravity subagent command missing platform flag: {sub_cmd!r}"
        )


def _check_antigravity_posix_command_has_platform_flag(failures):
    real_name = os.name
    try:
        os.name = "posix"
        _main_t, _sub_t, main_cmd, sub_cmd = _commands_for_platform(
            "/repo", platform="antigravity"
        )
    finally:
        os.name = real_name

    if "--statusline-platform antigravity" not in main_cmd:
        failures.append(
            f"posix antigravity main command missing platform flag: {main_cmd!r}"
        )
    if "--statusline-platform antigravity" not in sub_cmd:
        failures.append(
            f"posix antigravity subagent command missing platform flag: {sub_cmd!r}"
        )


def _check_claude_command_unaffected(failures):
    # The flag is antigravity-specific -- claude (default platform) keeps its
    # existing quoted-path command with no platform flag appended.
    real_name = os.name
    try:
        os.name = "nt"
        _main_t, _sub_t, main_cmd, sub_cmd = _commands_for_platform(
            "/repo", platform="claude"
        )
    finally:
        os.name = real_name

    if "--statusline-platform" in main_cmd or "--statusline-platform" in sub_cmd:
        failures.append(
            f"claude platform commands should not carry --statusline-platform: "
            f"{main_cmd!r} / {sub_cmd!r}"
        )


def check(failures):
    _check_antigravity_windows_command_has_platform_flag(failures)
    _check_antigravity_posix_command_has_platform_flag(failures)
    _check_claude_command_unaffected(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: install.py routes Antigravity CLI deterministically via --statusline-platform"
    )


if __name__ == "__main__":
    main()
