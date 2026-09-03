"""The statusline client's second half: the version digest it decides by, the
spawn it single-flights, and what it prints when the server did not answer.

Part of statusline_client.py for every rule that file states, split out only
because the repository holds every file at or under 400 lines, and imported
from the functions that reach it rather than at module level. Every render
reaches one: the code-version comparison that decides whether the running
server is worth asking happens before the datagram goes out.

Standard library only, with one exception inside _spawn: process_safe's
spawn_detached is the repository's only sanctioned way to start a process.
Duplicating sanitize_state_key, last_render_path, session_id_for and
code_version against statusline_lib is the price of a client that imports
nothing, and scripts/verify_client_fallback.py and verify_client_spawn.py pin
both halves against the package's originals. Every function takes what it
needs as an argument, because statusline_client.py already resolves every
directory and preference and two copies of that resolution is the drift that
leaves a client reading a file no server writes.
"""

import contextlib
import hashlib
import json
import os
import socket
import sys
import time

# Mirrors server_render._LAST_RENDER_PREFIX.
LAST_RENDER_PREFIX = "last-render-"

# The subagent panel is the one kind with no fallback line. It renders one
# JSON row per line, so no statusline belongs in it, and it arrives carrying
# the same session id as the Claude render that wrote the last-render file, so
# a shared lookup would paste that session's statusline into the panel.
KINDS_WITHOUT_A_FALLBACK_LINE = ("subagent",)

_ENCODING = "utf-8"


def sanitize_state_key(key):
    """This client's copy of base.sanitize_state_key(). A path component is
    never built from unsanitized input, and both ends have to sanitize
    identically or the client looks for a filename the server never wrote."""
    return "".join(c for c in str(key or "") if c.isalnum() or c in "-_")


def last_render_path(session_id, state_directory):
    """This client's copy of server_render.last_render_path(), resolved
    against the state directory the client itself resolved."""
    return os.path.join(
        state_directory, f"{LAST_RENDER_PREFIX}{sanitize_state_key(session_id)}.txt"
    )


def session_id_from(payload):
    """This client's copy of server_render.session_id_for(): Claude Code's
    session_id, Antigravity's conversation_id, Kimi's camelCase sessionId.
    All three spellings, because the file was named with whichever one the
    harness sent and a client that knew only one would fall back to a minimal
    line forever on the other two. "" when the payload carries none, which is
    Qwen's case."""
    return (
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("sessionId")
        or ""
    )


def recent_last_render(session_id, state_directory, maximum_age_seconds):
    """The render the server recorded for `session_id`, or "" when there is
    none, it cannot be read, it holds no text, or it has aged past
    `maximum_age_seconds`. A file stamped in the future is a clock that moved,
    so it is rejected too rather than trusted until it catches up. ValueError
    alongside OSError because the decode is strict: bytes that are not UTF-8
    raise UnicodeDecodeError, which is a ValueError and not an OSError, and
    whatever wrote such a file, it is not a render this client can print."""
    if not session_id:
        return ""
    path = last_render_path(session_id, state_directory)
    try:
        age = time.time() - os.path.getmtime(path)
        if abs(age) >= maximum_age_seconds:
            return ""
        with open(path, encoding=_ENCODING) as f:
            text = f.read()
    except (OSError, ValueError):
        return ""
    return text if text.strip() else ""


def fallback_text(kind, payload, state_directory, maximum_age_seconds):
    """What to print when the server did not answer, guarded.

    Every lookup below already degrades to an empty string, so reaching either
    handler means a shape of payload or of recorded file that nobody
    anticipated, which is not worth a traceback in the harness's status line.
    The minimal line is tried on its own, since the likelier of the two
    failures is reading the recorded file rather than formatting the payload,
    and an empty line is the floor. The kind check comes first so a guard can
    never route the subagent panel into a statusline.
    """
    if kind in KINDS_WITHOUT_A_FALLBACK_LINE:
        return ""
    try:
        return _recorded_or_minimal(kind, payload, state_directory, maximum_age_seconds)
    except Exception:
        return _minimal_line_or_nothing(payload)


def _minimal_line_or_nothing(payload):
    """The minimal line, or "" when even that raised. The last stop before the
    harness, so it catches everything rather than a named set."""
    try:
        return minimal_line(payload)
    except Exception:
        return ""


def _recorded_or_minimal(kind, payload, state_directory, maximum_age_seconds):
    """The unguarded choice between the two tiers. A recent last-render file
    is a real render at most a few seconds old, which beats anything this
    process could compute; past `maximum_age_seconds` it is worse than honest,
    so a minimal line built from the payload alone takes over. Both are
    single-line safe, which Kimi's TUI requires."""
    cached = recent_last_render(
        session_id_from(payload), state_directory, maximum_age_seconds
    )
    if not cached:
        return minimal_line(payload)
    return cached.splitlines()[0] if kind == "kimi" else cached


def _model_name(payload):
    """The model as the harness named it: model.display_name, then model.id,
    then the bare string Kimi sends under that same key."""
    model = payload.get("model")
    if isinstance(model, dict):
        return str(model.get("display_name") or model.get("id") or "")
    return str(model or "")


def _context_percent(payload):
    """Context window occupancy as a whole-number percentage, or "".
    current_usage is a mapping of token counts on Claude Code and Antigravity
    and a single number on Qwen, so both shapes are totalled the same way."""
    window = payload.get("context_window")
    if not isinstance(window, dict):
        return ""
    size = window.get("context_window_size")
    used = window.get("current_usage")
    if isinstance(used, dict):
        used = sum(value for value in used.values() if isinstance(value, (int, float)))
    if not isinstance(size, (int, float)) or not size:
        return ""
    if not isinstance(used, (int, float)):
        return ""
    return f"{round(100.0 * used / size)}%"


def minimal_line(payload):
    """Model, context percent and the current directory's basename, from the
    payload alone. No file reads, no subprocesses, no imports: this is the
    line that must be printable when everything else has failed. Every lookup
    degrades to an empty string, including one whose value is the wrong type
    entirely, since this payload came off a pipe from a harness whose version
    this client does not control: `workspace` may be a string and `cwd` a
    number. The directory is the field that survives the other two, and a
    payload carrying nothing usable at all (an unreadable stdin produces an
    empty one) still names the one this render happens in, so the line is
    never blank."""
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    directory = workspace.get("current_dir") or payload.get("cwd") or ""
    if not isinstance(directory, str):
        directory = ""
    fields = [
        _model_name(payload),
        _context_percent(payload),
        os.path.basename(directory.rstrip("/\\")) or directory,
    ]
    line = " | ".join(field for field in fields if field)
    current_directory = os.getcwd()
    return (
        line or os.path.basename(current_directory.rstrip("/\\")) or current_directory
    )


# --- Spawning a replacement server ------------------------------------------

# Mirrors server_socket.SPAWN_LOCK_FILENAME. The spawned server removes this
# file once it has bound, so the steady state is no lock at all.
SPAWN_LOCK_FILENAME = "server.spawn.lock"

# The entry point a client spawns, resolved against the client's own checkout.
SERVER_ENTRY_POINT = "statusline_server.py"

# Mirrors server_info._ROOT_ENTRY_POINTS. This module is on the list because
# it is part of the client; verify_client_spawn.py compares the two lists.
_ROOT_ENTRY_POINTS = (
    "statusline.py",
    "subagent_statusline.py",
    "qwen_statusline.py",
    "kimi_statusline.py",
    "statusline_client.py",
    "statusline_client_support.py",
    "statusline_server.py",
)

_LOOPBACK_ADDRESS = "127.0.0.1"

# The spawn seam: None means the real launcher, and a test assigns a recorder.
_spawner = None


def version_input_files(repository_root):
    """This client's copy of server_info.version_input_files(): sorted,
    repository-relative names of every file that feeds code_version. A name on
    one list and not the other is a digest that disagrees forever."""
    names = list(_ROOT_ENTRY_POINTS)
    library_directory = os.path.join(repository_root, "statusline_lib")
    for root, _directories, files in os.walk(library_directory):
        for file_name in files:
            if not file_name.endswith(".py"):
                continue
            relative = os.path.relpath(os.path.join(root, file_name), repository_root)
            names.append(relative.replace(os.sep, "/"))
    return sorted(names)


def code_version(repository_root):
    """This client's copy of server_info.code_version(), identical by
    construction: a digest that drifts means every render asks the server to
    shut down and spawns a replacement, forever. Stat metadata rather than file
    contents, because this runs on the client's path to a reply; mtimes are
    truncated to whole seconds because the two platforms disagree below that."""
    digest = hashlib.sha256()
    for name in version_input_files(repository_root):
        path = os.path.join(repository_root, name.replace("/", os.sep))
        try:
            stat_result = os.stat(path)
        except OSError:
            digest.update(f"{name}\0missing\n".encode())
            continue
        digest.update(
            f"{name}\0{stat_result.st_size}\0{int(stat_result.st_mtime)}\n".encode()
        )
    return digest.hexdigest()[:16]


def classify_failure(info, *, reset, repository_root):
    """Why the server cannot serve this render: "missing", "dead", "version" or
    "silent". Only "silent" does not justify a spawn: a server that took the
    datagram and did not answer in time is busy, not broken, and a second one is
    the pile-up this design exists to prevent. "dead" is a port that answered
    with an ICMP rejection, which is also the liveness check on the recorded
    pid, since the operating system releases the port when that process goes."""
    if info is None:
        return "missing"
    if reset:
        return "dead"
    if info.get("version") != code_version(repository_root):
        return "version"
    return "silent"


def spawn_lock_path(state_directory):
    """Where the single-flight spawn lock lives, next to server.json."""
    return os.path.join(state_directory, SPAWN_LOCK_FILENAME)


def _lock_is_stale(path, stale_seconds):
    """Whether an existing lock is old enough to ignore. One stamped in the
    future is a clock that moved, so it is stale too; one this process cannot
    stat counts as held, since removing an unseen lock starts two servers."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    return abs(age) >= stale_seconds


def claim_spawn_lock(path, stale_seconds):
    """True when this process is the one that may spawn the server. O_EXCL
    creation is the whole mechanism: every client that discovers the same dead
    server races to create one file, and exactly one wins. A lock whose writer
    died leaves the file behind, so one older than `stale_seconds` is replaced
    rather than obeyed. Every other failure to create it is read as "somebody
    else is on it": spawning nothing costs one fallback line."""
    payload = json.dumps({"pid": os.getpid(), "at": time.time()}).encode(_ENCODING)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _lock_is_stale(path, stale_seconds):
            return False
        with contextlib.suppress(OSError):
            os.remove(path)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    try:
        os.write(handle, payload)
    finally:
        os.close(handle)
    return True


def _send_shutdown(info):
    """Ask the server named in `info` to stop, without waiting: a shutdown
    draws no reply, and a failure here is a server already ruled out."""
    port = info.get("port")
    if not isinstance(port, int):
        return False
    request = json.dumps({"kind": "shutdown"}).encode(_ENCODING)
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control_socket,
    ):
        control_socket.sendto(request, (_LOOPBACK_ADDRESS, port))
        return True
    return False


def _log_spawn_failure(error_log_path, command, error):
    """One line in the client's error log, best effort. A client that cannot
    start a server and cannot say so falls back forever with no explanation."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(error_log_path), exist_ok=True)
        with open(error_log_path, "a", encoding=_ENCODING) as f:
            f.write(f"[{stamp}] statusline_client: spawn failed ({error}): {command}\n")


def _spawn(command, environment):
    """Start the server and return immediately. process_safe.spawn_detached is
    the repository's only sanctioned subprocess surface, imported inside this
    function because importing statusline_lib costs more than the render being
    rescued, and this is only reached when a server has to be started at all."""
    if _spawner is not None:
        return _spawner(command, environment)
    from statusline_lib.process_safe import spawn_detached

    return spawn_detached(command, env=environment)


def ensure_server(
    info,
    reason,
    *,
    lock_path,
    stale_seconds,
    repository_root,
    platform,
    error_log_path,
    info_path,
    read_info,
):
    """Spawn a replacement server, at most one across every racing client, and
    never wait for it. True when this process is the one that spawned it.

    A "version" server is asked to shut down first, so a fast-forward of the
    live checkout takes effect on the next render and a half-edited checkout
    crashes the new server rather than the running one. The STATUSLINE_PLATFORM
    pin is not optional: the spawned server's argv carries no
    --statusline-platform flag, so without it a Kimi or Qwen client would start
    a server writing its state where that harness never reads it.

    `info` is the snapshot the decision was made on: read_info(info_path) reads
    server.json again once the lock is held, since a client delayed between the
    two can wake to find another client's replacement already published, and a
    second server then is the pile-up the lock exists to prevent.
    """
    if reason == "version" and info is not None:
        _send_shutdown(info)
    if not claim_spawn_lock(lock_path, stale_seconds):
        return False
    published = read_info(info_path)
    replaced = published is not None and published != info
    if replaced and published.get("version") == code_version(repository_root):
        with contextlib.suppress(OSError):
            os.remove(lock_path)
        return False
    environment = dict(os.environ)
    if platform:
        environment["STATUSLINE_PLATFORM"] = platform
    command = [sys.executable, os.path.join(repository_root, SERVER_ENTRY_POINT)]
    try:
        _spawn(command, environment)
    except OSError as error:
        # The lock goes back, or one failed launch suppresses every other
        # client's spawn until the staleness window passes.
        _log_spawn_failure(error_log_path, command, error)
        with contextlib.suppress(OSError):
            os.remove(lock_path)
        return False
    return True
