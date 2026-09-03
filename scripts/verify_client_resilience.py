"""Verify that nothing the statusline client reads can crash it.

The fallback suite next door proves the client prints the right line when the
server does not answer. This one proves it prints a line at all when its
inputs are hostile: a recorded render that is not UTF-8, a payload whose
fields are the wrong type, a socket that cannot be constructed, and the two
different exceptions the two platforms raise for a dead port.

The client is entry-point glue, so a traceback out of it is not a stack trace
in a log, it is the harness's status line. Every check here therefore asserts
on exit code and on a clean stderr as well as on stdout.

Fixtures come from scripts/verify_client_fallback.py, which is what installs
the temporary HOME and CLAUDE_STATE_DIR the whole server family is isolated
by. Split from that file rather than appended to it because the repository
holds every file at or under 400 lines.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import os
import sys

# The scripts directory, so the fallback suite is importable. That import is
# what installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen
# before any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import _silent_server, _write_last_render
from verify_server_protocol import _run_client
from verify_server_requests import _SESSION_ID, _claude_payload

# Only now the repository root, for the writer side of the last-render path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.server import last_render_path

# Bytes no UTF-8 decoder accepts. Written straight to the last-render path, so
# the client's strict decode raises UnicodeDecodeError on the read.
_NOT_UTF8 = b"\xff\xfe render \x80\x81 bytes"

# A port number is required by request_render's guard but never reached: every
# socket injected below fails before or during the exchange.
_UNUSED_PORT = 1


class _RaisingSocket:
    """A socket that gets all the way to the receive and then raises. Enough
    of the interface for request_render, and nothing more."""

    def __init__(self, exception):
        self._exception = exception

    def settimeout(self, timeout):
        del timeout

    def connect(self, address):
        del address

    def send(self, data):
        """Accepts the request and drops it; request_render ignores the count
        a real socket returns here."""
        del data

    def recv(self, size):
        del size
        raise self._exception

    def close(self):
        """Nothing to release; request_render still closes in its finally."""


def _raising_socket_factory(exception):
    """A _socket_factory handing back a socket whose receive raises
    `exception`. Built here rather than as a lambda in a loop, so the
    exception is bound by argument instead of by closure."""

    def factory(*arguments, **keywords):
        del arguments, keywords
        return _RaisingSocket(exception)

    return factory


@contextlib.contextmanager
def _injected_socket_factory(factory):
    """The client's _socket_factory seam, restored on the way out. Yields the
    client module, since every caller needs it anyway."""
    import statusline_client

    saved_factory = statusline_client._socket_factory
    statusline_client._socket_factory = factory
    try:
        yield statusline_client
    finally:
        statusline_client._socket_factory = saved_factory


def _write_bytes_to_last_render(context):
    """Overwrite the recorded render with bytes that are not UTF-8, keeping
    the mtime fresh so the age gate lets the read happen."""
    _write_last_render(context, "placeholder")
    with open(last_render_path(_SESSION_ID, context.state_directory), "wb") as f:
        f.write(_NOT_UTF8)


def check_hostile_input_never_reaches_the_harness_as_a_traceback(failures):
    """Three inputs the client does not control and cannot validate away: a
    recorded render that is not UTF-8, a payload whose workspace is a string
    where a mapping belongs, and a payload whose cwd is a number. Each must
    print a line, exit 0, and leave stderr clean."""
    for label, prepare, payload in (
        ("a last-render file that is not UTF-8", _write_bytes_to_last_render, None),
        (
            "a string workspace",
            None,
            {"workspace": "not-a-mapping", "model": "a-model"},
        ),
        ("a numeric cwd", None, {"cwd": 3, "model": "a-model"}),
    ):
        with _silent_server() as context:
            if prepare is not None:
                prepare(context)
            result = _run_client(context, "claude", payload or _claude_payload())
        if not result.stdout.strip():
            failures.append(f"{label} must still produce a line")
        if result.returncode != 0:
            failures.append(f"{label} must exit 0; got {result.returncode}")
        if "Traceback" in result.stderr:
            failures.append(f"{label} produced a traceback: {result.stderr!r}")


def check_the_fallback_path_never_raises(failures):
    """The last-resort guard itself, which no realistic input reaches.

    Forced two ways: with the recorded-file read raising, the minimal line
    still comes back; with the minimal line raising as well, the client
    returns an empty string rather than propagating. Both are asserted in
    process, because a bug that only appears when the guard is removed cannot
    be provoked through stdin.
    """
    import statusline_client
    import statusline_client_support

    def explode(*arguments, **keywords):
        del arguments, keywords
        raise RuntimeError("a shape nobody anticipated")

    saved_recent = statusline_client_support.recent_last_render
    saved_minimal = statusline_client_support.minimal_line
    statusline_client_support.recent_last_render = explode
    try:
        text = statusline_client.fallback_text("claude", _claude_payload())
        if not text.strip():
            failures.append(f"a raising file read must still print a line: {text!r}")
        statusline_client_support.minimal_line = explode
        text = statusline_client.fallback_text("claude", _claude_payload())
        if text != "":
            failures.append(f"a wholly failed fallback must print nothing: {text!r}")
    finally:
        statusline_client_support.recent_last_render = saved_recent
        statusline_client_support.minimal_line = saved_minimal


def check_a_socket_constructor_failure_is_not_a_traceback(failures):
    """socket() itself can raise, on a host out of descriptors or with UDP
    locked down. Constructed outside request_render's guard, that escaped the
    client as a traceback and a blank statusline instead of a fallback."""

    def exploding_socket(*arguments, **keywords):
        del arguments, keywords
        raise OSError("no socket available")

    with _injected_socket_factory(exploding_socket) as client:
        reply, saw_reset = client.request_render("claude", {}, {"port": _UNUSED_PORT})
    if reply is not None:
        failures.append(f"a socket constructor failure must not answer: {reply!r}")
    if saw_reset:
        failures.append("a socket constructor failure is not a dead-port signal")


def check_a_dead_port_is_reported_on_both_platforms(failures):
    """A connected UDP socket reports ICMP port-unreachable as
    ConnectionResetError on Windows and ConnectionRefusedError on Linux. Both
    mean the port is dead rather than slow, and Task 16 spawns a replacement
    on exactly that signal, so both have to set it. Injected rather than
    provoked, so each platform's shape is covered on the other one too."""
    for exception_type in (ConnectionResetError, ConnectionRefusedError):
        factory = _raising_socket_factory(exception_type("the port is dead"))
        with _injected_socket_factory(factory) as client:
            reply, saw_reset = client.request_render(
                "claude", {}, {"port": _UNUSED_PORT}
            )
        name = exception_type.__name__
        if reply is not None:
            failures.append(f"{name} must not produce a reply: {reply!r}")
        if not saw_reset:
            failures.append(f"{name} must be reported as a dead port")


def check_a_plain_timeout_is_not_a_dead_port(failures):
    """The counterpart: a server that is merely busy times out, and that must
    not be mistaken for a dead port, or every slow render would spawn a second
    server."""
    factory = _raising_socket_factory(TimeoutError("nobody answered"))
    with _injected_socket_factory(factory) as client:
        reply, saw_reset = client.request_render("claude", {}, {"port": _UNUSED_PORT})
    if reply is not None:
        failures.append(f"a timeout must not produce a reply: {reply!r}")
    if saw_reset:
        failures.append("a timeout must not be reported as a dead port")


def check(failures):
    check_hostile_input_never_reaches_the_harness_as_a_traceback(failures)
    check_the_fallback_path_never_raises(failures)
    check_a_socket_constructor_failure_is_not_a_traceback(failures)
    check_a_dead_port_is_reported_on_both_platforms(failures)
    check_a_plain_timeout_is_not_a_dead_port(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: client resilience verified")


if __name__ == "__main__":
    main()
