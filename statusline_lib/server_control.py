"""Client side of the resident server's control protocol.

statusline-ctl's `server status`, `server stop` and `server restart`
subcommands are the only callers: request_status and request_shutdown send
the `status` and `shutdown` datagrams server_socket's wire format defines,
and wait_until_gone polls for a server's info file to disappear once a
shutdown has been sent. statusline_ctl.py stays argument parsing and
printing only; every line that talks to a socket or blocks on a poll lives
here instead.

Kept out of server_info.py, and out of statusline_lib/ generally as far as
scripts/verify_render_budget_static.py is concerned: that script scans
every file in this package (except the entries it names for a documented
reason, e.g. process_safe.py) for calls that could block a render, and bans
time.sleep outright. wait_until_gone's poll loop is exactly that kind of
call, but it is never reached by a render -- statusline_ctl.py is not part
of the render path -- so this module is named alongside process_safe.py in
that script's exclusion list, with the same shape of justification: the
code the scan would flag here is the fix, not the incident it exists to
catch.

Imports:
  server_socket -- for MAXIMUM_DATAGRAM_BYTES, the datagram size ceiling
                    shared with the server's own receive loop
"""

import contextlib
import json
import os
import socket
import time

from .server_socket import MAXIMUM_DATAGRAM_BYTES

_WIRE_ENCODING = "utf-8"


def _send(port, request, timeout):
    """Fire one JSON datagram at the resident server on `port` and forget
    it. Used for shutdown, which sends no reply for the caller to wait on;
    a send failure is swallowed, since the caller only cares whether the
    server eventually goes away."""
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
    ):
        sock.settimeout(timeout)
        sock.sendto(json.dumps(request).encode(_WIRE_ENCODING), ("127.0.0.1", port))


def _send_and_receive(port, request, timeout):
    """One request, one reply, or None on any failure: no server listening,
    the reply never arrived within `timeout`, or it came back as bytes that
    are not valid UTF-8. The caller decides what a missing reply means."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(json.dumps(request).encode(_WIRE_ENCODING), ("127.0.0.1", port))
            return sock.recvfrom(MAXIMUM_DATAGRAM_BYTES)[0].decode(_WIRE_ENCODING)
    except (OSError, UnicodeDecodeError):
        return None


def request_status(port, timeout=0.5):
    """Ask the resident server on `port` for its status summary. Returns the
    parsed JSON dict, or None when it did not answer in time or answered
    with something that is not valid JSON."""
    reply = _send_and_receive(port, {"kind": "status"}, timeout)
    if reply is None:
        return None
    try:
        return json.loads(reply)
    except ValueError:
        return None


def request_shutdown(port, timeout=0.5):
    """Ask the resident server on `port` to stop. Fire-and-forget: the
    server replies nothing to a shutdown, so the caller polls server.json
    (wait_until_gone) to learn whether it actually went away."""
    _send(port, {"kind": "shutdown"}, timeout)


def wait_until_gone(path, timeout=2.0, poll_interval=0.05):
    """Poll for `path` to stop existing, up to `timeout` seconds. Returns
    True once it disappears, False if it is still there when the deadline
    passes. A server that processed a shutdown datagram removes its info
    file within one receive-loop iteration, so the common case returns
    almost immediately; the deadline exists for the case where it doesn't."""
    deadline = time.monotonic() + timeout
    while os.path.exists(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
    return True
