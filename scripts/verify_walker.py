"""Verify _walker_root_list and _walker_subcommand in statusline_lib/walker.py.

Covers:
  - _walker_root_list: missing config file, malformed JSON, valid extra_roots,
    non-list extra_roots, realpath deduplication, non-existent dirs filtered,
    OSError from os.path.realpath falling back to normpath.
  - _walker_subcommand: binary not found, subprocess success with JSON,
    non-zero returncode, empty stdout, TimeoutExpired, OSError, JSON parse error.

Run from anywhere; imports from schoen-claude-status by path.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.walker as walker_module
from scripts._walker_helpers import restore_walker_state, save_walker_state
from statusline_lib.walker import _walker_root_list, _walker_subcommand


def _fake_expanduser_for(tmp, original):
    def fake_expanduser(path):
        if path == "~":
            return tmp
        return original(path)

    return fake_expanduser


def _check_root_list_missing_config(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            walker_module._WALKER_ROOTS_CONFIG_PATH = os.path.join(
                tmp, "nonexistent-walker-roots.json"
            )
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            result = _walker_root_list()
            canon_default = os.path.realpath(default_dir)
            if result != [canon_default]:
                failures.append(
                    f"missing config: expected [{canon_default!r}], got {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_root_list_malformed_json(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "walker-roots.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            walker_module._WALKER_ROOTS_CONFIG_PATH = config_path
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            result = _walker_root_list()
            canon_default = os.path.realpath(default_dir)
            if result != [canon_default]:
                failures.append(
                    f"malformed JSON: expected [{canon_default!r}], got {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_root_list_extra_roots(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            extra1 = os.path.join(tmp, "extra1")
            os.makedirs(extra1, exist_ok=True)
            extra2 = os.path.join(tmp, "extra2_nonexistent")
            config_path = os.path.join(tmp, "walker-roots.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"extra_roots": [extra1, extra2]}, fh)
            walker_module._WALKER_ROOTS_CONFIG_PATH = config_path
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            result = _walker_root_list()
            canon_default = os.path.realpath(default_dir)
            canon_extra1 = os.path.realpath(extra1)
            if canon_default not in result:
                failures.append(
                    f"extra_roots: default dir missing from result {result!r}"
                )
            if canon_extra1 not in result:
                failures.append(
                    f"extra_roots: extra1 dir missing from result {result!r}"
                )
            canon_extra2 = os.path.realpath(extra2)
            if canon_extra2 in result:
                failures.append(
                    f"extra_roots: nonexistent extra2 should not appear in {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_root_list_dedup(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            config_path = os.path.join(tmp, "walker-roots.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"extra_roots": [default_dir]}, fh)
            walker_module._WALKER_ROOTS_CONFIG_PATH = config_path
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            result = _walker_root_list()
            canon_default = os.path.realpath(default_dir)
            if result.count(canon_default) != 1:
                failures.append(
                    f"dedup: expected exactly one occurrence, got {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_root_list_non_list_extra_roots(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "walker-roots.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"extra_roots": "not-a-list"}, fh)
            walker_module._WALKER_ROOTS_CONFIG_PATH = config_path
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            result = _walker_root_list()
            canon_default = os.path.realpath(default_dir)
            if result != [canon_default]:
                failures.append(
                    f"non-list extra_roots: expected [{canon_default!r}], got {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_root_list_realpath_oserror(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        with tempfile.TemporaryDirectory() as tmp:
            default_dir = os.path.join(tmp, ".claude", "projects")
            os.makedirs(default_dir, exist_ok=True)
            walker_module._WALKER_ROOTS_CONFIG_PATH = os.path.join(
                tmp, "nonexistent-walker-roots.json"
            )
            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            walker_module.os.path.realpath = lambda p: (_ for _ in ()).throw(
                OSError("simulated realpath failure")
            )
            result = _walker_root_list()
            norm_default = os.path.normpath(default_dir)
            if norm_default not in result:
                failures.append(
                    f"realpath OSError fallback: normpath {norm_default!r} not in {result!r}"
                )
    finally:
        restore_walker_state(state)


def _check_subcommand_no_binary(failures):
    state = save_walker_state()
    try:
        os.environ.pop(walker_module._WALKER_BIN_ENV, None)
        walker_module.os.path.isfile = lambda p: False
        walker_module.shutil.which = lambda name: None
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"no binary: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_success(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        class FakeResult:
            returncode = 0
            stdout = '{"sessions": [1, 2, 3]}'
            stderr = ""

        walker_module.subprocess.run = lambda cmd, **kwargs: FakeResult()
        result = _walker_subcommand("list")
        if result != {"sessions": [1, 2, 3]}:
            failures.append(f"success: expected parsed JSON, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_nonzero_returncode(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        class FakeResult:
            returncode = 1
            stdout = '{"ok": true}'
            stderr = "error"

        walker_module.subprocess.run = lambda cmd, **kwargs: FakeResult()
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"nonzero returncode: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_empty_stdout(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        class FakeResult:
            returncode = 0
            stdout = "   \n"
            stderr = ""

        walker_module.subprocess.run = lambda cmd, **kwargs: FakeResult()
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"empty stdout: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_timeout(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 2)

        walker_module.subprocess.run = fake_run
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"TimeoutExpired: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_oserror(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        def fake_run(cmd, **kwargs):
            raise OSError("no such file")

        walker_module.subprocess.run = fake_run
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"OSError: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_subcommand_json_parse_failure(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/fake/walker"
        walker_module.os.path.isfile = lambda p: p == "/fake/walker"

        class FakeResult:
            returncode = 0
            stdout = "not json at all !!!"
            stderr = ""

        walker_module.subprocess.run = lambda cmd, **kwargs: FakeResult()
        result = _walker_subcommand("list")
        if result is not None:
            failures.append(f"JSON parse failure: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def _check_root_list_platform_branches(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    original_environ = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # We want default paths to exist so they aren't filtered out of _walker_root_list()
            # default = ~/.gemini/antigravity-cli/brain
            anti_brain = os.path.join(tmp, ".gemini", "antigravity-cli", "brain")
            os.makedirs(anti_brain, exist_ok=True)
            # default = ~/.claude/projects
            claude_projects = os.path.join(tmp, ".claude", "projects")
            os.makedirs(claude_projects, exist_ok=True)

            walker_module.os.path.expanduser = _fake_expanduser_for(
                tmp, original_expanduser
            )
            # 1. STATUSLINE_PLATFORM = "antigravity"
            os.environ.clear()
            os.environ["STATUSLINE_PLATFORM"] = "antigravity"
            res = _walker_root_list()
            if os.path.realpath(anti_brain) not in res:
                failures.append(f"platform=antigravity missing anti_brain, got {res!r}")

            # 2. STATUSLINE_PLATFORM = "claude"
            os.environ.clear()
            os.environ["STATUSLINE_PLATFORM"] = "claude"
            res = _walker_root_list()
            if os.path.realpath(claude_projects) not in res:
                failures.append(f"platform=claude missing claude_projects, got {res!r}")

            # 3. No env variables at all
            os.environ.clear()
            res = _walker_root_list()
            if os.path.realpath(claude_projects) not in res:
                failures.append(f"no platform env missing claude_projects, got {res!r}")

    finally:
        os.environ.clear()
        os.environ.update(original_environ)
        restore_walker_state(state)
        walker_module.os.path.expanduser = original_expanduser


def main():
    failures = []

    _check_root_list_missing_config(failures)
    _check_root_list_malformed_json(failures)
    _check_root_list_extra_roots(failures)
    _check_root_list_dedup(failures)
    _check_root_list_non_list_extra_roots(failures)
    _check_root_list_realpath_oserror(failures)
    _check_root_list_platform_branches(failures)

    _check_subcommand_no_binary(failures)
    _check_subcommand_success(failures)
    _check_subcommand_nonzero_returncode(failures)
    _check_subcommand_empty_stdout(failures)
    _check_subcommand_timeout(failures)
    _check_subcommand_oserror(failures)
    _check_subcommand_json_parse_failure(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: _walker_root_list and _walker_subcommand behave correctly across "
        "all config, dedup, filter, and subprocess error paths"
    )


if __name__ == "__main__":
    main()
