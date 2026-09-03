"""The resident server's transport: its constants, its socket, its wire format.

Everything here is about datagrams and knows nothing about the Server that
sends them, which is the point of the split: server.py stayed under the
repository's 400-line ceiling, and the parts that can be reasoned about
without a running server are testable without one.

`Server.bind`, `Server.serve_forever`, and `Server.close` live in server.py
and `serve` lives in server_entry.py; these are the pieces they are
assembled from.

Imports:
  base  -- state_dir, to locate the client's spawn lock
  prefs -- pref, behind the two overridable constants
"""

import contextlib
import ctypes
import json
import os
import socket

from .base import state_dir
from .prefs import pref

# WSAIoctl control code for SIO_UDP_CONNRESET (winsock2.h), passed FALSE to
# stop Windows surfacing a departed UDP client's ICMP port-unreachable as a
# WinError 10054 on the NEXT recvfrom. socket.ioctl only accepts SIO_RCVALL,
# SIO_KEEPALIVE_VALS and SIO_LOOPBACK_FAST_PATH, so this needs WSAIoctl
# directly via ctypes.
_SIO_UDP_CONNRESET = 0x9800000C

# The transport's four numbers. The idle window ends a server nobody renders
# against any more, since the client spawns a replacement on its next render
# and an idle process is pure cost. The poll interval is how the receive loop
# notices that without sleeping anywhere. The datagram size is UDP's own
# ceiling, so no request is ever truncated by a buffer chosen too small. The
# receive buffer is raised well above the default because a burst of
# concurrent renders arrives as a burst of datagrams, and one the kernel drops
# costs that client its reply.
IDLE_EXIT_SECONDS = 600
RECEIVE_POLL_SECONDS = 0.5
MAXIMUM_DATAGRAM_BYTES = 65535
RECEIVE_BUFFER_BYTES = 1 << 20

# The client single-flights server spawns behind this file in the state
# directory. The server it spawned is the one that knows the spawn finished.
SPAWN_LOCK_FILENAME = "server.spawn.lock"

_WIRE_ENCODING = "utf-8"


def pref_number(name, default, cast):
    """A positive numeric prefs override for one of the constants above,
    falling back to `default` when the key is unset, unparseable, or not
    positive. The seams exist so a test can shrink the server's idle window
    and its pool instead of waiting ten minutes or starting four threads."""
    try:
        value = cast(pref(name))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _disable_windows_connection_reset(sock):
    """Best-effort SIO_UDP_CONNRESET off, so a departed client's ICMP
    port-unreachable never reaches recvfrom as WinError 10054 in the first
    place. Purely an optimization: any failure here (no ctypes.windll on this
    interpreter, a non-zero WSAIoctl return) is swallowed, and
    is_departed_client_reset remains the receive loop's safety net."""
    with contextlib.suppress(AttributeError, OSError):
        flag = ctypes.c_ulong(0)
        bytes_returned = ctypes.c_ulong(0)
        ctypes.windll.ws2_32.WSAIoctl(
            ctypes.c_size_t(sock.fileno()),
            _SIO_UDP_CONNRESET,
            ctypes.byref(flag),
            ctypes.sizeof(flag),
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
            None,
        )


def open_datagram_socket():
    """A localhost UDP socket bound to a random port, returned with that port.

    SO_EXCLUSIVEADDRUSE on Windows: without it a second server can bind the
    same port and silently steal half the datagrams. The receive timeout is
    what lets the receive loop check its idle window without sleeping.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if os.name == "nt":
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        _disable_windows_connection_reset(sock)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(RECEIVE_POLL_SECONDS)
    return sock, sock.getsockname()[1]


def clear_spawn_lock(state_directory):
    """Remove the client's spawn lock, best effort. Called once the server it
    guarded is listening AND has published its server.json: this process is
    the evidence the spawn finished, and a lock nobody clears would stall the
    next client that needs to spawn.

    Publishing first is what makes the lock a real mutex. A client that finds
    the lock free reads server.json once and sends to the port it names
    without re-reading it, so a lock released ahead of the file is a window in
    which that read answers with the previous server's port, or with nothing,
    and the client spawns a second server onto a machine that holds one.
    """
    with contextlib.suppress(OSError):
        os.remove(os.path.join(state_dir(state_directory), SPAWN_LOCK_FILENAME))


def parse_request(data):
    """One received datagram as a request dict, or None when it is not one.

    Anything that is not UTF-8, not JSON, or not a JSON object is None and the
    caller drops it in silence. A port on localhost receives whatever anything
    on the machine cares to send it, and the only client that matters retries
    by rendering again a moment later.
    """
    try:
        request = json.loads(data.decode(_WIRE_ENCODING))
    except (UnicodeDecodeError, ValueError):
        return None
    return request if isinstance(request, dict) else None


def is_departed_client_reset(error):
    """True when `error` is the Windows ICMP-port-unreachable signature: an
    earlier reply's sendto reached a client whose port had already closed,
    surfaced on the NEXT recvfrom as WinError 10054 (ConnectionResetError).
    No data is lost on this error, only a reply nobody was waiting for
    anymore, so the receive loop skips the traceback rather than logging one
    per departed client. _disable_windows_connection_reset above stops most
    of these before they happen; this is the fallback for whatever it missed
    (an interpreter without ctypes.windll, a WSAIoctl call that failed)."""
    return isinstance(error, ConnectionResetError)


class DepartedClientResetCounter:
    """How many recvfrom failures the receive loop skipped as a departed
    client's UDP reset rather than logging. Kept here, not in server.py, so
    the loop's OSError branch stays a one-line classification instead of
    growing the file the repository holds at a 400-line ceiling; the count
    is what makes the skip observable, surfaced through the `status` request
    kind."""

    def __init__(self):
        self.count = 0

    def note(self, error):
        """Record `error` and return whether it was a departed-client reset,
        so the caller knows whether to still log it."""
        if not is_departed_client_reset(error):
            return False
        self.count += 1
        return True


RECEIVE_ERROR_LOG_INTERVAL_SECONDS = 60.0


class ReceiveErrorLogLimiter:
    """Allow the first receive traceback, then at most one per interval."""

    def __init__(self, clock, interval_seconds=RECEIVE_ERROR_LOG_INTERVAL_SECONDS):
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._last_logged_at = None

    def should_log(self):
        now = self._clock()
        if (
            self._last_logged_at is not None
            and now - self._last_logged_at < self._interval_seconds
        ):
            return False
        self._last_logged_at = now
        return True
