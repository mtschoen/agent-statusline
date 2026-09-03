"""Verify qwen_statusline.py's own contract, separate from the client and
the render adapter that produces the line it prints.

Two things are this file's job now that rendering has moved to the resident
server (statusline.py's wrapper delegation is gone -- see PLAN.md's
resident-server redesign): the shim must inject `--statusline-platform
qwen` into sys.argv before it imports statusline_client, since
application_directory() reads the platform from argv when
STATUSLINE_PLATFORM is unset; and run as a subprocess against an isolated
home with no live server, it must survive empty/null/malformed/wrong-typed
payloads by printing a non-empty fallback line and exiting 0 -- the same
contract verify_client_fallback.py pins for statusline_client.py directly,
exercised here through the installed literal file a harness actually
invokes.

Qwen's own adapter (statusline_lib/qwen.py, wrong-typed fields, degenerate
payloads, model summaries) is covered end to end by scripts/verify_qwen_-
adapter.py, which calls render_qwen_statusline directly. This file's
subprocess never reaches it, since nothing here ever starts a real
server.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, REPO)

from _client_environment import isolated_client_environment

from scripts._server_wait_helpers import server_json_appeared
from statusline_lib.server_socket import SPAWN_LOCK_FILENAME

_ENCODING = "utf-8"
_SPAWN_LOCK_PREFERENCE = "STATUSLINE_SPAWN_LOCK_STALE_SECONDS"
_HELD_SPAWN_LOCK_SECONDS = "3600"

# Mirrors statusline_client._PLATFORM_APP_DIR_PARTS["qwen"]: the platform
# app_dir() resolves to under a HOME this suite controls.
_QWEN_APP_DIR_PARTS = (".qwen",)


def _state_dir(tmp_home):
    """The isolated home's qwen state directory, per
    statusline_client._PLATFORM_APP_DIR_PARTS["qwen"]. Shared by
    _hold_spawn_lock and the post-run server.json absence check below, so a
    drift between this mirror and the client's own resolution shows up as a
    failure rather than a silent leak."""
    return os.path.join(tmp_home, *_QWEN_APP_DIR_PARTS, "state")


def _hold_spawn_lock(tmp_home):
    """A fresh single-flight spawn lock in the isolated home's qwen state
    directory, so a subprocess that finds no live server prints its
    fallback and does not start a real server behind this suite's back."""
    state_dir = _state_dir(tmp_home)
    os.makedirs(state_dir, exist_ok=True)
    with open(
        os.path.join(state_dir, SPAWN_LOCK_FILENAME), "w", encoding=_ENCODING
    ) as f:
        json.dump({"pid": os.getpid(), "at": time.time()}, f)


def _run_qwen(failures, payload_raw, tmp_home):
    env = isolated_client_environment(
        tmp_home,
        encoding=_ENCODING,
        **{_SPAWN_LOCK_PREFERENCE: _HELD_SPAWN_LOCK_SECONDS},
    )
    _hold_spawn_lock(tmp_home)
    result = subprocess.run(
        [sys.executable, os.path.join(REPO, "qwen_statusline.py")],
        input=payload_raw,
        capture_output=True,
        text=True,
        encoding=_ENCODING,
        env=env,
        timeout=30,
        check=False,
    )
    server_info_path = os.path.join(_state_dir(tmp_home), "server.json")
    if server_json_appeared(server_info_path):
        failures.append(
            f"qwen_statusline.py should not have spawned a real server "
            f"(the spawn lock should have blocked it): {server_info_path} appeared"
        )
    return result


def _expect_non_blank(failures, label, result):
    if result.returncode != 0:
        failures.append(
            f"{label} must not crash, got exit {result.returncode}: {result.stderr!r}"
        )
        return
    if not result.stdout.strip():
        failures.append(f"{label} must never print a blank fallback line")


# The injection statement itself, not just any mention of the flag: a plain
# substring search for "--statusline-platform" also matches this file's own
# docstring, which would make the ordering check below vacuous (it always
# passes, since the docstring comes first, regardless of whether the real
# injection code exists at all below it).
_INJECTION_MARKER = 'sys.argv += ["--statusline-platform"'


def _check_platform_injected_before_client_import(failures):
    """The platform flag must land in sys.argv before statusline_client is
    imported: statusline_client's application_directory() reads it from
    sys.argv when STATUSLINE_PLATFORM is unset, so an import that ran first
    would resolve the wrong harness directory once inside the client."""
    with open(os.path.join(REPO, "qwen_statusline.py"), encoding=_ENCODING) as f:
        source = f.read()
    injection_index = source.find(_INJECTION_MARKER)
    import_index = source.find("import statusline_client")
    if injection_index < 0 or import_index < 0:
        failures.append(
            "qwen_statusline.py must both inject --statusline-platform and"
            " import statusline_client"
        )
        return
    if injection_index > import_index:
        failures.append(
            "qwen_statusline.py must inject --statusline-platform before"
            " importing statusline_client"
        )


def _check_empty_object(failures):
    with tempfile.TemporaryDirectory() as tmp:
        _expect_non_blank(
            failures, "empty {} payload with no server", _run_qwen(failures, "{}", tmp)
        )


def _check_null_top_level_payload(failures):
    """A literal JSON `null` payload is valid JSON, so it reaches the
    client's payload parsing rather than an exception."""
    with tempfile.TemporaryDirectory() as tmp:
        _expect_non_blank(
            failures,
            "null top-level payload with no server",
            _run_qwen(failures, "null", tmp),
        )


def _check_non_dict_top_level_payload(failures):
    """A JSON array at the top level is also valid JSON with no dict shape."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(failures, "[]", tmp)
    if result.returncode != 0:
        failures.append(
            f"list top-level payload must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )


def _check_malformed_json(failures):
    """Stdin that is not valid JSON at all: statusline_client's
    _payload_from_stdin() must degrade to an empty payload rather than
    raise."""
    with tempfile.TemporaryDirectory() as tmp:
        _expect_non_blank(
            failures,
            "malformed JSON with no server",
            _run_qwen(failures, "{ not json", tmp),
        )


def _check_wrong_typed_fields(failures):
    """Wrong-typed fields (the class of payload a render adapter is most
    likely to crash on) must not crash the client's minimal-line fallback
    either."""
    payload = json.dumps(
        {
            "model": {"display_name": 5},
            "context_window": {"context_window_size": "x", "current_usage": 5000},
            "metrics": {"models": [1, 2, 3]},
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(failures, payload, tmp)
    _expect_non_blank(failures, "wrong-typed payload with no server", result)


def _check_full_payload_falls_back_with_the_model_name(failures):
    """A well-formed payload still falls back (no server is running), and
    the fallback line's minimal_line() reads model.display_name the same
    way Qwen's real adapter does."""
    payload = json.dumps(
        {
            "model": {"display_name": "qwen3-coder"},
            "cwd": "C:/path/to/project",
            "context_window": {"context_window_size": 256_000, "current_usage": 12_000},
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(failures, payload, tmp)
    _expect_non_blank(failures, "full payload with no server", result)
    if "qwen3-coder" not in result.stdout:
        failures.append(
            f"the fallback line should still name the model, got {result.stdout!r}"
        )


def check(failures):
    _check_platform_injected_before_client_import(failures)
    _check_empty_object(failures)
    _check_null_top_level_payload(failures)
    _check_non_dict_top_level_payload(failures)
    _check_malformed_json(failures)
    _check_wrong_typed_fields(failures)
    _check_full_payload_falls_back_with_the_model_name(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: qwen_statusline.py injects its platform flag before importing"
        " statusline_client and falls back to a non-empty line for every"
        " degenerate payload when no server answers"
    )


if __name__ == "__main__":
    main()
