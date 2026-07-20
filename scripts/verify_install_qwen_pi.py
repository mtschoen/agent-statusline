"""Verify the Qwen Code and Pi extension install helpers extracted from
install.py into statusline_lib (statusline_lib.qwen_install's ui.statusLine
merge, statusline_lib.platform_commands's Qwen command routing and Pi loader
path/contents) -- same dict-in/dict-out, no-I/O shape as the other install
helper modules, so the verify suite can exercise them directly.

Run from anywhere.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.platform_commands import (
    _pi_loader_contents,
    _pi_loader_path,
    _qwen_command_for_platform,
)
from statusline_lib.qwen_install import _merge_qwen_statusline, _qwen_settings_current


def _check_qwen_command_windows(failures):
    real_name = os.name
    try:
        os.name = "nt"
        target, command = _qwen_command_for_platform("/repo")
    finally:
        os.name = real_name
    if target != "/repo/qwen_statusline.py":
        failures.append(f"qwen nt target unexpected: {target!r}")
    if command != 'py -3 "/repo/qwen_statusline.py"':
        failures.append(f"qwen nt command unexpected: {command!r}")


def _check_qwen_command_posix(failures):
    real_name = os.name
    try:
        os.name = "posix"
        target, command = _qwen_command_for_platform("/repo")
    finally:
        os.name = real_name
    if target != "/repo/qwen_statusline.py":
        failures.append(f"qwen posix target unexpected: {target!r}")
    if command != 'bash "/repo/qwen-statusline-command.sh"':
        failures.append(f"qwen posix command unexpected: {command!r}")


def _check_qwen_settings_current(failures):
    command = 'bash "/repo/qwen-statusline-command.sh"'
    if _qwen_settings_current({}, command):
        failures.append("empty settings should not be reported current")
    if _qwen_settings_current({"ui": {"statusLine": {"type": "command"}}}, command):
        failures.append("statusLine missing a command should not be current")
    wrong_type = {"ui": {"statusLine": {"type": "other", "command": command}}}
    if _qwen_settings_current(wrong_type, command):
        failures.append("a non-command statusLine type should not be current")
    wrong_command = {
        "ui": {"statusLine": {"type": "command", "command": "something else"}}
    }
    if _qwen_settings_current(wrong_command, command):
        failures.append("a mismatched command should not be current")
    matching = {"ui": {"statusLine": {"type": "command", "command": command}}}
    if not _qwen_settings_current(matching, command):
        failures.append("an exact match should be reported current")


def _check_merge_qwen_statusline_fresh_insert(failures):
    command = 'bash "/repo/qwen-statusline-command.sh"'
    settings = {}
    _merge_qwen_statusline(settings, command)
    ui = settings.get("ui") or {}
    if ui.get("statusLine") != {"type": "command", "command": command}:
        failures.append(f"fresh insert should install ui.statusLine, got {settings!r}")


def _check_merge_qwen_statusline_preserves_other_ui_keys(failures):
    command = 'bash "/repo/qwen-statusline-command.sh"'
    settings = {"ui": {"theme": "dark"}}
    _merge_qwen_statusline(settings, command)
    if settings["ui"].get("theme") != "dark":
        failures.append(f"merge should preserve unrelated ui keys, got {settings!r}")
    if settings["ui"].get("statusLine") != {"type": "command", "command": command}:
        failures.append(f"merge should install ui.statusLine, got {settings!r}")


def _check_merge_qwen_statusline_updates_existing(failures):
    settings = {"ui": {"statusLine": {"type": "command", "command": "stale command"}}}
    new_command = 'bash "/repo/qwen-statusline-command.sh"'
    _merge_qwen_statusline(settings, new_command)
    if settings["ui"]["statusLine"]["command"] != new_command:
        failures.append(f"merge should overwrite a stale command, got {settings!r}")


def _check_pi_loader_path(failures):
    expected = os.path.expanduser("~/.pi/agent/extensions/agent-statusline/index.ts")
    if _pi_loader_path() != expected:
        failures.append(f"pi loader path unexpected: {_pi_loader_path()!r}")


def _check_pi_loader_contents(failures):
    contents = _pi_loader_contents("/repo")
    if contents != 'export { default } from "/repo/pi-extension/index.ts";\n':
        failures.append(f"pi loader contents unexpected: {contents!r}")


def check(failures):
    _check_qwen_command_windows(failures)
    _check_qwen_command_posix(failures)
    _check_qwen_settings_current(failures)
    _check_merge_qwen_statusline_fresh_insert(failures)
    _check_merge_qwen_statusline_preserves_other_ui_keys(failures)
    _check_merge_qwen_statusline_updates_existing(failures)
    _check_pi_loader_path(failures)
    _check_pi_loader_contents(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: Qwen command routing/settings merge and the Pi extension loader "
        "path/contents all behave"
    )


if __name__ == "__main__":
    main()
