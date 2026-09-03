"""Verify the wire protocol between statusline_client.py and the resident
server: a real server on a real port, the real client as a subprocess.

The client is entry-point glue (outside the coverage gate by AGENTS.md), so
this script is where its behavior is pinned. Nothing here asserts on elapsed
time; the client's own timeout is shortened through the prefs file where a
test needs it to expire.

The fixtures come from the three server suites next door rather than from a
second synthetic home of this file's own: importing scripts/verify_server_-
requests.py installs the temporary HOME and CLAUDE_STATE_DIR, and client
subprocesses run with an isolated environment pointing at that state.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import json
import os
import subprocess
import sys

# The scripts directory, so the server suites next door are importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _client_environment import isolated_client_environment
from verify_server_loop import _serving, _stop
from verify_server_requests import (
    _ENCODING,
    _HOME,
    _REPO,
    _STATE_DIR,
    _claude_payload,
    _kimi_payload,
    _qwen_payload,
    _subagent_payload,
)
from verify_server_socket import _socket_server

# Only now the repository root, and only now statusline_lib: importing the
# suites above is what installed the temporary HOME and CLAUDE_STATE_DIR, and
# several statusline_lib modules resolve app_dir()-based paths at import time,
# so the package must not be imported before that isolation is in place.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.base import app_dir, state_dir

_CLIENT = os.path.join(_REPO, "statusline_client.py")

# A ceiling rather than a measurement: a healthy client answers in well under
# a second, so this only elapses when the client hangs, and then the check
# fails instead of blocking the suite.
_HARD_TIMEOUT_SECONDS = 30

# The four harness platforms base.app_dir() knows by name.
_PLATFORMS = ("claude", "qwen", "kimi", "antigravity")

# The prefs key the client's receive timeout is retuned through.
_TIMEOUT_PREFERENCE = "STATUSLINE_CLIENT_TIMEOUT_MS"

# Every subprocess.run below decodes the same way the real client does: a
# rendered glyph must degrade to "?" on a cp1252 console rather than crash
# the check.
_DECODE_ERRORS = "replace"


class _Context:
    """What a check needs to talk to one running server: the server itself,
    the port it bound, the state directory it published server.json into, and
    the isolated environment a client subprocess uses to find it."""

    def __init__(self, server, port):
        self.server = server
        self.port = port
        self.state_directory = _STATE_DIR
        self.environment = _client_environment()


def _client_environment(**overrides):
    """The isolated environment used by every real client subprocess."""
    return isolated_client_environment(
        _HOME,
        state_directory=_STATE_DIR,
        encoding=_ENCODING,
        **overrides,
    )


def _fixed_render(text):
    """A stand-in renderer that always answers `text`, assigned onto one
    server instance so it only takes effect because dispatch is by name."""
    return lambda payload: text


@contextlib.contextmanager
def _running_server(failures, reply=None):
    """A bound server answering real datagrams on its own thread, shut down
    over the wire on the way out. `reply` replaces the claude render with a
    fixed answer, for the checks that are about the transport rather than
    about what a render produces."""
    server = _socket_server()
    if reply is not None:
        server._render_claude = _fixed_render(reply)
    port = server.bind()
    thread = _serving(server)
    try:
        yield _Context(server, port)
    finally:
        _stop(server, port, thread, failures)


def _run_with_payload(command, payload, environment):
    """Shared subprocess.run shape for a command fed `payload` on stdin: the
    real client directly, or one of the installed entry points wrapping it."""
    return subprocess.run(
        command,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding=_ENCODING,
        errors=_DECODE_ERRORS,
        timeout=_HARD_TIMEOUT_SECONDS,
        env=environment,
        check=False,
    )


def _run_client(context, kind, payload):
    """The real client as a subprocess, with the payload on stdin."""
    return _run_with_payload(
        [sys.executable, _CLIENT, "--kind", kind], payload, context.environment
    )


def _run_client_arguments(arguments, environment=None):
    """The real client as a subprocess with `arguments` and no stdin at all:
    the flag-only entry points (--print-directories, --print-version,
    --ensure-server) read nothing, so they are driven through here rather
    than through _run_client. Sibling suites import this rather than
    reaching for subprocess themselves."""
    return subprocess.run(
        [sys.executable, _CLIENT, *arguments],
        capture_output=True,
        text=True,
        encoding=_ENCODING,
        errors=_DECODE_ERRORS,
        timeout=_HARD_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        env=_client_environment() if environment is None else environment,
        check=False,
    )


def check_a_live_server_answers_the_client(failures):
    with _running_server(failures) as context:
        result = _run_client(context, "claude", _claude_payload())
    if result.returncode != 0:
        failures.append(f"client exited {result.returncode}: {result.stderr!r}")
    if not result.stdout.strip():
        failures.append("client printed nothing against a live server")
    elif "STATUSLINE ERROR" in result.stdout:
        failures.append(f"client printed an error line: {result.stdout!r}")


def check_the_client_prints_the_reply_verbatim(failures):
    """Whatever the server sends is what appears on stdout, byte for byte:
    the client is not allowed to reformat, wrap, or trim a reply."""
    with _running_server(failures, reply="line one\nline two") as context:
        result = _run_client(context, "claude", _claude_payload())
    if result.stdout != "line one\nline two":
        failures.append(f"reply was not printed verbatim: {result.stdout!r}")


def check_an_empty_reply_prints_nothing(failures):
    """A render that legitimately produced no text goes out as a zero-length
    datagram. That is a real answer, so the client prints nothing and exits
    successfully rather than treating it as a server that never replied."""
    with _running_server(failures, reply="") as context:
        result = _run_client(context, "claude", _claude_payload())
    if result.stdout != "":
        failures.append(f"an empty reply must print nothing: {result.stdout!r}")
    elif result.returncode != 0:
        failures.append(f"an empty reply must still exit 0, got {result.returncode}")


def check_the_subagent_kind_round_trips(failures):
    with _running_server(failures) as context:
        result = _run_client(context, "subagent", _subagent_payload())
    if not result.stdout.strip():
        failures.append("the subagent kind printed nothing against a live server")
    for line in result.stdout.splitlines():
        try:
            json.loads(line)
        except ValueError:
            failures.append(f"subagent output line is not JSON: {line!r}")


_ENTRY_POINTS = (
    ("statusline.py", _claude_payload),
    ("subagent_statusline.py", _subagent_payload),
    ("kimi_statusline.py", _kimi_payload),
    ("qwen_statusline.py", _qwen_payload),
)

# No fallback path can print this: fallback text is always either
# minimal_line()'s " | "-joined fields or a last-render file this fresh
# server never wrote, so a wrapper that fell back instead of reaching the
# server would print something else.
_LIVE_SERVER_MARKER = "LIVE-SERVER-MARKER-8f2c1e04"

_HANDLER_NAMES_UNDER_TEST = (
    "_render_claude",
    "_render_subagent",
    "_render_kimi",
    "_render_qwen",
)


def check_every_entry_point_reaches_a_live_server(failures):
    """statusline.py, subagent_statusline.py, kimi_statusline.py and
    qwen_statusline.py are each their own literal file a harness invokes
    directly; CLAUDE_STATE_DIR in context.environment routes all four to
    this suite's single server regardless of their own platform's app_dir.
    Matching the exact marker (not just a non-empty line) proves the
    wrapper reached the live server rather than silently falling back."""
    with _running_server(failures) as context:
        for handler_name in _HANDLER_NAMES_UNDER_TEST:
            setattr(context.server, handler_name, _fixed_render(_LIVE_SERVER_MARKER))
        for name, payload_factory in _ENTRY_POINTS:
            result = _run_with_payload(
                [sys.executable, os.path.join(_REPO, name)],
                payload_factory(),
                context.environment,
            )
            if result.returncode != 0:
                failures.append(
                    f"{name} exited {result.returncode} against a live server:"
                    f" {result.stderr!r}"
                )
            elif result.stdout != _LIVE_SERVER_MARKER:
                failures.append(
                    f"{name} did not print the live server's reply verbatim"
                    f" (fell back instead?): {result.stdout!r}"
                )


@contextlib.contextmanager
def _patched_environment(environment, arguments):
    """Resolve the package's own app_dir()/state_dir() under exactly the
    environment and argv a client subprocess was handed. base.app_dir() reads
    both, so both have to be installed for the comparison to mean anything."""
    saved_environment = dict(os.environ)
    saved_argv = list(sys.argv)
    os.environ.clear()
    os.environ.update(environment)
    sys.argv = [saved_argv[0], "--print-directories", *arguments]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_environment)
        sys.argv = saved_argv


def _directory_cases():
    """Every way the client can be pointed at a directory: the four platform
    values through STATUSLINE_PLATFORM, the same four through the argv flag,
    the two state-directory overrides, and the two Antigravity signals
    base.app_dir() falls back on when no platform was resolved at all."""
    base = _client_environment(CLAUDE_STATE_DIR=None)
    cases = []
    for platform in _PLATFORMS:
        cases.append(({**base, "STATUSLINE_PLATFORM": platform}, []))
        cases.append((dict(base), ["--statusline-platform", platform]))
    for name, value in (
        ("CLAUDE_STATE_DIR", os.path.join(_HOME, "claude-state")),
        ("ANTIGRAVITY_STATE_DIR", os.path.join(_HOME, "antigravity-state")),
        ("ANTIGRAVITY_AGENT", "1"),
        ("ANTIGRAVITY_CONVERSATION_ID", "conversation-0001"),
    ):
        cases.append(({**base, name: value}, []))
    return cases


def check_the_client_resolves_the_same_directories_as_the_package(failures):
    """The client carries its own copies of app_dir() and state_dir(). If the
    two ever drift, the client looks for server.json somewhere the server
    never writes it, and every render silently falls back forever."""
    for environment, arguments in _directory_cases():
        result = _run_client_arguments(
            ["--print-directories", *arguments], environment=environment
        )
        printed = result.stdout.strip().splitlines()
        with _patched_environment(environment, arguments):
            expected = [app_dir(), state_dir()]
        if printed != expected:
            failures.append(
                f"client resolved {printed}, the package resolves {expected}"
            )


def _timeout_for(preferences, environment_value):
    """Resolve one timeout case in a fresh client-module process."""
    path = os.path.join(_HOME, "client-timeout-prefs.json")
    if preferences is None:
        preferences_path = os.devnull
    else:
        with open(path, "w", encoding=_ENCODING) as preference_file:
            json.dump(preferences, preference_file)
        preferences_path = path
    environment = _client_environment(
        STATUSLINE_PREFS_PATH=preferences_path,
        **{_TIMEOUT_PREFERENCE: environment_value},
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import statusline_client; print(statusline_client._client_timeout_seconds())",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        encoding=_ENCODING,
        errors=_DECODE_ERRORS,
        timeout=_HARD_TIMEOUT_SECONDS,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"timeout probe failed: {result.stderr!r}")
    return float(result.stdout.strip())


def check_the_client_timeout_is_overridable(failures):
    """The default and preference precedence, resolved in fresh processes."""
    default = 0.150
    for preferences, environment_value, expected in (
        (None, None, default),
        (None, "50", 0.05),
        (None, "nonsense", default),
        (None, "0", default),
        ({_TIMEOUT_PREFERENCE: 25}, "50", 0.025),
        ({_TIMEOUT_PREFERENCE: None}, "50", 0.05),
    ):
        resolved = _timeout_for(preferences, environment_value)
        if resolved != expected:
            failures.append(
                f"prefs {preferences!r} plus environment {environment_value!r} "
                f"resolved {resolved}, expected {expected}"
            )
    if "statusline_client" in sys.modules:
        failures.append("the timeout check imported statusline_client in-process")


def check_the_client_environment_excludes_ambient_settings(failures):
    names = ("STATUSLINE_CLIENT_TIMEOUT_MS", "STATUSLINE_PLATFORM", "HTTPS_PROXY")
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "ambient-value-must-not-leak"
        environment = _client_environment()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    leaked = [name for name in names if name in environment]
    if leaked:
        failures.append(f"client environment inherited ambient settings: {leaked}")


def check(failures):
    check_the_client_environment_excludes_ambient_settings(failures)
    check_a_live_server_answers_the_client(failures)
    check_the_client_prints_the_reply_verbatim(failures)
    check_an_empty_reply_prints_nothing(failures)
    check_the_subagent_kind_round_trips(failures)
    check_every_entry_point_reaches_a_live_server(failures)
    check_the_client_resolves_the_same_directories_as_the_package(failures)
    check_the_client_timeout_is_overridable(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: client and server protocol verified")


if __name__ == "__main__":
    main()
