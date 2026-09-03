"""Verify the resident server's receive loop and its entry point: real UDP
datagrams in, rendered text out, and the two ways the loop ends.

The socket lifecycle either side of this loop (bind, close, server.json, the
prefs seams) is scripts/verify_server_socket.py, whose fixtures this file
imports; the request half underneath it, handle_request against a dict, is
scripts/verify_server_requests.py. Importing either of those is also what
installs the temporary HOME and CLAUDE_STATE_DIR this suite is isolated by,
so it has to happen before statusline_lib is touched anywhere.

A datagram whose content could unwind the loop is driven through a scripted
socket rather than the network, so its replies can be read directly.

Every server here is bound to 127.0.0.1 port 0 and answers real datagrams,
but nothing decides anything by reading the wall clock: the idle window is
driven by the injected clock or shrunk through its prefs override, and every
client receive carries a timeout so a server that never answers fails the
check instead of hanging it.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import socket
import sys
import threading

# The scripts directory, so the two suites next door are importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_server_requests import (
    _ENCODING,
    _ERROR_LOG,
    _STATE_DIR,
    _claude_payload,
    _FakeClock,
    _qwen_payload,
    _subagent_payload,
)
from verify_server_socket import _IDLE_PREF, _WORKERS_PREF, _socket_server

# Only now the repository root, and only now statusline_lib: the imports above
# are what installed the temporary HOME and CLAUDE_STATE_DIR, and several
# statusline_lib modules resolve app_dir()-based paths at import time, so the
# package must not be imported before that isolation is in place.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.server as server_module
import statusline_lib.server_entry as server_entry_module
from statusline_lib.base import app_dir
from statusline_lib.server import IDLE_EXIT_SECONDS, MAXIMUM_DATAGRAM_BYTES
from statusline_lib.server_entry import serve
from statusline_lib.server_info import read_server_info, server_info_path

# Generous, and a ceiling rather than a measurement: a healthy server replies
# in single-digit milliseconds, so this only elapses when the reply is never
# coming, and then the check fails instead of blocking the suite.
_REPLY_TIMEOUT_SECONDS = 5.0

# How long a check waits for serve_forever's thread to return. Same shape as
# above: the healthy case joins immediately.
_JOIN_TIMEOUT_SECONDS = 10.0


class _FlakySocket:
    """A receive socket that fails once with a plain OSError (not the
    departed-client ConnectionResetError WinError 10054 has become; that
    case is _DepartedClientResetSocket below), then goes quiet forever. The
    loop must log that and keep serving, then reach its idle exit."""

    def __init__(self, clock):
        self._clock = clock
        self.calls = 0

    def recvfrom(self, size):
        del size
        self.calls += 1
        if self.calls == 1:
            raise OSError("simulated transient receive error")
        self._clock.now += IDLE_EXIT_SECONDS + 1
        raise TimeoutError()

    def close(self):
        pass


class _DepartedClientResetSocket:
    """A receive socket that fails once the way Windows does when an earlier
    sendto drew an ICMP port unreachable from a client that has already gone
    (a UDP recvfrom raising ConnectionResetError, WinError 10054), then goes
    quiet forever. The loop must count that, not log it, and still reach its
    idle exit."""

    def __init__(self, clock):
        self._clock = clock
        self.calls = 0

    def recvfrom(self, size):
        del size
        self.calls += 1
        if self.calls == 1:
            raise ConnectionResetError(
                10054, "An existing connection was forcibly closed by the remote host"
            )
        self._clock.now += IDLE_EXIT_SECONDS + 1
        raise TimeoutError()

    def close(self):
        pass


class _BurstingErrorSocket:
    """A receive socket that raises three plain OSErrors at the same fake-clock
    time, advances the clock past the rate limit interval, raises a fourth plain
    OSError, then advances past the idle window and raises TimeoutError."""

    def __init__(self, clock):
        self._clock = clock
        self.calls = 0

    def recvfrom(self, size):
        del size
        self.calls += 1
        if self.calls <= 3:
            raise OSError(f"burst error {self.calls}")
        if self.calls == 4:
            self._clock.now += 61.0
            raise OSError("burst error 4")
        self._clock.now += IDLE_EXIT_SECONDS + 1
        raise TimeoutError()

    def close(self):
        pass


def _datagram(request):
    return json.dumps(request).encode(_ENCODING)


def _client_socket():
    """A throwaway UDP socket for one exchange, bound to nothing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(_REPLY_TIMEOUT_SECONDS)
    return sock


def _send_bytes(port, data):
    """Fire one datagram at the server and wait for nothing."""
    with _client_socket() as sock:
        sock.sendto(data, ("127.0.0.1", port))


def _send(port, request):
    _send_bytes(port, _datagram(request))


def _send_and_receive(port, request):
    """One request, one reply, or None when no reply arrived in time. An
    empty reply is a real answer and comes back as the empty string."""
    with _client_socket() as sock:
        sock.sendto(_datagram(request), ("127.0.0.1", port))
        try:
            return sock.recvfrom(MAXIMUM_DATAGRAM_BYTES)[0].decode(_ENCODING)
        except TimeoutError:
            return None


def _serving(server):
    """Start serve_forever on its own thread and hand back the thread."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server, port, thread, failures):
    """Shut a served server down over the wire and take its socket with it."""
    _send(port, {"kind": "shutdown"})
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    if thread.is_alive():
        failures.append("serve_forever did not return after a shutdown datagram")
    server.close()


def check_serve_forever_exits_when_idle(failures):
    """Driven with the fake clock: no request for the idle window ends the
    loop. Nothing here waits on the wall clock."""
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    server.bind()
    clock.now += IDLE_EXIT_SECONDS + 1
    server.serve_forever()
    server.close()
    if not server.stop_requested:
        failures.append("an idle server must stop itself")


def check_serve_forever_exits_on_shutdown(failures):
    server = _socket_server()
    port = server.bind()
    _stop(server, port, _serving(server), failures)


def check_a_render_kind_answers_over_the_wire(failures):
    """The whole point of the transport: a real payload in one datagram, the
    rendered line back in one datagram."""
    server = _socket_server()
    port = server.bind()
    thread = _serving(server)
    try:
        reply = _send_and_receive(
            port, {"kind": "claude", "payload": _claude_payload()}
        )
        if not reply:
            failures.append(
                f"the claude kind answered nothing over the wire: {reply!r}"
            )
    finally:
        _stop(server, port, thread, failures)


def check_an_empty_subagent_reply_is_a_zero_length_datagram(failures):
    """A subagent panel with no renderable rows renders the empty string.
    That is a real answer, so it goes on the wire as a zero-length datagram
    and the client prints nothing; the alternative, no reply at all, would
    cost the client its whole timeout for a render that succeeded."""
    server = _socket_server()
    port = server.bind()
    thread = _serving(server)
    try:
        payload = _subagent_payload()
        payload["tasks"] = []
        reply = _send_and_receive(port, {"kind": "subagent", "payload": payload})
        if reply is None:
            failures.append("an empty subagent render must still send a datagram")
        elif reply != "":
            failures.append(f"an empty subagent render must send no bytes: {reply!r}")
    finally:
        _stop(server, port, thread, failures)


def check_a_malformed_datagram_is_ignored(failures):
    """Bytes that are not JSON, not UTF-8, or not a JSON object must be
    dropped without a reply and without taking the loop down."""
    server = _socket_server()
    port = server.bind()
    thread = _serving(server)
    try:
        _send_bytes(port, b"not json at all")
        _send_bytes(port, b"\xff\xfe not utf-8 either")
        _send_bytes(port, b'"a bare string"')
        reply = _send_and_receive(port, {"kind": "status"})
        if not reply:
            failures.append("the loop stopped answering after a malformed datagram")
    finally:
        _stop(server, port, thread, failures)


def check_only_a_recognized_kind_refreshes_the_idle_timer(failures):
    """Garbage must not keep a server alive: an unrecognized kind is logged
    and dropped without touching the idle clock, so anything spraying nonsense
    at the port cannot hold the process open past its window."""
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    before = server._last_request_at
    clock.now += 300
    server.handle_request({"kind": "nonsense"})
    if server._last_request_at != before:
        failures.append("an unrecognized kind must not refresh the idle timer")
    server.handle_request({"kind": "status"})
    if server._last_request_at != clock.now:
        failures.append("a recognized kind must refresh the idle timer")
    server.close()


def check_a_transient_receive_error_is_logged_and_survived(failures):
    """One failing recvfrom must cost a log line, not the server."""
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    server.bind()
    real_socket = server._socket
    server._socket = _FlakySocket(clock)
    try:
        server.serve_forever()
    finally:
        server._socket = real_socket
        server.close()
    if not server.stop_requested:
        failures.append("the loop must still reach its idle exit after a bad receive")
    with open(_ERROR_LOG, encoding=_ENCODING) as f:
        if "simulated transient receive error" not in f.read():
            failures.append("a failing receive must log its traceback")


def check_a_departed_client_reset_is_counted_not_logged(failures):
    """A ConnectionResetError from recvfrom (WinError 10054, a departed
    client's ICMP port unreachable on the wire) must cost the loop a counter
    increment, not a traceback: it is the noisy-log bug this pair of checks
    guards against, so this one fails before the fix (a traceback appended)
    and passes after it (the log untouched, the counter at one)."""
    log_before = ""
    if os.path.exists(_ERROR_LOG):
        with open(_ERROR_LOG, encoding=_ENCODING) as f:
            log_before = f.read()
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    server.bind()
    real_socket = server._socket
    server._socket = _DepartedClientResetSocket(clock)
    try:
        server.serve_forever()
    finally:
        server._socket = real_socket
        server.close()
    if not server.stop_requested:
        failures.append(
            "the loop must still reach its idle exit after a departed reset"
        )
    if server._skipped_resets.count != 1:
        failures.append(
            f"a departed client's reset must be counted: {server._skipped_resets.count}"
        )
    log_after = ""
    if os.path.exists(_ERROR_LOG):
        with open(_ERROR_LOG, encoding=_ENCODING) as f:
            log_after = f.read()
    if log_after != log_before:
        failures.append(
            "a departed client's reset must not write to the error log: "
            f"{log_after[len(log_before) :]!r}"
        )


def check_bursting_receive_errors_are_rate_limited(failures):
    """Repeated transient receive errors within the rate-limit interval must
    only log the first traceback; after the interval, another traceback is logged."""
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    server.bind()
    real_socket = server._socket
    server._socket = _BurstingErrorSocket(clock)
    logged = []
    saved_log_traceback = server_module.log_traceback
    server_module.log_traceback = lambda path: logged.append(path)
    try:
        server.serve_forever()
    finally:
        server_module.log_traceback = saved_log_traceback
        server._socket = real_socket
        server.close()
    if not server.stop_requested:
        failures.append("the loop must still reach its idle exit after bursting errors")
    if len(logged) != 2:
        failures.append(
            f"bursting receive errors must log exactly 2 tracebacks, got {len(logged)}"
        )


def check_serve_runs_a_server_and_returns_zero(failures):
    """The entry point body end to end: resolve the directories, bind, sweep
    once, run the receive loop, tear everything down, return 0. The idle
    window is shrunk through its override so the loop ends on its first poll
    rather than in ten minutes; nothing here asserts on how long that took."""
    os.environ[_IDLE_PREF] = "0.001"
    os.environ[_WORKERS_PREF] = "1"
    try:
        code = serve([])
    finally:
        os.environ.pop(_IDLE_PREF, None)
        os.environ.pop(_WORKERS_PREF, None)
    if code != 0:
        failures.append(f"serve must return 0, got {code!r}")
    if read_server_info(server_info_path(_STATE_DIR)) is not None:
        failures.append("serve must remove server.json on its way out")


def _bind_that_fails(self):
    """Stand-in for Server.bind that fails the way a state directory nothing
    can write to would."""
    del self
    raise OSError("this bind was always going to fail")


def check_a_failing_bind_is_logged_and_returns_one(failures):
    """serve() runs as a detached spawn with nowhere to print, so a bind that
    raises has to leave a traceback in the error log and a non-zero exit code.
    An escaping traceback would go into a pipe nobody reads, and the client
    would keep spawning a server that keeps dying with no record anywhere."""
    log = os.path.join(app_dir(), ".statusline-error.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    saved = server_entry_module.Server.bind
    server_entry_module.Server.bind = _bind_that_fails
    try:
        code = serve([])
    finally:
        server_entry_module.Server.bind = saved
    if code != 1:
        failures.append(f"a failing bind must make serve return 1, got {code!r}")
    with open(log, encoding=_ENCODING) as f:
        if "this bind was always going to fail" not in f.read():
            failures.append("a failing bind must log its traceback")


def check_server_exposes_startup_housekeeping(failures):
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    sweeps = []
    server._housekeeper = lambda directory, now: sweeps.append((directory, now)) or 0
    server.maybe_housekeep()
    server.close()
    if sweeps != [(_STATE_DIR, clock.now)]:
        failures.append(f"public startup housekeeping must sweep once: {sweeps!r}")


def check_server_exposes_exception_logging(failures):
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    logged_paths = []
    saved_log_traceback = server_module.log_traceback
    server_module.log_traceback = lambda path: logged_paths.append(path)
    try:
        try:
            raise RuntimeError("synthetic exception")
        except RuntimeError:
            server.log_exception()
    finally:
        server_module.log_traceback = saved_log_traceback
        server.close()
    if logged_paths != [server._error_log_path]:
        failures.append(
            f"log_exception must log to server error log path: {logged_paths!r}"
        )


class _ScriptedSocket:
    """A receive socket that hands the loop a fixed list of datagrams, records
    every reply it is asked to send, and then advances the injected clock past
    the idle window so the loop ends on its own. No network, no wall clock."""

    def __init__(self, clock, datagrams):
        self._clock = clock
        self._datagrams = list(datagrams)
        self.sent = []

    def recvfrom(self, size):
        del size
        if self._datagrams:
            return self._datagrams.pop(0), ("127.0.0.1", 54321)
        self._clock.now += IDLE_EXIT_SECONDS + 1
        raise TimeoutError()

    def sendto(self, data, address):
        del address
        self.sent.append(data)


def _replies_after(failures, poison, description):
    """The datagrams serve_forever sends for `poison` followed by a status
    request. A poison datagram that unwinds the loop is the defect: the
    process ends, the next render finds a dead port, and the client spawns a
    replacement that dies on the same input."""
    clock = _FakeClock()
    server = _socket_server(clock=clock)
    server.bind()
    scripted = _ScriptedSocket(clock, [poison, _datagram({"kind": "status"})])
    real_socket, server._socket = server._socket, scripted
    try:
        server.serve_forever()
    except BaseException as error:
        failures.append(f"{description} ended the receive loop: {error!r}")
    finally:
        server._socket = real_socket
        server.close()
    return scripted.sent


def _answered_a_status_request(sent):
    """True when the last datagram sent is a status reply, which is how each
    check below asserts the server was still serving afterwards."""
    if not sent:
        return False
    try:
        return "pid" in json.loads(sent[-1].decode(_ENCODING))
    except ValueError:
        return False


def check_a_surrogate_bearing_reply_does_not_end_the_loop(failures):
    """A payload field carrying a lone surrogate survives JSON in both
    directions and lands in the rendered reply, where a strict encode raises
    UnicodeEncodeError. That is a ValueError, so the suppress(OSError) around
    the send does not catch it and it unwinds the whole loop. The reply is
    encoded with "replace" instead, so the datagram still goes out and the
    next request is still served."""
    payload = _qwen_payload()
    payload["model"] = {"display_name": "x\ud800y"}
    sent = _replies_after(
        failures, _datagram({"kind": "qwen", "payload": payload}), "a surrogate render"
    )
    if len(sent) != 2:
        failures.append(f"a surrogate-bearing render must still reply: {sent!r}")
    if not _answered_a_status_request(sent):
        failures.append("the loop stopped answering after a surrogate-bearing render")


def check_a_deeply_nested_datagram_does_not_end_the_loop(failures):
    """json's own recursion guard raises RecursionError, which is neither a
    UnicodeDecodeError nor a ValueError, so parse_request does not catch it
    either. Anything on localhost can send those bytes, so the loop body has
    to survive them and go on serving."""
    sent = _replies_after(
        failures, b"[" * 20000 + b"]" * 20000, "a deeply nested datagram"
    )
    if len(sent) != 1:
        failures.append(f"a deeply nested datagram must draw no reply: {sent!r}")
    if not _answered_a_status_request(sent):
        failures.append("the loop stopped answering after a deeply nested datagram")


def check(failures):
    check_server_exposes_startup_housekeeping(failures)
    check_server_exposes_exception_logging(failures)
    check_serve_forever_exits_when_idle(failures)
    check_serve_forever_exits_on_shutdown(failures)
    check_a_render_kind_answers_over_the_wire(failures)
    check_an_empty_subagent_reply_is_a_zero_length_datagram(failures)
    check_a_malformed_datagram_is_ignored(failures)
    check_only_a_recognized_kind_refreshes_the_idle_timer(failures)
    check_a_transient_receive_error_is_logged_and_survived(failures)
    check_bursting_receive_errors_are_rate_limited(failures)
    check_a_departed_client_reset_is_counted_not_logged(failures)
    check_serve_runs_a_server_and_returns_zero(failures)
    check_a_failing_bind_is_logged_and_returns_one(failures)
    check_a_surrogate_bearing_reply_does_not_end_the_loop(failures)
    check_a_deeply_nested_datagram_does_not_end_the_loop(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server receive loop verified")


if __name__ == "__main__":
    main()
