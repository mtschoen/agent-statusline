"""Verify _find_walker_binary in statusline_lib/walker.py.

Covers all discovery paths: CLAUDE_WALKER_BIN override (hit and miss),
home-directory candidates, shutil.which fallback across all four name
variants, and the not-found (None) return.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.walker as walker_module
from scripts._walker_helpers import restore_walker_state, save_walker_state
from statusline_lib.walker import _find_walker_binary


def _check_env_override_found(failures):
    state = save_walker_state()
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            os.environ[walker_module._WALKER_BIN_ENV] = tmp_path
            result = _find_walker_binary()
            if result != tmp_path:
                failures.append(
                    f"env override to existing file: expected {tmp_path!r}, got {result!r}"
                )
        finally:
            os.unlink(tmp_path)
    finally:
        restore_walker_state(state)


def _check_env_override_not_a_file(failures):
    state = save_walker_state()
    try:
        os.environ[walker_module._WALKER_BIN_ENV] = "/definitely/does/not/exist/walker"
        walker_module.shutil.which = lambda name: None
        walker_module.os.path.isfile = lambda p: False
        result = _find_walker_binary()
        if result is not None:
            failures.append(
                f"env override to missing file should fall through to None, got {result!r}"
            )
    finally:
        restore_walker_state(state)


def _check_home_candidate(failures):
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    try:
        os.environ.pop(walker_module._WALKER_BIN_ENV, None)
        fake_home = tempfile.mkdtemp()
        try:
            build_dir = os.path.join(
                fake_home, "claude-walker", "cpp", "build", "Release"
            )
            os.makedirs(build_dir, exist_ok=True)
            fake_bin = os.path.join(build_dir, "walker.exe")
            with open(fake_bin, "w") as fh:
                fh.write("")

            def fake_expanduser(path):
                if path == "~":
                    return fake_home
                return original_expanduser(path)

            walker_module.os.path.expanduser = fake_expanduser
            result = _find_walker_binary()
            if result != fake_bin:
                failures.append(
                    f"home candidate (Release/walker.exe): expected {fake_bin!r}, got {result!r}"
                )
        finally:
            import shutil as _shutil

            _shutil.rmtree(fake_home, ignore_errors=True)
    finally:
        restore_walker_state(state)


def _check_which_fallback_second_name(failures):
    state = save_walker_state()
    try:
        os.environ.pop(walker_module._WALKER_BIN_ENV, None)
        walker_module.os.path.isfile = lambda p: False
        found_names = []

        def fake_which(name):
            found_names.append(name)
            if name == "claude-walker":
                return "/usr/local/bin/claude-walker"
            return None

        walker_module.shutil.which = fake_which
        result = _find_walker_binary()
        if result != "/usr/local/bin/claude-walker":
            failures.append(
                f"which 'claude-walker': expected '/usr/local/bin/claude-walker', got {result!r}"
            )
        if "claude-walker.exe" not in found_names:
            failures.append(
                "which fallback: 'claude-walker.exe' should have been tried before 'claude-walker'"
            )
    finally:
        restore_walker_state(state)


def _check_which_all_four_names(failures):
    state = save_walker_state()
    try:
        os.environ.pop(walker_module._WALKER_BIN_ENV, None)
        walker_module.os.path.isfile = lambda p: False
        found_names = []

        def fake_which(name):
            found_names.append(name)
            if name == "walker":
                return "/usr/bin/walker"
            return None

        walker_module.shutil.which = fake_which
        result = _find_walker_binary()
        if result != "/usr/bin/walker":
            failures.append(
                f"which 'walker': expected '/usr/bin/walker', got {result!r}"
            )
        expected = ["claude-walker.exe", "claude-walker", "walker.exe", "walker"]
        if found_names != expected:
            failures.append(
                f"which loop order: expected {expected!r}, got {found_names!r}"
            )
    finally:
        restore_walker_state(state)


def _check_not_found_returns_none(failures):
    state = save_walker_state()
    try:
        os.environ.pop(walker_module._WALKER_BIN_ENV, None)
        walker_module.os.path.isfile = lambda p: False
        walker_module.shutil.which = lambda name: None
        result = _find_walker_binary()
        if result is not None:
            failures.append(f"not found: expected None, got {result!r}")
    finally:
        restore_walker_state(state)


def main():
    failures = []
    _check_env_override_found(failures)
    _check_env_override_not_a_file(failures)
    _check_home_candidate(failures)
    _check_which_fallback_second_name(failures)
    _check_which_all_four_names(failures)
    _check_not_found_returns_none(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: _find_walker_binary correctly handles env override, home candidates, "
        "shutil.which fallback (all four names), and not-found"
    )


if __name__ == "__main__":
    main()
