"""The statusline client: one datagram, one reply, print it, exit.

Every installed statusline command is a wrapper around this file. It reads the
harness payload from stdin, sends it to the resident server named in
state_dir()/server.json, waits at most CLIENT_TIMEOUT_SECONDS for one reply,
and prints that reply verbatim. When no reply arrives it prints a fallback
line instead: the render the server recorded last, if that is still recent,
and otherwise a minimal line assembled from the payload alone. A server that
is missing, dead, or running code this checkout has moved past is replaced by
a spawn single-flighted across every racing client and never waited for.

Standard library only, and deliberately no imports from statusline_lib on the
hot path: importing the package costs more than everything this file does. The
duplication of app_dir(), state_dir() and (in the support module)
code_version() is the price, covered by verify_server_protocol.py and
verify_client_spawn.py against the package's own resolution and digest.

Do not rename this file: install.py writes its literal path into the SessionStart
hook, and the four entry wrappers import it by name.
"""

import json
import os
import socket
import sys

# Force UTF-8 stdout regardless of the Windows console code page, the same way
# statusline.py does: a reply carries glyphs like the beacon's stopwatch, and
# on a cp1252 stdout writing one raises UnicodeEncodeError. errors="replace"
# degrades a future non-encodable glyph to "?" rather than losing the line.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# How long a render waits for the server before giving up on it. The whole
# point of the resident server is that a reply arrives in single-digit
# milliseconds, so this is a ceiling on the damage a wedged one can do, not a
# budget anything is expected to use.
CLIENT_TIMEOUT_SECONDS = 0.150

# UDP's own ceiling, so no reply is ever truncated by a buffer chosen too
# small. Mirrors server_socket.MAXIMUM_DATAGRAM_BYTES.
MAXIMUM_DATAGRAM_BYTES = 65535

# Mirrors server_info.SERVER_INFO_FILENAME.
SERVER_INFO_FILENAME = "server.json"

# How old the server's recorded last render may be and still be worth
# printing. A render a few seconds old is the truth slightly late, which beats
# anything this process could compute; a render half a minute old is a claim
# about a session that has moved on, which is worse than saying less.
FALLBACK_MAXIMUM_AGE_SECONDS = 30.0

# How old the single-flight spawn lock may be before a client ignores it: far
# longer than a spawn takes, far shorter than a user notices.
SPAWN_LOCK_STALE_SECONDS = 10.0

# This checkout, as the client sees it. The version digest and the server this
# client spawns are both resolved from here, so a client never starts a server
# out of a checkout other than the one it is running from.
_REPOSITORY_ROOT = os.path.dirname(os.path.abspath(__file__))

# What --ensure-server probes with: the status kind renders nothing.
_LIVENESS_KIND = "status"

_LOOPBACK_ADDRESS = "127.0.0.1"
_WIRE_ENCODING = "utf-8"
_PREFS_FILENAME = ".statusline-prefs.json"
_TIMEOUT_PREFERENCE = "STATUSLINE_CLIENT_TIMEOUT_MS"
_FALLBACK_AGE_PREFERENCE = "STATUSLINE_FALLBACK_MAXIMUM_AGE_SECONDS"
_SPAWN_LOCK_PREFERENCE = "STATUSLINE_SPAWN_LOCK_STALE_SECONDS"
_ERROR_LOG_FILENAME = ".statusline-error.log"

# Mirrors base._PLATFORM_APP_DIR_PARTS: resolved platform name -> the path
# components of that harness's configuration directory under ~.
_PLATFORM_APP_DIR_PARTS = {
    "antigravity": (".gemini", "antigravity-cli"),
    "qwen": (".qwen",),
    "kimi": (".kimi-code",),
    "claude": (".claude",),
}

# The socket constructor, as a module-level seam: a test injects one that
# raises, so both platforms' dead-port shapes and a failing socket() itself
# can be exercised anywhere.
_socket_factory = socket.socket


def _platform_from_argv():
    """The `--statusline-platform <value>` (or `=`-joined) argv value, or None.
    Reads sys.argv rather than main()'s argv, because base._platform_from_argv
    does and the two have to agree for every wrapper and every spawned child."""
    argv = sys.argv
    for index, argument in enumerate(argv):
        if argument == "--statusline-platform" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--statusline-platform="):
            return argument.split("=", 1)[1]
    return None


def platform_name():
    """The resolved platform name, or None. Environment wins over argv, the
    same precedence base.platform_name() uses."""
    return os.environ.get("STATUSLINE_PLATFORM") or _platform_from_argv()


def application_directory():
    """This client's copy of base.app_dir(): the harness configuration
    directory, defaulting to ~/.claude. Kept identical line for line, because a
    directory the two disagree about is a server.json the client never finds."""
    platform = platform_name()
    if platform in _PLATFORM_APP_DIR_PARTS:
        return os.path.join(os.path.expanduser("~"), *_PLATFORM_APP_DIR_PARTS[platform])

    if os.environ.get("ANTIGRAVITY_AGENT") == "1" or os.environ.get(
        "ANTIGRAVITY_CONVERSATION_ID"
    ):
        home = os.path.expanduser("~")
        # Test isolation check: if home is mocked in verify tests,
        # .gemini/antigravity-cli won't exist but .claude might.
        if not os.path.exists(
            os.path.join(home, ".gemini", "antigravity-cli")
        ) and os.path.exists(os.path.join(home, ".claude")):
            return os.path.join(home, ".claude")
        return os.path.join(home, ".gemini", "antigravity-cli")
    return os.path.join(os.path.expanduser("~"), ".claude")


def state_directory():
    """This client's copy of base.state_dir(): CLAUDE_STATE_DIR, then
    ANTIGRAVITY_STATE_DIR, then application_directory()/state."""
    return (
        os.environ.get("CLAUDE_STATE_DIR")
        or os.environ.get("ANTIGRAVITY_STATE_DIR")
        or os.path.join(application_directory(), "state")
    )


def server_info_path():
    """Where the running server publishes its port. This client's copy of
    server_info.server_info_path()."""
    return os.path.join(state_directory(), SERVER_INFO_FILENAME)


def read_server_info(path):
    """The server info dict at `path`, or None if the file is missing,
    unreadable, or does not parse as a JSON object. This client's copy of
    server_info.read_server_info()."""
    try:
        with open(path, encoding=_WIRE_ENCODING) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _preference(name):
    """One STATUSLINE_* setting: the prefs file, then the environment, then
    None. This client's copy of prefs.pref(), so a test can retune the client
    without a restart and without importing the package."""
    path = os.environ.get("STATUSLINE_PREFS_PATH") or os.path.join(
        application_directory(), _PREFS_FILENAME
    )
    try:
        with open(path, encoding=_WIRE_ENCODING) as f:
            preferences = json.load(f)
    except (OSError, ValueError):
        preferences = {}
    if isinstance(preferences, dict) and preferences.get(name) is not None:
        return str(preferences[name])
    return os.environ.get(name)


def _positive_preference(name):
    """One numeric STATUSLINE_* setting as a positive float, or None when it
    is unset, unparseable, or not positive. This client's copy of the guard
    server_socket.pref_number() applies to the server's own constants."""
    try:
        value = float(_preference(name))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _client_timeout_seconds():
    """The receive timeout, in seconds. Unset, unparseable and non-positive
    all fall back to the constant; a positive number of milliseconds wins."""
    milliseconds = _positive_preference(_TIMEOUT_PREFERENCE)
    return CLIENT_TIMEOUT_SECONDS if milliseconds is None else milliseconds / 1000.0


def _fallback_maximum_age_seconds():
    """How old the recorded last render may be and still be printed, in
    seconds. Overridable so a test can age a file out without waiting."""
    return (
        _positive_preference(_FALLBACK_AGE_PREFERENCE) or FALLBACK_MAXIMUM_AGE_SECONDS
    )


def _spawn_lock_stale_seconds():
    """How old the spawn lock may be, in seconds. Overridable so a test can
    exercise both arms without waiting out the real window."""
    return _positive_preference(_SPAWN_LOCK_PREFERENCE) or SPAWN_LOCK_STALE_SECONDS


def _support():
    """statusline_client_support, imported here rather than at module level.
    Every render loads it for the code-version comparison that decides whether
    to ask the server at all; what stays cold is the code inside it, since the
    fallback line and the spawn are only reached once a server has failed.
    One function, so the import is named in one place."""
    import statusline_client_support

    return statusline_client_support


def last_render_path(session_id):
    """This client's copy of server_render.last_render_path(), resolved
    through this client's own state_directory()."""
    return _support().last_render_path(session_id, state_directory())


def fallback_text(kind, payload):
    """The line to print when the server did not answer: the render it
    recorded last if that is still recent, and otherwise a minimal line built
    from the payload alone. Total by construction, so no shape of payload or
    of recorded file can reach the harness as a traceback."""
    return _support().fallback_text(
        kind, payload, state_directory(), _fallback_maximum_age_seconds()
    )


def spawn_lock_path():
    """This client's single-flight spawn lock, next to server.json."""
    return _support().spawn_lock_path(state_directory())


def claim_spawn_lock():
    """True when this process is the one that may spawn the server."""
    return _support().claim_spawn_lock(spawn_lock_path(), _spawn_lock_stale_seconds())


def classify_failure(info, *, reset):
    """Why the server cannot serve this render: "missing", "dead", "version"
    or "silent", of which only "silent" is not worth a spawn."""
    return _support().classify_failure(
        info, reset=reset, repository_root=_REPOSITORY_ROOT
    )


def ensure_server(info, reason):
    """Spawn a replacement server, at most one across every racing client, and
    never wait for it: the render has already been printed. `info` is the
    snapshot this render read, and the support module re-reads server.json
    through this client's own reader once it holds the spawn lock."""
    return _support().ensure_server(
        info,
        reason,
        lock_path=spawn_lock_path(),
        stale_seconds=_spawn_lock_stale_seconds(),
        repository_root=_REPOSITORY_ROOT,
        platform=platform_name(),
        error_log_path=os.path.join(application_directory(), _ERROR_LOG_FILENAME),
        info_path=server_info_path(),
        read_info=read_server_info,
    )


def request_render(kind, payload, info):
    """Send one datagram, wait for one reply. Returns (reply, saw_reset).

    A dead port announces itself through ICMP port-unreachable: the send
    succeeds, the rejection comes back, and the NEXT receive on the connected
    socket raises, as ConnectionResetError on Windows and
    ConnectionRefusedError on Linux. Both are siblings under ConnectionError,
    which is what this catches, and both mean the port is dead rather than
    slow, the one signal that justifies spawning a replacement. An ordinary
    timeout does not, since a server that is merely busy looks the same.

    Constructing the socket is inside the guard, not above it: socket() itself
    raises on a host out of descriptors or with UDP locked down, and that is a
    server this client could not reach, not a crash the user should see.
    """
    port = info.get("port")
    if not isinstance(port, int):
        return None, False
    try:
        columns = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        columns = 0
    # The harness sets COLUMNS on this client, but the render happens in the
    # server, which never sees this environment: compact.py's auto-shrink only
    # works if the width travels with the request.
    request = {"kind": kind, "payload": payload, "version": info.get("version")}
    request["columns"] = columns if columns > 0 else None
    client_socket = None
    try:
        client_socket = _socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        client_socket.settimeout(_client_timeout_seconds())
        # connect() before send(), so the kernel accepts a reply only from the
        # server's own address: an unconnected socket would take a datagram
        # from anything on the machine that guessed this ephemeral port.
        client_socket.connect((_LOOPBACK_ADDRESS, port))
        client_socket.send(json.dumps(request).encode(_WIRE_ENCODING))
        data = client_socket.recv(MAXIMUM_DATAGRAM_BYTES)
    except ConnectionError:
        # Before OSError, which it subclasses: a refused or reset connection
        # is the one failure that proves the port is dead rather than slow.
        return None, True
    except (OSError, ValueError):
        return None, False
    finally:
        if client_socket is not None:
            client_socket.close()
    return data.decode(_WIRE_ENCODING, "replace"), False


def _ask_server(kind, payload, info):
    """(reply, reason) for one exchange: the text to print or None, and why
    the server could not be used, "silent" when it answered or is merely busy.
    The version comparison happens before the datagram rather than after it,
    because a server running code the checkout has moved past would answer,
    and its answer is the wrong one."""
    reason = classify_failure(info, reset=False)
    if reason != "silent":
        return None, reason
    reply, saw_reset = request_render(kind, payload, info)
    if reply is not None:
        return reply, "silent"
    return None, classify_failure(info, reset=saw_reset)


def _kind_from_arguments(arguments, default_kind):
    """The `--kind <name>` (or `=`-joined) value, or `default_kind`. Parsed by
    hand rather than with argparse: the import is measurable next to a client
    whose whole job is one datagram."""
    for index, argument in enumerate(arguments):
        if argument == "--kind" and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith("--kind="):
            return argument.split("=", 1)[1]
    return default_kind


def _payload_from_stdin():
    """The harness payload as a dict. Anything that is not a JSON object --
    no stdin at all, bytes that are not UTF-8, a bare list -- is an empty
    payload rather than a crash. Read as bytes and decoded explicitly, so a
    Windows console code page cannot corrupt a session title on the way in."""
    try:
        raw = sys.stdin.buffer.read()
    except (AttributeError, OSError, ValueError):
        return {}
    try:
        payload = json.loads(raw.decode(_WIRE_ENCODING, "replace"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main(kind, argv=None):
    """One render: resolve the server, ask it, print what it says.

    `kind` is what the calling wrapper renders; `--kind` on the command line
    overrides it, which is what lets one file serve every harness and lets a
    test drive any kind. A missing or silent server costs the caller one
    timeout and a fallback line, and still exits 0: a statusline that fails
    loudly is worse than one that says a little less for a moment. The three
    flags below carry no render: --print-directories and --print-version exist
    for the duplication checks, and --ensure-server is the SessionStart hook's
    way of starting a server before the first render needs one.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    kind = _kind_from_arguments(arguments, kind)
    if "--print-directories" in arguments:
        # Exists only so the duplication check in verify_server_protocol.py
        # has something to compare the package's own resolution against.
        sys.stdout.write(f"{application_directory()}\n{state_directory()}\n")
        return 0

    if "--print-version" in arguments:
        # The same duplication check, for the other copied algorithm.
        sys.stdout.write(f"{_support().code_version(_REPOSITORY_ROOT)}\n")
        return 0

    info = read_server_info(server_info_path())
    if "--ensure-server" in arguments:
        # The SessionStart path: no stdin, no output, a live server behind it.
        _, reason = _ask_server(_LIVENESS_KIND, {}, info)
    else:
        payload = _payload_from_stdin()
        reply, reason = _ask_server(kind, payload, info)
        sys.stdout.write(reply if reply is not None else fallback_text(kind, payload))
    if reason != "silent":
        ensure_server(info, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main("claude"))
