"""Verify the nudge-hook settings merge (statusline_lib.nudge_install, wired
in by install.py): sentinel-based identity that survives a script rename,
basename migration of pre-sentinel entries, stale-duplicate removal,
empty-group pruning, in-place update, fresh insert, and the already-current
check rejecting settings with leftovers.

Pure in-memory checks against the merge helpers -- no settings.json is read or
written. Run from anywhere.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.nudge_install import (
    _NUDGE_SENTINEL,
    _merge_nudge_hook,
    _nudge_command,
    _nudge_hook_current,
    _nudge_markers,
)

TARGET, COMMAND = _nudge_command("/repo")
MARKERS = _nudge_markers(TARGET)
# An entry written by an older install, before a hypothetical script rename:
# different filename and body, same sentinel. Recognizing this WITHOUT any
# list of historical filenames is the property the sentinel exists for.
RENAMED_COMMAND = (
    f'python3 "/repo/some_long_forgotten_name.py" || true {_NUDGE_SENTINEL}'
)
# An entry written by an install from before the sentinel existed: current
# filename, no sentinel. Matched via the basename marker and migrated.
PRE_SENTINEL_COMMAND = 'python3 "/repo/wrap_nudge.py" || true'
# An entry written before the schoen-claude-status -> agent-statusline repo
# rename: current filename, but the sentinel still carries the old repo name
# literally (not derived from the live _NUDGE_SENTINEL, since that now holds
# the new value). Matched via the basename marker, same as PRE_SENTINEL_COMMAND.
LEGACY_REPO_SENTINEL_COMMAND = (
    'python3 "/repo/wrap_nudge.py" || true #managed-by:schoen-claude-status/wrap-nudge'
)
OTHER_HOOK = {"type": "command", "command": "echo unrelated"}


def _settings_with(*commands):
    """Settings dict with one single-hook matcher group per command."""
    return {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": command}]}
                for command in commands
            ]
        }
    }


def _nudge_commands(settings):
    """Every UserPromptSubmit command recognized as ours, in order."""
    found = []
    hook_groups = settings.get("hooks", {})
    for group in hook_groups.get("UserPromptSubmit", []):
        for hook in group.get("hooks", []):
            command = hook.get("command") or ""
            if any(marker in command for marker in MARKERS):
                found.append(command)
    return found


def _check_command_carries_sentinel(failures):
    if _NUDGE_SENTINEL not in COMMAND:
        failures.append(f"command should embed the sentinel, got {COMMAND!r}")


def _check_fresh_insert(failures):
    settings = {}
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(f"fresh insert: expected one new hook, got {settings!r}")
    if not _nudge_hook_current(settings, MARKERS, COMMAND):
        failures.append("fresh insert: should be current after merge")


def _check_survives_rename(failures):
    settings = _settings_with(RENAMED_COMMAND)
    settings["hooks"]["UserPromptSubmit"].append({"hooks": [copy.deepcopy(OTHER_HOOK)]})
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(
            f"rename: sentinel entry should be repointed in place, got {_nudge_commands(settings)!r}"
        )
    groups = settings["hooks"]["UserPromptSubmit"]
    if len(groups) != 2 or groups[1]["hooks"][0] != OTHER_HOOK:
        failures.append(f"rename: unrelated hook disturbed: {groups!r}")


def _check_pre_sentinel_migrated(failures):
    settings = _settings_with(PRE_SENTINEL_COMMAND)
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(
            f"migration: pre-sentinel entry should be repointed, got {_nudge_commands(settings)!r}"
        )


def _check_legacy_repo_sentinel_migrated(failures):
    # An entry stamped with the pre-rename sentinel (literal old repo name,
    # not derived from the live constant) must still be found via the
    # basename marker and repointed onto the current command, including the
    # current sentinel -- proving the rename didn't orphan existing installs.
    settings = _settings_with(LEGACY_REPO_SENTINEL_COMMAND)
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(
            f"legacy sentinel: pre-rename entry should be repointed, got {_nudge_commands(settings)!r}"
        )
    if not _nudge_hook_current(settings, MARKERS, COMMAND):
        failures.append("legacy sentinel: should be current after merge")


def _check_stale_duplicate_removed(failures):
    # The original failure mode: an entry from an older install alongside a
    # freshly appended one. The merge must leave exactly one.
    settings = _settings_with(RENAMED_COMMAND, COMMAND)
    if _nudge_hook_current(settings, MARKERS, COMMAND):
        failures.append("current-check: must be False while a stale duplicate exists")
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(
            f"duplicate cleanup: expected exactly one hook, got {_nudge_commands(settings)!r}"
        )
    if len(settings["hooks"]["UserPromptSubmit"]) != 1:
        failures.append(
            f"duplicate cleanup: emptied group should be pruned: {settings['hooks']['UserPromptSubmit']!r}"
        )
    if not _nudge_hook_current(settings, MARKERS, COMMAND):
        failures.append("duplicate cleanup: should be current after merge")


def _check_shared_group_preserved(failures):
    # A stale duplicate sharing a matcher group with an unrelated hook: only
    # our entry is removed, the group survives for the other hook.
    settings = _settings_with(COMMAND)
    settings["hooks"]["UserPromptSubmit"].append(
        {
            "hooks": [
                {"type": "command", "command": RENAMED_COMMAND},
                copy.deepcopy(OTHER_HOOK),
            ]
        }
    )
    _merge_nudge_hook(settings, MARKERS, COMMAND)
    groups = settings["hooks"]["UserPromptSubmit"]
    if len(groups) != 2 or groups[1]["hooks"] != [OTHER_HOOK]:
        failures.append(
            f"shared group: unrelated hook lost or group pruned: {groups!r}"
        )
    if _nudge_commands(settings) != [COMMAND]:
        failures.append(
            f"shared group: stale entry should be gone, got {_nudge_commands(settings)!r}"
        )


def _check_command_both_platform_branches(failures):
    # Both arms of _nudge_command's os.name branch, regardless of the host OS
    # (CI runs this suite on Linux AND Windows; 100% coverage must hold on
    # both). Simulate each platform by patching os.name, then restore it.
    real_name = os.name
    try:
        os.name = "nt"
        _target, nt_command = _nudge_command("/repo")
        os.name = "posix"
        _target, posix_command = _nudge_command("/repo")
    finally:
        os.name = real_name

    if "py -3" not in nt_command or "; exit 0" not in nt_command:
        failures.append(
            f"nt nudge command should use 'py -3' and '; exit 0'; got {nt_command!r}"
        )
    if "python3" not in posix_command or "|| true" not in posix_command:
        failures.append(
            f"POSIX nudge command should use python3 and '|| true'; got {posix_command!r}"
        )
    for command in (nt_command, posix_command):
        if _NUDGE_SENTINEL not in command:
            failures.append(f"nudge command should carry the sentinel; got {command!r}")


def check(failures):
    _check_command_carries_sentinel(failures)
    _check_fresh_insert(failures)
    _check_survives_rename(failures)
    _check_pre_sentinel_migrated(failures)
    _check_legacy_repo_sentinel_migrated(failures)
    _check_stale_duplicate_removed(failures)
    _check_shared_group_preserved(failures)
    _check_command_both_platform_branches(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: nudge merge keys identity off the sentinel, migrates pre-sentinel "
        "entries, dedupes, prunes empty groups"
    )


if __name__ == "__main__":
    main()
