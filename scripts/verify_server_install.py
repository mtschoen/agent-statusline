"""Verify the SessionStart server-hook settings merge
(statusline_lib.server_install, wired in by install.py): sentinel-based
identity that survives a script rename, basename migration of pre-sentinel
entries, stale-duplicate removal, empty-group pruning, in-place update,
fresh insert, and the already-current check rejecting settings with
leftovers.

Mirrors scripts/verify_install_nudge_merge.py exactly, against the
SessionStart hook instead of the UserPromptSubmit one.

Pure in-memory checks against the merge helpers -- no settings.json is read or
written. Run from anywhere.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.server_install import (
    _SERVER_SENTINEL,
    _merge_server_hook,
    _server_hook_command,
    _server_hook_current,
    _server_hook_markers,
)

TARGET, COMMAND = _server_hook_command("/repo")
MARKERS = _server_hook_markers(TARGET)
# An entry written by an older install, before a hypothetical script rename:
# different filename and body, same sentinel. Recognizing this WITHOUT any
# list of historical filenames is the property the sentinel exists for.
RENAMED_COMMAND = (
    f'python3 "/repo/some_long_forgotten_name.py" || true {_SERVER_SENTINEL}'
)
# An entry written by an install from before the sentinel existed: current
# filename, no sentinel. Matched via the basename marker and migrated.
PRE_SENTINEL_COMMAND = 'python3 "/repo/statusline_client.py" --ensure-server || true'
OTHER_HOOK = {"type": "command", "command": "echo unrelated"}


def _settings_with(*commands):
    """Settings dict with one single-hook matcher group per command."""
    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": command}]}
                for command in commands
            ]
        }
    }


def _server_commands(settings):
    """Every SessionStart command recognized as ours, in order."""
    found = []
    hook_groups = settings.get("hooks", {})
    for group in hook_groups.get("SessionStart", []):
        for hook in group.get("hooks", []):
            command = hook.get("command") or ""
            if any(marker in command for marker in MARKERS):
                found.append(command)
    return found


def _check_command_carries_sentinel(failures):
    if _SERVER_SENTINEL not in COMMAND:
        failures.append(f"command should embed the sentinel, got {COMMAND!r}")


def _check_command_targets_ensure_server(failures):
    if "--ensure-server" not in COMMAND or "statusline_client.py" not in COMMAND:
        failures.append(
            f"command should invoke statusline_client.py --ensure-server, got {COMMAND!r}"
        )


def _check_fresh_insert(failures):
    settings = {}
    _merge_server_hook(settings, MARKERS, COMMAND)
    if _server_commands(settings) != [COMMAND]:
        failures.append(f"fresh insert: expected one new hook, got {settings!r}")
    if not _server_hook_current(settings, MARKERS, COMMAND):
        failures.append("fresh insert: should be current after merge")


def _check_survives_rename(failures):
    settings = _settings_with(RENAMED_COMMAND)
    settings["hooks"]["SessionStart"].append({"hooks": [copy.deepcopy(OTHER_HOOK)]})
    _merge_server_hook(settings, MARKERS, COMMAND)
    if _server_commands(settings) != [COMMAND]:
        failures.append(
            f"rename: sentinel entry should be repointed in place, got {_server_commands(settings)!r}"
        )
    groups = settings["hooks"]["SessionStart"]
    if len(groups) != 2 or groups[1]["hooks"][0] != OTHER_HOOK:
        failures.append(f"rename: unrelated hook disturbed: {groups!r}")


def _check_pre_sentinel_migrated(failures):
    settings = _settings_with(PRE_SENTINEL_COMMAND)
    _merge_server_hook(settings, MARKERS, COMMAND)
    if _server_commands(settings) != [COMMAND]:
        failures.append(
            f"migration: pre-sentinel entry should be repointed, got {_server_commands(settings)!r}"
        )


def _check_stale_duplicate_removed(failures):
    # The original failure mode: an entry from an older install alongside a
    # freshly appended one. The merge must leave exactly one.
    settings = _settings_with(RENAMED_COMMAND, COMMAND)
    if _server_hook_current(settings, MARKERS, COMMAND):
        failures.append("current-check: must be False while a stale duplicate exists")
    _merge_server_hook(settings, MARKERS, COMMAND)
    if _server_commands(settings) != [COMMAND]:
        failures.append(
            f"duplicate cleanup: expected exactly one hook, got {_server_commands(settings)!r}"
        )
    if len(settings["hooks"]["SessionStart"]) != 1:
        failures.append(
            f"duplicate cleanup: emptied group should be pruned: {settings['hooks']['SessionStart']!r}"
        )
    if not _server_hook_current(settings, MARKERS, COMMAND):
        failures.append("duplicate cleanup: should be current after merge")


def _check_shared_group_preserved(failures):
    # A stale duplicate sharing a matcher group with an unrelated hook: only
    # our entry is removed, the group survives for the other hook.
    settings = _settings_with(COMMAND)
    settings["hooks"]["SessionStart"].append(
        {
            "hooks": [
                {"type": "command", "command": RENAMED_COMMAND},
                copy.deepcopy(OTHER_HOOK),
            ]
        }
    )
    _merge_server_hook(settings, MARKERS, COMMAND)
    groups = settings["hooks"]["SessionStart"]
    if len(groups) != 2 or groups[1]["hooks"] != [OTHER_HOOK]:
        failures.append(
            f"shared group: unrelated hook lost or group pruned: {groups!r}"
        )
    if _server_commands(settings) != [COMMAND]:
        failures.append(
            f"shared group: stale entry should be gone, got {_server_commands(settings)!r}"
        )


def _check_unrelated_session_start_hook_preserved(failures):
    # A SessionStart hook the user configured for something else entirely
    # (not a stale copy of ours -- no marker match at all) must survive a
    # fresh insert untouched.
    settings = _settings_with(OTHER_HOOK["command"])
    _merge_server_hook(settings, MARKERS, COMMAND)
    groups = settings["hooks"]["SessionStart"]
    if len(groups) != 2 or groups[0]["hooks"][0] != OTHER_HOOK:
        failures.append(f"unrelated hook should be preserved untouched: {groups!r}")
    if _server_commands(settings) != [COMMAND]:
        failures.append(
            f"insert alongside unrelated hook should still add ours, got {_server_commands(settings)!r}"
        )


def _check_command_both_platform_branches(failures):
    # Both arms of _server_hook_command's os.name branch, regardless of the
    # host OS (CI runs this suite on Linux AND Windows; 100% coverage must
    # hold on both). Simulate each platform by patching os.name, then
    # restore it.
    real_name = os.name
    try:
        os.name = "nt"
        _target, nt_command = _server_hook_command("/repo")
        os.name = "posix"
        _target, posix_command = _server_hook_command("/repo")
    finally:
        os.name = real_name

    if "py -3" not in nt_command or "exit 0" not in nt_command:
        failures.append(
            f"nt server command should use 'py -3' and force exit 0; got {nt_command!r}"
        )
    if "python3" not in posix_command or "|| true" not in posix_command:
        failures.append(
            f"POSIX server command should use python3 and '|| true'; got {posix_command!r}"
        )
    for command in (nt_command, posix_command):
        if _SERVER_SENTINEL not in command:
            failures.append(
                f"server command should carry the sentinel; got {command!r}"
            )
        if "--ensure-server" not in command:
            failures.append(
                f"server command should pass --ensure-server; got {command!r}"
            )


def check(failures):
    _check_command_carries_sentinel(failures)
    _check_command_targets_ensure_server(failures)
    _check_fresh_insert(failures)
    _check_survives_rename(failures)
    _check_pre_sentinel_migrated(failures)
    _check_stale_duplicate_removed(failures)
    _check_shared_group_preserved(failures)
    _check_unrelated_session_start_hook_preserved(failures)
    _check_command_both_platform_branches(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: server hook merge keys identity off the sentinel, migrates pre-sentinel "
        "entries, dedupes, prunes empty groups"
    )


if __name__ == "__main__":
    main()
