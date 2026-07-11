"""Verify statusline_lib.settings_io (generic JSON load/atomic-write) and
statusline_lib.claude_family_install (the pure merge helpers extracted from
install.py's near-identical _install_claude / _install_antigravity, wave-2
install-side dedup): missing-script detection, the desired-entries shape,
the already-current check, and the in-place settings merge -- same
dict-in/dict-out shape as nudge_install.py and codex_install.py, so
install.py stays the only place that does I/O and prints progress (aislop's
python-print-debug rule only exempts recognized CLI entry points, and
statusline_lib modules aren't one).

Also smoke-tests install.py itself end to end for the claude and
antigravity platforms against an isolated fake HOME, and (when bash is on
PATH) executes the installed shell wrapper to prove it resolves the shared
interpreter-probe shim and renders.

Exercises real temp-file I/O for settings_io (there is no in-memory
substitute for atomic replace) following scripts/verify_prefs.py's tempfile
pattern. Run from anywhere.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import install
from statusline_lib.claude_family_install import (
    desired_statusline_entries,
    merge_statusline_family_settings,
    missing_required_scripts,
    statusline_family_already_current,
)
from statusline_lib.settings_io import atomic_write_settings, load_settings


@contextlib.contextmanager
def _tmp_dir():
    with tempfile.TemporaryDirectory(prefix="statusline-install-") as d:
        yield d


# ---- statusline_lib.settings_io -------------------------------------------


def _check_load_missing_returns_empty(failures):
    with _tmp_dir() as d:
        got = load_settings(os.path.join(d, "does-not-exist.json"))
        if got != {}:
            failures.append(f"missing settings file should load as {{}}, got {got!r}")


def _check_load_empty_file_returns_empty(failures):
    with _tmp_dir() as d:
        path = os.path.join(d, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("   \n")
        got = load_settings(path)
        if got != {}:
            failures.append(f"blank settings file should load as {{}}, got {got!r}")


def _check_load_valid_object(failures):
    with _tmp_dir() as d:
        path = os.path.join(d, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"foo": "bar"}, f)
        got = load_settings(path)
        if got != {"foo": "bar"}:
            failures.append(f"valid settings object should round-trip, got {got!r}")


def _check_load_non_object_raises_value_error(failures):
    with _tmp_dir() as d:
        path = os.path.join(d, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        try:
            load_settings(path)
            failures.append("a top-level JSON array should raise ValueError")
        except ValueError as e:
            if "not a JSON object" not in str(e):
                failures.append(f"unexpected ValueError message: {e}")


def _check_load_malformed_json_raises_decode_error(failures):
    with _tmp_dir() as d:
        path = os.path.join(d, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        try:
            load_settings(path)
            failures.append("malformed JSON should raise JSONDecodeError")
        except json.JSONDecodeError:
            pass


def _check_atomic_write_creates_parent_and_round_trips(failures):
    with _tmp_dir() as d:
        path = os.path.join(d, "nested", "settings.json")
        atomic_write_settings(path, {"a": 1})
        if not os.path.exists(path):
            failures.append("atomic_write_settings should create missing parent dirs")
        if os.path.exists(path + ".tmp"):
            failures.append("atomic_write_settings should not leave a .tmp file behind")
        with open(path, encoding="utf-8") as f:
            if json.load(f) != {"a": 1}:
                failures.append("written settings did not round-trip")


# ---- statusline_lib.claude_family_install ----------------------------------

_NUDGE_SENTINEL = "#managed-by:test"
_MAIN_COMMAND = 'py -3 "/repo/statusline.py"'
_SUBAGENT_COMMAND = 'py -3 "/repo/subagent_statusline.py"'
_NUDGE_COMMAND = f'py -3 "/repo/wrap_nudge.py" {_NUDGE_SENTINEL}'
_NUDGE_MARKERS = (_NUDGE_SENTINEL,)


def _check_missing_required_scripts(failures):
    with _tmp_dir() as d:
        present = os.path.join(d, "present.py")
        with open(present, "w", encoding="utf-8") as f:
            f.write("# stub\n")
        missing_one = os.path.join(d, "missing.py")

        got = missing_required_scripts(present, missing_one)
        if got != (missing_one,):
            failures.append(f"expected only the missing path back, got {got!r}")

        got = missing_required_scripts(present)
        if got != ():
            failures.append(
                f"all-present should report no missing scripts, got {got!r}"
            )

        got = missing_required_scripts(missing_one, present)
        if got != (missing_one,):
            failures.append(
                f"missing script should be reported regardless of position, got {got!r}"
            )


def _check_desired_statusline_entries_shape(failures):
    desired_statusline, desired_subagent = desired_statusline_entries(
        _MAIN_COMMAND, _SUBAGENT_COMMAND, 3
    )
    if desired_statusline != {
        "type": "command",
        "command": _MAIN_COMMAND,
        "refreshInterval": 3,
    }:
        failures.append(f"unexpected desired_statusline shape: {desired_statusline!r}")
    if desired_subagent != {"type": "command", "command": _SUBAGENT_COMMAND}:
        failures.append(f"unexpected desired_subagent shape: {desired_subagent!r}")


def _check_already_current_and_merge_round_trip(failures):
    desired_statusline, desired_subagent = desired_statusline_entries(
        _MAIN_COMMAND, _SUBAGENT_COMMAND, 3
    )
    settings = {}

    if statusline_family_already_current(
        settings, desired_statusline, desired_subagent, _NUDGE_MARKERS, _NUDGE_COMMAND
    ):
        failures.append("empty settings should not already be current")

    merge_statusline_family_settings(
        settings, desired_statusline, desired_subagent, _NUDGE_MARKERS, _NUDGE_COMMAND
    )

    if settings.get("statusLine") != desired_statusline:
        failures.append(f"merge should install statusLine, got {settings!r}")
    if settings.get("subagentStatusLine") != desired_subagent:
        failures.append(f"merge should install subagentStatusLine, got {settings!r}")
    hooks = settings.get("hooks") or {}
    if not hooks.get("UserPromptSubmit"):
        failures.append(f"merge should also install the nudge hook, got {settings!r}")

    if not statusline_family_already_current(
        settings, desired_statusline, desired_subagent, _NUDGE_MARKERS, _NUDGE_COMMAND
    ):
        failures.append("settings should be current immediately after merge")


def _check_already_current_requires_every_piece(failures):
    # Each of the three pieces (statusLine, subagentStatusLine, nudge hook)
    # must independently gate already-current -- a partial match (e.g. an
    # older install that predates the nudge hook) is not current.
    desired_statusline, desired_subagent = desired_statusline_entries(
        _MAIN_COMMAND, _SUBAGENT_COMMAND, 3
    )
    settings = {
        "statusLine": desired_statusline,
        "subagentStatusLine": desired_subagent,
    }
    if statusline_family_already_current(
        settings, desired_statusline, desired_subagent, _NUDGE_MARKERS, _NUDGE_COMMAND
    ):
        failures.append(
            "settings missing the nudge hook should not be reported already-current"
        )


# ---- install.py end-to-end smoke -------------------------------------------


@contextlib.contextmanager
def _fake_home():
    """Point os.path.expanduser at an isolated temp HOME so the smoke test
    never touches the real ~/.claude or ~/.gemini."""
    with tempfile.TemporaryDirectory(prefix="statusline-fake-home-") as home:
        real_expanduser = os.path.expanduser

        def fake_expanduser(path):
            if path == "~" or path.startswith(("~/", "~\\")):
                return home + path[1:]
            return real_expanduser(path)

        os.path.expanduser = fake_expanduser
        try:
            yield home
        finally:
            os.path.expanduser = real_expanduser


def _check_install_claude_smoke(failures):
    # Assertions must stay inside the fake-home scope -- TemporaryDirectory
    # deletes the tree on exit, so checking os.path.exists afterward would
    # always report False regardless of what install.py actually wrote.
    with _fake_home() as home:
        with contextlib.redirect_stdout(io.StringIO()):
            code = install._install_claude(REPO, dry_run=False)
        if code != 0:
            failures.append(f"install.py claude smoke should return 0, got {code!r}")
        settings_path = os.path.join(home, ".claude", "settings.json")
        if not os.path.exists(settings_path):
            failures.append(
                "install.py claude smoke should write ~/.claude/settings.json"
            )
            return
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
        if "statusLine" not in settings or "subagentStatusLine" not in settings:
            failures.append(f"claude settings missing statusline keys: {settings!r}")
        hooks = settings.get("hooks") or {}
        if not hooks.get("UserPromptSubmit"):
            failures.append(f"claude settings missing the nudge hook: {settings!r}")


def _check_install_antigravity_smoke(failures):
    with _fake_home() as home:
        with contextlib.redirect_stdout(io.StringIO()):
            code = install._install_antigravity(REPO, dry_run=False)
        if code != 0:
            failures.append(
                f"install.py antigravity smoke should return 0, got {code!r}"
            )
        settings_path = os.path.join(
            home, ".gemini", "antigravity-cli", "settings.json"
        )
        if not os.path.exists(settings_path):
            failures.append(
                "install.py antigravity smoke should write "
                "~/.gemini/antigravity-cli/settings.json"
            )
            return
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
        status_line = settings.get("statusLine") or {}
        command = status_line.get("command", "")
        if "--statusline-platform antigravity" not in command:
            failures.append(
                f"antigravity command should carry the routing flag, got {command!r}"
            )


def _check_installed_wrapper_resolves_shim(failures):
    # Executes the real statusline-command.sh (not a copy) from its repo
    # location -- proves the sourced interpreter-probe.sh shim actually
    # resolves relative to the sourcing script and the wrapper still renders.
    bash = shutil.which("bash")
    if bash is None:
        print("SKIP: bash not on PATH -- cannot exercise the shell wrapper here")
        return
    result = subprocess.run(
        [bash, os.path.join(REPO, "statusline-command.sh")],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        failures.append(
            f"statusline-command.sh should exit 0 via the sourced shim, "
            f"got {result.returncode}: stderr={result.stderr!r}"
        )
    if not result.stdout.strip():
        failures.append("statusline-command.sh should render non-empty output")


def check(failures):
    _check_load_missing_returns_empty(failures)
    _check_load_empty_file_returns_empty(failures)
    _check_load_valid_object(failures)
    _check_load_non_object_raises_value_error(failures)
    _check_load_malformed_json_raises_decode_error(failures)
    _check_atomic_write_creates_parent_and_round_trips(failures)
    _check_missing_required_scripts(failures)
    _check_desired_statusline_entries_shape(failures)
    _check_already_current_and_merge_round_trip(failures)
    _check_already_current_requires_every_piece(failures)
    _check_install_claude_smoke(failures)
    _check_install_antigravity_smoke(failures)
    _check_installed_wrapper_resolves_shim(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: settings_io load/atomic-write, the claude-family merge helpers, and "
        "install.py's end-to-end claude/antigravity smoke (incl. the sourced "
        "wrapper shim) all behave"
    )


if __name__ == "__main__":
    main()
