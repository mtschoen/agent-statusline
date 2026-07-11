"""Verify statusline_lib/base.py's shared state-dir/sanitize resolver and the
entry-script glue helpers (hostname, is_local_mode, spinner_frame, safe_write,
log_traceback) that statusline.py / qwen_statusline.py / subagent_statusline.py
used to each define locally.

Most of these are also exercised indirectly by the entry scripts' own verify
suites (e.g. every statusline.py render calls hostname()/is_local_mode()/
spinner_frame()/safe_write()), but those run the entry scripts as subprocesses
in several places, which coverage.py doesn't see -- this script calls each
helper directly, in-process, so every branch (including the error paths) is
covered regardless of what the entry-point suites happen to exercise.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.base as _base
from statusline_lib.base import (
    SPINNER_FRAMES,
    hostname,
    is_local_mode,
    log_traceback,
    safe_write,
    sanitize_state_key,
    spinner_frame,
    state_dir,
)


def check_state_dir_explicit_arg_wins(failures):
    if state_dir("/explicit/path") != "/explicit/path":
        failures.append("an explicit state_dir arg must win over everything else")


def check_state_dir_env_vars(failures):
    saved_claude = os.environ.pop("CLAUDE_STATE_DIR", None)
    saved_agy = os.environ.pop("ANTIGRAVITY_STATE_DIR", None)
    try:
        os.environ["CLAUDE_STATE_DIR"] = "/claude/state"
        os.environ["ANTIGRAVITY_STATE_DIR"] = "/agy/state"
        if state_dir() != "/claude/state":
            failures.append("CLAUDE_STATE_DIR must win over ANTIGRAVITY_STATE_DIR")

        os.environ.pop("CLAUDE_STATE_DIR")
        if state_dir() != "/agy/state":
            failures.append(
                "ANTIGRAVITY_STATE_DIR must be used when CLAUDE_STATE_DIR is unset"
            )

        os.environ.pop("ANTIGRAVITY_STATE_DIR")
        expected = os.path.join(_base.app_dir(), "state")
        if state_dir() != expected:
            failures.append(
                f"with no override, state_dir() must default to app_dir()/state; "
                f"got {state_dir()!r}, expected {expected!r}"
            )
    finally:
        for name, value in (
            ("CLAUDE_STATE_DIR", saved_claude),
            ("ANTIGRAVITY_STATE_DIR", saved_agy),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_sanitize_state_key(failures):
    if sanitize_state_key("abc-123_XYZ") != "abc-123_XYZ":
        failures.append("alnum/-/_ characters must pass through unchanged")
    if sanitize_state_key("a/b\\c:d") != "abcd":
        failures.append("path-unsafe characters must be stripped")
    if sanitize_state_key(None) != "":
        failures.append("None must sanitize to an empty string")
    if sanitize_state_key(12345) != "12345":
        failures.append("non-string keys must coerce to str before filtering")


def check_hostname_success(failures):
    name = hostname()
    if not name or "." in name:
        failures.append(
            f"hostname() should be a non-empty, domain-stripped name; got {name!r}"
        )


def check_hostname_strips_domain_and_falls_back(failures):
    original = _base.socket.gethostname
    try:
        _base.socket.gethostname = lambda: "myhost.example.com"
        if hostname() != "myhost":
            failures.append("hostname() must strip everything after the first dot")

        _base.socket.gethostname = lambda: ""
        if hostname() != "unknown":
            failures.append("an empty hostname must fall back to 'unknown'")

        def _raise():
            raise OSError("no hostname")

        _base.socket.gethostname = _raise
        if hostname() != "unknown":
            failures.append("a gethostname() OSError must degrade to 'unknown'")
    finally:
        _base.socket.gethostname = original


def check_is_local_mode_env_vars(failures):
    saved_claude = os.environ.pop("CLAUDE_LOCAL_MODE", None)
    saved_agy = os.environ.pop("ANTIGRAVITY_LOCAL_MODE", None)
    original_app_dir = _base.app_dir
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _base.app_dir = lambda: tmp
            if is_local_mode():
                failures.append("no env var and no marker file must not be local mode")

            os.environ["CLAUDE_LOCAL_MODE"] = "1"
            if not is_local_mode():
                failures.append("CLAUDE_LOCAL_MODE=1 must be local mode")
            os.environ.pop("CLAUDE_LOCAL_MODE")

            os.environ["ANTIGRAVITY_LOCAL_MODE"] = "1"
            if not is_local_mode():
                failures.append("ANTIGRAVITY_LOCAL_MODE=1 must be local mode")
            os.environ.pop("ANTIGRAVITY_LOCAL_MODE")

            with open(os.path.join(tmp, ".local-mode"), "w", encoding="utf-8") as f:
                f.write("")
            if not is_local_mode():
                failures.append("a .local-mode marker file must be local mode")
    finally:
        _base.app_dir = original_app_dir
        if saved_claude is None:
            os.environ.pop("CLAUDE_LOCAL_MODE", None)
        else:
            os.environ["CLAUDE_LOCAL_MODE"] = saved_claude
        if saved_agy is None:
            os.environ.pop("ANTIGRAVITY_LOCAL_MODE", None)
        else:
            os.environ["ANTIGRAVITY_LOCAL_MODE"] = saved_agy


def check_spinner_frame(failures):
    frame = spinner_frame()
    if frame not in SPINNER_FRAMES:
        failures.append(
            f"spinner_frame() must return one of SPINNER_FRAMES; got {frame!r}"
        )

    original_time = _base.time.time
    try:
        _base.time.time = lambda: 0.0
        if spinner_frame() != SPINNER_FRAMES[0]:
            failures.append("spinner_frame() at t=0 must be the first frame")
    finally:
        _base.time.time = original_time


def check_safe_write_success_and_failure(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.txt")
        safe_write(path, "hello")
        with open(path, encoding="utf-8") as f:
            if f.read() != "hello":
                failures.append("safe_write must write the given text verbatim")

        blocker = os.path.join(tmp, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        safe_write(os.path.join(blocker, "unwritable.txt"), "x")  # must not raise


def check_log_traceback_success_and_failure(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "error.log")
        try:
            raise ValueError("boom")
        except ValueError:
            log_traceback(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if "boom" not in content or "ValueError" not in content:
            failures.append(f"log_traceback must record the exception; got {content!r}")

        blocker = os.path.join(tmp, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        log_traceback(os.path.join(blocker, "unwritable.log"))  # must not raise


def main():
    failures = []
    for check in (
        check_state_dir_explicit_arg_wins,
        check_state_dir_env_vars,
        check_sanitize_state_key,
        check_hostname_success,
        check_hostname_strips_domain_and_falls_back,
        check_is_local_mode_env_vars,
        check_spinner_frame,
        check_safe_write_success_and_failure,
        check_log_traceback_success_and_failure,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: shared state-dir/sanitize/entry-glue helpers all verified")


if __name__ == "__main__":
    main()
