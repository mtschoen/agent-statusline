"""Verify interpreter-probe.sh: caching across renders, candidate execution
avoidance on cache hits, broken-py fallback, candidate change invalidation,
TTL expiry, and graceful degradation on corrupt/unwritable cache files.

Run from anywhere; imports / resolves from agent-statusline by path.
"""

import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from statusline_lib.process_safe import run_captured

_TEXT_ENCODING = "utf-8"
_MOCK_BIN_PREFIX = "statusline-mock-bin-"
_STATE_DIR_PREFIX = "statusline-state-"
_HOME_DIR_PREFIX = "statusline-home-"


def _run_probe(env, probe_path, bash_bin, extra_args=""):
    command = f'source "{probe_path}" {extra_args} && echo "$PY"'
    return run_captured(
        [bash_bin, "-c", command],
        timeout=10,
        env=env,
        check=False,
    )


def _check_broken_py_fallback(failures, bash_bin, probe_path):
    with tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir:
        mock_py = os.path.join(bin_dir, "py")
        with open(mock_py, "w", encoding=_TEXT_ENCODING) as f:
            f.write("#!/bin/sh\nexit 101\n")
        os.chmod(mock_py, 0o755)

        with tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir:
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            env["CLAUDE_STATE_DIR"] = state_dir

            result = _run_probe(env, probe_path, bash_bin)
            if result.returncode != 0:
                failures.append(
                    f"probe should succeed with broken py on PATH, "
                    f"got {result.returncode}: stderr={result.stderr!r}"
                )
            got_py = result.stdout.strip()
            if got_py == "py -3":
                failures.append("probe should not select 'py -3' when mock py fails")
            if not got_py:
                failures.append("probe should select a fallback interpreter")


def _check_working_py(failures, bash_bin, probe_path):
    with tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir:
        mock_py = os.path.join(bin_dir, "py")
        with open(mock_py, "w", encoding=_TEXT_ENCODING) as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(mock_py, 0o755)

        with tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir:
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            env["CLAUDE_STATE_DIR"] = state_dir
            result = _run_probe(env, probe_path, bash_bin)
            if result.returncode != 0 or result.stdout.strip() != "py -3":
                failures.append(
                    f"probe should select 'py -3' when py succeeds, "
                    f"got {result.stdout.strip()!r} (exit {result.returncode})"
                )


def _check_cache_hit_skips_execution(failures, bash_bin, probe_path):
    with tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir:
        mock_py = os.path.join(bin_dir, "py")
        call_log = os.path.join(bin_dir, "calls.log")
        with open(mock_py, "w", encoding=_TEXT_ENCODING) as f:
            f.write(f'#!/bin/sh\necho "call" >> "{call_log}"\nexit 0\n')
        os.chmod(mock_py, 0o755)

        with tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir:
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            env["CLAUDE_STATE_DIR"] = state_dir

            # Call 1: miss, executes mock py
            result1 = _run_probe(env, probe_path, bash_bin)
            if result1.returncode != 0 or result1.stdout.strip() != "py -3":
                failures.append(
                    f"first call should return 'py -3', got {result1.stdout.strip()!r}"
                )

            if not os.path.exists(call_log):
                failures.append("first call should execute candidate test")
                return
            with open(call_log, encoding=_TEXT_ENCODING) as f:
                call_count1 = len(f.read().splitlines())
            if call_count1 != 1:
                failures.append(
                    f"first call should execute candidate exactly once, got {call_count1}"
                )

            # Call 2: hit, skips execution test
            result2 = _run_probe(env, probe_path, bash_bin)
            if result2.returncode != 0 or result2.stdout.strip() != "py -3":
                failures.append(
                    f"second call should return 'py -3', got {result2.stdout.strip()!r}"
                )

            with open(call_log, encoding=_TEXT_ENCODING) as f:
                call_count2 = len(f.read().splitlines())
            if call_count2 != 1:
                failures.append(
                    f"second call should skip execution test via cache, got {call_count2} calls"
                )


def _check_cache_invalidation_on_candidate_change(failures, bash_bin, probe_path):
    with (
        tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir1,
        tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir2,
        tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir,
    ):
        call_log = os.path.join(state_dir, "calls.log")
        mock_py1 = os.path.join(bin_dir1, "py")
        with open(mock_py1, "w", encoding=_TEXT_ENCODING) as f:
            f.write(f'#!/bin/sh\necho "call1" >> "{call_log}"\nexit 0\n')
        os.chmod(mock_py1, 0o755)

        mock_py2 = os.path.join(bin_dir2, "py")
        with open(mock_py2, "w", encoding=_TEXT_ENCODING) as f:
            f.write(f'#!/bin/sh\necho "call2" >> "{call_log}"\nexit 0\n')
        os.chmod(mock_py2, 0o755)

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir1}{os.pathsep}{env.get('PATH', '')}"
        env["CLAUDE_STATE_DIR"] = state_dir

        _run_probe(env, probe_path, bash_bin)

        # Switch PATH to bin_dir2 (different candidate path)
        env["PATH"] = f"{bin_dir2}{os.pathsep}{env.get('PATH', '')}"
        _run_probe(env, probe_path, bash_bin)

        if not os.path.exists(call_log):
            failures.append("candidate execution log missing")
            return
        with open(call_log, encoding=_TEXT_ENCODING) as f:
            calls = f.read().splitlines()
        if calls != ["call1", "call2"]:
            failures.append(
                f"PATH change should invalidate cache and re-probe; got calls {calls!r}"
            )


def _check_cache_expiry(failures, bash_bin, probe_path):
    with (
        tempfile.TemporaryDirectory(prefix=_MOCK_BIN_PREFIX) as bin_dir,
        tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir,
    ):
        mock_py = os.path.join(bin_dir, "py")
        call_log = os.path.join(bin_dir, "calls.log")
        with open(mock_py, "w", encoding=_TEXT_ENCODING) as f:
            f.write(f'#!/bin/sh\necho "call" >> "{call_log}"\nexit 0\n')
        os.chmod(mock_py, 0o755)

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["CLAUDE_STATE_DIR"] = state_dir

        _run_probe(env, probe_path, bash_bin)

        # Backdate the cache timestamp by 4000 seconds (TTL is 3600)
        cache_file = os.path.join(state_dir, ".statusline-interpreter-cache")
        if not os.path.exists(cache_file):
            failures.append("cache file should exist after probe execution")
            return
        with open(cache_file, encoding=_TEXT_ENCODING) as f:
            lines = f.read().splitlines()
        if len(lines) >= 3:
            old_time = int(time.time()) - 4000
            with open(cache_file, "w", encoding=_TEXT_ENCODING) as f:
                f.write(f"{old_time}\n{lines[1]}\n{lines[2]}\n")

        # Third run should re-probe due to expired TTL
        _run_probe(env, probe_path, bash_bin)
        with open(call_log, encoding=_TEXT_ENCODING) as f:
            calls = f.read().splitlines()
        if len(calls) != 2:
            failures.append(
                f"expired cache should trigger re-probe; got {len(calls)} calls"
            )


def _check_corrupt_cache_degrades(failures, bash_bin, probe_path):
    with tempfile.TemporaryDirectory(prefix=_STATE_DIR_PREFIX) as state_dir:
        cache_file = os.path.join(state_dir, ".statusline-interpreter-cache")
        with open(cache_file, "w", encoding=_TEXT_ENCODING) as f:
            f.write("garbage\ncorrupted\n")

        env = dict(os.environ)
        env["CLAUDE_STATE_DIR"] = state_dir

        result = _run_probe(env, probe_path, bash_bin)
        if result.returncode != 0 or not result.stdout.strip():
            failures.append(
                f"corrupt cache should degrade gracefully; got code {result.returncode}, "
                f"stdout={result.stdout.strip()!r}"
            )


def _clean_test_environment(home_directory):
    environment = dict(os.environ)
    environment.pop("CLAUDE_STATE_DIR", None)
    environment.pop("ANTIGRAVITY_STATE_DIR", None)
    environment.pop("STATUSLINE_PLATFORM", None)
    environment.pop("ANTIGRAVITY_AGENT", None)
    environment.pop("ANTIGRAVITY_CONVERSATION_ID", None)
    environment["HOME"] = home_directory
    environment["USERPROFILE"] = home_directory
    return environment


def _check_platform_routing(failures, bash_bin, probe_path):
    cases = [
        (
            "antigravity-arg",
            "--statusline-platform antigravity",
            {},
            os.path.join(".gemini", "antigravity-cli"),
        ),
        ("qwen-arg", "--statusline-platform qwen", {}, ".qwen"),
        ("kimi-arg", "--statusline-platform kimi", {}, ".kimi-code"),
        ("claude-arg", "--statusline-platform claude", {}, ".claude"),
        (
            "antigravity-env",
            "",
            {"STATUSLINE_PLATFORM": "antigravity"},
            os.path.join(".gemini", "antigravity-cli"),
        ),
        ("qwen-env", "", {"STATUSLINE_PLATFORM": "qwen"}, ".qwen"),
        ("kimi-env", "", {"STATUSLINE_PLATFORM": "kimi"}, ".kimi-code"),
        ("claude-env", "", {"STATUSLINE_PLATFORM": "claude"}, ".claude"),
    ]
    for label, extra_args, env_override, relative_app_dir in cases:
        with tempfile.TemporaryDirectory(prefix=_HOME_DIR_PREFIX) as home_dir:
            env = _clean_test_environment(home_dir)
            env.update(env_override)
            result = _run_probe(env, probe_path, bash_bin, extra_args=extra_args)
            if result.returncode != 0 or not result.stdout.strip():
                failures.append(
                    f"probe should succeed for {label}, got exit {result.returncode}"
                )
            cache_file = os.path.join(
                home_dir, relative_app_dir, ".statusline-interpreter-cache"
            )
            if not os.path.exists(cache_file):
                failures.append(f"{label} should write cache to {cache_file}")


def _check_wrapper_routing(failures, bash_bin):
    wrapper_cases = [
        (
            "qwen wrapper",
            ["qwen-statusline-command.sh"],
            ".qwen",
            [".claude", ".gemini", ".kimi-code"],
        ),
        (
            "kimi wrapper",
            ["kimi-statusline-command.sh"],
            ".kimi-code",
            [".claude", ".gemini", ".qwen"],
        ),
        (
            "claude wrapper",
            ["statusline-command.sh"],
            ".claude",
            [".gemini", ".qwen", ".kimi-code"],
        ),
        (
            "antigravity wrapper",
            ["statusline-command.sh", "--statusline-platform", "antigravity"],
            os.path.join(".gemini", "antigravity-cli"),
            [".claude", ".qwen", ".kimi-code"],
        ),
    ]
    for label, script_command, relative_app_dir, forbidden_dirs in wrapper_cases:
        with tempfile.TemporaryDirectory(prefix=_HOME_DIR_PREFIX) as home_dir:
            env = _clean_test_environment(home_dir)
            full_command = [
                bash_bin,
                os.path.join(REPO, script_command[0]),
                *script_command[1:],
            ]
            result = run_captured(full_command, timeout=10, env=env, check=False)
            if result.returncode != 0:
                failures.append(
                    f"{label} should exit 0, got {result.returncode}: {result.stderr!r}"
                )
            if not result.stdout.strip():
                failures.append(f"{label} should produce non-empty stdout")
            cache_file = os.path.join(
                home_dir, relative_app_dir, ".statusline-interpreter-cache"
            )
            if not os.path.exists(cache_file):
                failures.append(f"{label} should write cache file to {cache_file}")
            for forbidden in forbidden_dirs:
                forbidden_path = os.path.join(home_dir, forbidden)
                if os.path.exists(forbidden_path):
                    failures.append(
                        f"{label} should not create foreign directory {forbidden_path}"
                    )


def check(failures):
    bash_bin = shutil.which("bash")
    if bash_bin is None:
        return
    probe_path = os.path.join(REPO, "interpreter-probe.sh")
    _check_broken_py_fallback(failures, bash_bin, probe_path)
    _check_working_py(failures, bash_bin, probe_path)
    _check_cache_hit_skips_execution(failures, bash_bin, probe_path)
    _check_cache_invalidation_on_candidate_change(failures, bash_bin, probe_path)
    _check_cache_expiry(failures, bash_bin, probe_path)
    _check_corrupt_cache_degrades(failures, bash_bin, probe_path)
    _check_platform_routing(failures, bash_bin, probe_path)
    _check_wrapper_routing(failures, bash_bin)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: interpreter-probe.sh caching, invalidation, TTL, and fallbacks all verified"
    )


if __name__ == "__main__":
    main()
