"""Verify the Kimi Code CLI install helpers: statusline_lib.kimi_install's
`[status_line] command` TOML merge (same text-surgery + tomllib re-parse
contract as codex_install.py) and statusline_lib.platform_commands's
`_kimi_command_for_platform` routing on BOTH OS arms (os.name patched to
force the foreign arm, same as scripts/verify_install_qwen_pi.py).

Also smoke-tests install.py's kimi wiring end to end (the
_install_toml_platform kimi branch) against an isolated fake HOME -- real
temp-dir fixtures only, never the real ~/.kimi-code.

Run from anywhere.
"""

import os
import sys
import tempfile
import tomllib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import install
import statusline_lib.kimi_install as kimi_install
from statusline_lib.kimi_install import kimi_config_current, merge_kimi_config
from statusline_lib.platform_commands import (
    _kimi_command_for_platform,
    _kimi_space_path_warning,
)

COMMAND_POSIX = 'bash "/repo/kimi-statusline-command.sh"'
COMMAND_NT = "py -3 C:/repo/kimi_statusline.py"


def _expect_value_error(failures, label, text, command, message):
    try:
        merge_kimi_config(text, command)
    except ValueError as exc:
        if message not in str(exc):
            failures.append(f"{label}: wrong error: {exc}")
    else:
        failures.append(f"{label}: expected ValueError")


def _check_command_windows(failures):
    real_name = os.name
    try:
        os.name = "nt"
        target, command = _kimi_command_for_platform("/repo")
    finally:
        os.name = real_name
    if target != "/repo/kimi_statusline.py":
        failures.append(f"kimi nt target unexpected: {target!r}")
    # UNQUOTED form: kimi-code spawns via Node spawn(cmd.exe, ["/d", "/s",
    # "/c", command]), which mangles any quoted command string -- verified
    # live against kimi-code's status-line-command.ts (quoted form exits 2
    # with a garbage path; unquoted exits 0). Requires a space-free repo
    # path, which _kimi_space_path_warning exists to flag.
    if command != "py -3 /repo/kimi_statusline.py":
        failures.append(f"kimi nt command unexpected: {command!r}")


def _check_kimi_space_path_warning(failures):
    real_name = os.name
    try:
        os.name = "nt"
        spaced = _kimi_space_path_warning("/repo with spaces/kimi_statusline.py")
        unspaced = _kimi_space_path_warning("/repo/kimi_statusline.py")
    finally:
        os.name = real_name
    if spaced is None or "space-free" not in spaced:
        failures.append(f"nt space path should warn, got {spaced!r}")
    if unspaced is not None:
        failures.append(f"nt space-free path should not warn, got {unspaced!r}")
    real_name = os.name
    try:
        os.name = "posix"
        posix_spaced = _kimi_space_path_warning("/repo with spaces/kimi_statusline.py")
    finally:
        os.name = real_name
    if posix_spaced is not None:
        failures.append(f"posix space path should not warn, got {posix_spaced!r}")


def _check_command_posix(failures):
    real_name = os.name
    try:
        os.name = "posix"
        target, command = _kimi_command_for_platform("/repo")
    finally:
        os.name = real_name
    if target != "/repo/kimi_statusline.py":
        failures.append(f"kimi posix target unexpected: {target!r}")
    if command != 'bash "/repo/kimi-statusline-command.sh"':
        failures.append(f"kimi posix command unexpected: {command!r}")


def _check_merge_empty_config(failures):
    merged = merge_kimi_config("", COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"empty config: command not installed, got {merged!r}")
    if not kimi_config_current(merged, COMMAND_POSIX):
        failures.append("empty config: merged result should be current")


def _check_merge_no_trailing_newline(failures):
    """A file whose last byte is not a newline needs a separator before the
    appended section header."""
    merged = merge_kimi_config('model = "k3"', COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["model"] != "k3" or parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"no trailing newline: bad append, got {merged!r}")


def _check_merge_section_insert_preserves_siblings(failures):
    """An existing [status_line] section without a command key gets the
    command inserted under it; the items list (built-in footer slots, which
    kimi-code owns) and comments are preserved verbatim."""
    text = (
        'theme = "dark"\n'
        "\n"
        "[status_line] # footer config\n"
        'items = ["model", "cwd"]\n'
        "# a note about the footer\n"
        "\n"
        "[editor]\n"
        "line_numbers = true\n"
    )
    merged = merge_kimi_config(text, COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    status_line = parsed["status_line"]
    if status_line["command"] != COMMAND_POSIX:
        failures.append(f"section insert: command not installed, got {merged!r}")
    if status_line["items"] != ["model", "cwd"]:
        failures.append(f"section insert: items clobbered, got {merged!r}")
    if "# footer config" not in merged or "# a note about the footer" not in merged:
        failures.append(f"section insert: comments not preserved, got {merged!r}")
    if parsed["editor"]["line_numbers"] is not True or parsed["theme"] != "dark":
        failures.append(f"section insert: unrelated content changed, got {merged!r}")


def _check_merge_replaces_existing_command(failures):
    text = '[status_line]\ncommand = "stale command"\nitems = ["model"]\n'
    merged = merge_kimi_config(text, COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"replace: stale command not overwritten, got {merged!r}")
    if parsed["status_line"]["items"] != ["model"]:
        failures.append(f"replace: sibling items key clobbered, got {merged!r}")
    if "stale command" in merged:
        failures.append(f"replace: stale command line survived, got {merged!r}")


def _check_merge_replaces_command_at_eof_without_newline(failures):
    """The command assignment as the literal last bytes of the file (no
    trailing newline) exercises the replace span's EOF branch."""
    merged = merge_kimi_config('[status_line]\ncommand = "stale"', COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"EOF replace: command not overwritten, got {merged!r}")


def _check_merge_bare_section_header_at_eof(failures):
    """A [status_line] header as the last line without a trailing newline:
    the insert separator must start the command on its own line."""
    merged = merge_kimi_config("[status_line]", COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"bare header: command not installed, got {merged!r}")


def _check_merge_commented_out_command_is_not_replaced(failures):
    """A commented-out `# command = ...` line is NOT an assignment: the merge
    must insert a real command line and leave the comment untouched."""
    text = '[status_line]\n# command = "old idea"\n'
    merged = merge_kimi_config(text, COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"commented command: not installed, got {merged!r}")
    if '# command = "old idea"' not in merged:
        failures.append(f"commented command: comment clobbered, got {merged!r}")


def _check_merge_command_only_matches_status_line_section(failures):
    """A `command` key under a DIFFERENT table must not be touched; the
    command is inserted under [status_line] instead."""
    text = '[tasks]\ncommand = "make test"\n\n[status_line]\nitems = ["model"]\n'
    merged = merge_kimi_config(text, COMMAND_POSIX)
    parsed = tomllib.loads(merged)
    if parsed["tasks"]["command"] != "make test":
        failures.append(f"wrong-section: tasks.command clobbered, got {merged!r}")
    if parsed["status_line"]["command"] != COMMAND_POSIX:
        failures.append(f"wrong-section: command not installed, got {merged!r}")


def _check_merge_crlf_preserved(failures):
    text = '[status_line]\r\ncommand = "stale"\r\n'
    merged = merge_kimi_config(text, COMMAND_NT)
    if "\r\n" not in merged:
        failures.append(f"crlf: line endings not preserved, got {merged!r}")
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != COMMAND_NT:
        failures.append(f"crlf: command not overwritten, got {merged!r}")


def _check_quoting_escapes_round_trip(failures):
    """A command string carrying literal double-quotes or backslashes (not
    the emitted Windows form -- that is deliberately unquoted -- but possible
    from a hand-edited config) must escape into a TOML basic string that
    parses back to the exact value."""
    quoted = 'py -3 "C:/repo/kimi_statusline.py"'
    merged = merge_kimi_config("", quoted)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != quoted:
        failures.append(
            f"quoting: command did not round-trip, got "
            f"{parsed['status_line']['command']!r}"
        )
    backslashed = r"py -3 C:\repo\kimi_statusline.py"
    merged = merge_kimi_config("", backslashed)
    parsed = tomllib.loads(merged)
    if parsed["status_line"]["command"] != backslashed:
        failures.append(
            f"quoting: backslashed command did not round-trip, got "
            f"{parsed['status_line']['command']!r}"
        )


def _check_merge_invalid_toml(failures):
    _expect_value_error(
        failures, "invalid TOML", "not = [valid", COMMAND_POSIX, "invalid TOML"
    )


def _check_merge_dotted_or_inline_form_refused(failures):
    _expect_value_error(
        failures,
        "dotted status_line key",
        'status_line.command = "legacy"\n',
        COMMAND_POSIX,
        "unsupported dotted or inline-table form",
    )
    _expect_value_error(
        failures,
        "inline status_line table",
        'status_line = { command = "legacy" }\n',
        COMMAND_POSIX,
        "unsupported dotted or inline-table form",
    )


def _check_merge_integrity_guard(failures):
    """The re-parse safety net: if the emitted command line disagrees with
    what the merge claims to install (here forced by monkeypatching
    _command_line, the same way verify_codex_install.py forces the codex
    guard), merge_kimi_config must fail loudly rather than return a config
    that doesn't run the requested command."""
    real_command_line = kimi_install._command_line
    try:
        kimi_install._command_line = lambda command, newline: (
            f'command = "wrong"{newline}'
        )
        _expect_value_error(
            failures,
            "integrity guard",
            "[status_line]\n",
            COMMAND_POSIX,
            "did not retain",
        )
    finally:
        kimi_install._command_line = real_command_line


def _check_kimi_config_current(failures):
    if kimi_config_current("", COMMAND_POSIX):
        failures.append("empty config should not be reported current")
    matching = merge_kimi_config("", COMMAND_POSIX)
    if not kimi_config_current(matching, COMMAND_POSIX):
        failures.append("an exact match should be reported current")
    stale = '[status_line]\ncommand = "something else"\n'
    if kimi_config_current(stale, COMMAND_POSIX):
        failures.append("a mismatched command should not be current")
    bare_string = 'status_line = "not a table"\n'
    if kimi_config_current(bare_string, COMMAND_POSIX):
        failures.append("a bare-string status_line should not be reported current")


def _run_install_kimi(tmp_home, repo, dry_run):
    """Run install.py's kimi branch in-process with HOME faked to tmp_home."""
    original_environ = os.environ.copy()
    try:
        os.environ["HOME"] = tmp_home
        os.environ["USERPROFILE"] = tmp_home
        return install._install_toml_platform(repo, dry_run, "kimi")
    finally:
        os.environ.clear()
        os.environ.update(original_environ)


def _check_install_end_to_end(failures):
    """install.py's kimi wiring: fresh install writes tui.toml, a re-run
    reports already-current, --dry-run writes nothing, a missing entry script
    and invalid existing TOML both fail with exit 1."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, ".kimi-code", "tui.toml")
        # Forward-slashed, matching install.py main()'s repo normalization.
        repo = REPO.replace("\\", "/")
        if _run_install_kimi(tmp, repo, dry_run=True) != 0:
            failures.append("kimi install dry-run should exit 0")
        if os.path.exists(config_path):
            failures.append("kimi install dry-run must not write tui.toml")
        if _run_install_kimi(tmp, repo, dry_run=False) != 0:
            failures.append("kimi install should exit 0")
        if not os.path.exists(config_path):
            failures.append("kimi install should write tui.toml")
        else:
            with open(config_path, encoding="utf-8") as f:
                parsed = tomllib.loads(f.read())
            _target, command = _kimi_command_for_platform(repo)
            if parsed["status_line"]["command"] != command:
                failures.append(
                    f"kimi install wrote the wrong command: "
                    f"{parsed['status_line']['command']!r} != {command!r}"
                )
            if _run_install_kimi(tmp, repo, dry_run=False) != 0:
                failures.append("kimi install re-run should exit 0 (already current)")
        if _run_install_kimi(tmp, "/nonexistent-repo", dry_run=False) != 1:
            failures.append("kimi install with a missing entry script should exit 1")


def _check_install_invalid_existing_config(failures):
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = os.path.join(tmp, ".kimi-code")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "tui.toml"), "w", encoding="utf-8") as f:
            f.write("not = [valid")
        if _run_install_kimi(tmp, REPO, dry_run=False) != 1:
            failures.append("kimi install over invalid TOML should exit 1 (refusal)")


def check(failures):
    _check_command_windows(failures)
    _check_kimi_space_path_warning(failures)
    _check_command_posix(failures)
    _check_merge_empty_config(failures)
    _check_merge_no_trailing_newline(failures)
    _check_merge_section_insert_preserves_siblings(failures)
    _check_merge_replaces_existing_command(failures)
    _check_merge_replaces_command_at_eof_without_newline(failures)
    _check_merge_bare_section_header_at_eof(failures)
    _check_merge_commented_out_command_is_not_replaced(failures)
    _check_merge_command_only_matches_status_line_section(failures)
    _check_merge_crlf_preserved(failures)
    _check_quoting_escapes_round_trip(failures)
    _check_merge_invalid_toml(failures)
    _check_merge_dotted_or_inline_form_refused(failures)
    _check_merge_integrity_guard(failures)
    _check_kimi_config_current(failures)
    _check_install_end_to_end(failures)
    _check_install_invalid_existing_config(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: kimi [status_line] command TOML merge, platform routing (both "
        "OS arms), and install.py kimi wiring all behave"
    )


if __name__ == "__main__":
    main()
