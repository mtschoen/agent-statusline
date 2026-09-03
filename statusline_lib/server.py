"""The resident render server: request handling.

One server process per app_dir() answers every statusline render on the
machine, so the per-process cost of a render (imports, a full transcript
walk, a set of cold caches) is paid once for the whole process rather than
once for every render. The transport underneath is server_socket.

The split that makes that safe is the whole design. A render is pure
formatting over the request payload plus the in-memory tables in server_state,
so it is served inline on the receive loop. Everything that can block (git,
psutil, HTTP, the walker) is a refresh job on the bounded pool in server_jobs,
which the constructor installs as the package-wide refresh sink: the receive
loop never waits on it, the pool cannot grow past its ceiling, and a wedged
refresher costs one stale field rather than every render on the machine.

The socket half lives here too: bind() publishes a random localhost UDP port
in server.json, serve_forever() is the receive loop (and the idle exit that
ends a server nobody is rendering against), close() gives back everything
bind() took, and serve() is the entry point body statusline_server.py calls.

Imports:
  base          -- app_dir, log_line, log_traceback, platform_name, state_dir
  server_info   -- code_version, and the server.json read/write helpers
  server_jobs   -- WorkerPool, set_refresh_sink
  server_socket -- the transport: constants, the socket, the wire format
  server_render -- the harness renders, the last-render file, $COLUMNS
  server_state  -- StateTables, housekeep_state_dir
  transcript_summaries -- reset_transcript_summaries
"""

import contextlib
import json
import os
import time

from .base import app_dir, log_line, log_traceback, platform_name, state_dir
from .server_info import (
    code_version,
    read_server_info,
    remove_server_info,
    server_info_path,
    write_server_info,
)
from .server_jobs import WORKER_POOL_SIZE, WorkerPool, set_refresh_sink
from .server_render import (
    columns_environment,
    last_render_path,
    record_render_timing,
    render_claude_request,
    render_kimi_request,
    render_qwen_request,
    render_subagent_request,
    session_id_for,
    write_debug_input_log,
    write_last_render,
)
from .server_socket import (
    IDLE_EXIT_SECONDS,
    MAXIMUM_DATAGRAM_BYTES,
    clear_spawn_lock,
    open_datagram_socket,
    parse_request,
    pref_number,
)
from .server_state import (
    HOUSEKEEPING_INTERVAL_SECONDS,
    StateTables,
    housekeep_state_dir,
)
from .transcript_summaries import reset_transcript_summaries

# last_render_path and write_last_render are re-exported rather than defined
# here: they live beside the renders that write them, but this module is the
# server's documented surface, so both names resolve from it.
__all__ = ["REQUEST_KINDS", "Server", "last_render_path", "serve", "write_last_render"]

# Request kind -> the name of the Server method that serves it. Names rather
# than function objects, so an attribute set on one instance (a test's
# failing renderer, a future per-platform override) wins the way normal
# attribute lookup does.
_HANDLER_NAMES = {
    "claude": "_render_claude",
    "subagent": "_render_subagent",
    "kimi": "_render_kimi",
    "qwen": "_render_qwen",
    "shutdown": "_handle_shutdown",
    "status": "_handle_status",
}

# The protocol surface, in the same order. Anything else is answered with
# silence rather than an error the client has no way to act on.
REQUEST_KINDS = ("claude", "subagent", "kimi", "qwen", "shutdown", "status")

# The request kinds whose handling is timed for the render-duration suffix.
# "subagent" is excluded on purpose: its panel carries no duration of its own.
_TIMED_KINDS = ("claude", "kimi", "qwen")


class Server:
    """The resident render server for one app_dir().

    Renders are pure formatting over the payload plus in-memory state, so they
    are served inline on the receive loop and reply in single-digit
    milliseconds. Anything that can block (git, psutil, HTTP, the walker) runs
    on the worker pool and never on the receive path. That split is the whole
    design: the pool has a hard ceiling, the receive loop never waits on it,
    and a wedged refresher can therefore slow a field, never a render.
    """

    def __init__(
        self,
        state_directory,
        repository_root,
        *,
        clock=time.time,
        pool=None,
        tables=None,
        error_log_path=None,
    ):
        self._state_directory = state_directory
        self._repository_root = repository_root
        self._clock = clock
        self._error_log_path = error_log_path or os.path.join(
            app_dir(), ".statusline-error.log"
        )
        self._tables = tables or StateTables(clock=clock)
        self._pool = pool or WorkerPool(
            size=pref_number("STATUSLINE_SERVER_WORKERS", WORKER_POOL_SIZE, int),
            error_logger=self._log_job_error,
        )
        # An attribute rather than a direct call so a test can swap the sweep
        # for a recorder and drive the schedule with a fake clock.
        self._housekeeper = housekeep_state_dir
        self._started_at = clock()
        self._last_housekeeping = 0.0
        self._last_request_at = self._started_at
        self._idle_exit_seconds = pref_number(
            "STATUSLINE_SERVER_IDLE_SECONDS", float(IDLE_EXIT_SECONDS), float
        )
        self._socket = None
        self._port = None
        self._closed = False
        self._info_path = server_info_path(state_directory)
        # Set only once write_server_info has actually returned, so close()
        # can tell "this server published server.json" from "this server never
        # got that far", and never removes a file it did not write.
        self._info_published = False
        self.stop_requested = False
        # Every cache reader in the package asks for recomputation through
        # server_jobs.request_refresh, which is a no-op until something
        # installs a sink. This is the process that has one. The displaced
        # sink is kept so stop_refresh_sink can put it back: the sink is a
        # module global, so a stopped server that never restored it would go
        # on receiving every refresh request into a pool nobody drains.
        self._previous_refresh_sink = set_refresh_sink(self._pool.submit)
        self._refresh_sink_installed = True

    def stop_refresh_sink(self):
        """Give the refresh sink back to whoever held it before this server.
        Idempotent, so Task 13's close() can call it unconditionally."""
        if not self._refresh_sink_installed:
            return
        self._refresh_sink_installed = False
        set_refresh_sink(self._previous_refresh_sink)

    def bind(self):
        """Bind a random localhost UDP port, start the pool, publish
        server.json, then clear the spawn lock. Returns the port."""
        self._socket, self._port = open_datagram_socket()
        self._pool.start()
        write_server_info(
            self._info_path,
            pid=os.getpid(),
            port=self._port,
            version=code_version(self._repository_root),
            started_at=self._started_at,
            platform=platform_name() or "claude",
        )
        self._info_published = True
        clear_spawn_lock(self._state_directory)
        return self._port

    def serve_forever(self):
        """Receive, handle, reply, until shutdown or the idle window passes.

        The timeout on the socket is what makes the idle check work without a
        sleep anywhere: every RECEIVE_POLL_SECONDS the loop either has a
        datagram or gets a chance to notice how long it has been idle. A
        reply of None is a request with nothing to say (a shutdown, an
        unrecognized kind, a render that raised); an empty reply is a render
        that legitimately produced no text, and it goes out as a zero-length
        datagram so the client prints nothing rather than waiting out its
        whole timeout for an answer that already happened.
        """
        while not self.stop_requested:
            try:
                data, address = self._socket.recvfrom(MAXIMUM_DATAGRAM_BYTES)
            except TimeoutError:
                if self._clock() - self._last_request_at >= self._idle_exit_seconds:
                    self.stop_requested = True
                continue
            except OSError:
                # Transient on Windows: a UDP recvfrom raises WinError 10054
                # when an earlier sendto drew an ICMP port unreachable from a
                # client that has already gone. One log line, not a dead
                # server; close() is what actually ends this loop.
                log_traceback(self._error_log_path)
                continue
            try:
                request = parse_request(data)
                if request is None:
                    continue
                reply = self.handle_request(request)
                if reply is None:
                    continue
                with contextlib.suppress(OSError):
                    self._socket.sendto(reply.encode("utf-8", "replace"), address)
            except Exception:
                # Nothing a datagram carries may end this process. Two of
                # these are reachable from bytes alone: a deeply nested
                # payload raises RecursionError out of parse_request, and a
                # reply holding a lone surrogate raises UnicodeEncodeError,
                # neither of which the narrower guards catch. A poisoned
                # request that killed the loop would leave every later render
                # spawning a replacement that dies on the same input.
                log_traceback(self._error_log_path)

    def close(self):
        """Give back everything bind() took: the pool, the refresh sink, the
        socket, server.json, and the process-wide summary table the pool filled.
        Idempotent, because the shutdown path closes explicitly and serve()
        closes again in its finally. The stop flag is set here too, so a
        serve_forever still running on another thread ends at its next poll. The
        closed socket is kept rather than dropped for the same reason: a receive
        already in flight against it fails with the OSError that loop already
        handles, where a None would raise an AttributeError nothing catches.

        server.json goes first, and only while it is still this server's.
        First, because stopping the pool waits on every worker and a wedged
        refresher can hold each of them, and for that whole window the file
        would still advertise a port nothing answers on. This server's,
        because a client can spawn a successor while this process drains: the
        publish flag says this process wrote the file, and the recorded pid
        says the file on disk is still the one it wrote.
        """
        if self._closed:
            return
        self._closed = True
        self.stop_requested = True
        if self._info_published:
            self._info_published = False
            published = read_server_info(self._info_path)
            if published is not None and published.get("pid") == os.getpid():
                remove_server_info(self._info_path)
        self._pool.stop()
        self.stop_refresh_sink()
        reset_transcript_summaries()
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()

    def handle_request(self, request):
        """Reply text for one request, or None when the client should fall
        back to its own last-render file.

        None means one of two things, and every case is logged: the request
        was a shutdown, which has nothing to say, or nothing rendered, which
        is either an unrecognized kind or a render that raised.

        "Render" here means one request's dispatch through this method, not a
        whole process lifetime: the resident server has no process boundary
        per render, so a claude, kimi or qwen request times only its own
        handler call below, the same duration format_render_suffix shows on
        the NEXT render. A claude or subagent request also dumps its payload
        to `.statusline-*-input.log` via server_render.write_debug_input_log.
        Both side effects are best-effort and cannot change or block the reply.

        The request's `columns` is the client's terminal width, applied as
        $COLUMNS around the dispatch below and restored after it, so
        compact.py's width gate reads the client's terminal, not this one's.
        """
        kind = request.get("kind")
        handler_name = _HANDLER_NAMES.get(kind)
        if handler_name is None:
            log_line(self._error_log_path, f"unrecognized request kind: {kind!r}")
            return None
        # After the kind check, not before: an unrecognized request must not
        # refresh the idle window, or anything spraying nonsense at the port
        # would hold a server open that nobody is rendering against.
        self._last_request_at = self._clock()
        payload = request.get("payload") or {}
        try:
            self._maybe_housekeep()
            write_debug_input_log(kind, payload)
            started = time.perf_counter() if kind in _TIMED_KINDS else None
            with columns_environment(request.get("columns")):
                reply = getattr(self, handler_name)(payload)
            if started is not None:
                elapsed_ms = (time.perf_counter() - started) * 1000
                record_render_timing(
                    session_id_for(payload), elapsed_ms, self._state_directory
                )
            return reply
        except Exception:
            log_traceback(self._error_log_path)
            return None

    def _handle_shutdown(self, payload):
        """Ask the serve loop to stop. Replies nothing, because there is
        nothing to say and no client is waiting on an answer. Logged, so a
        server that vanished can be told from one that was asked to go."""
        del payload
        log_line(self._error_log_path, "shutdown requested")
        self.stop_requested = True

    def _handle_status(self, payload):
        """What this process is holding and how hard the pool is working, as
        one JSON object. `version` is the code digest the client compares
        against its own to detect a checkout that has moved."""
        del payload
        summary = self._tables.summary()
        summary.update(
            {
                "uptime_seconds": self._clock() - self._started_at,
                "queue_depth": self._pool.queue_depth(),
                "in_flight": self._pool.in_flight_count(),
                "peak_in_flight": self._pool.peak_in_flight(),
                "workers": self._pool.worker_count(),
                "version": code_version(self._repository_root),
                "pid": os.getpid(),
            }
        )
        return json.dumps(summary)

    # The four renders live in server_render; _HANDLER_NAMES names these.

    def _render_claude(self, payload):
        return render_claude_request(
            payload, self._tables, self._clock, self._state_directory
        )

    def _render_subagent(self, payload):
        return render_subagent_request(payload, self._clock())

    def _render_kimi(self, payload):
        return render_kimi_request(payload, self._tables, self._state_directory)

    def _render_qwen(self, payload):
        return render_qwen_request(payload, self._tables, self._state_directory)

    def _maybe_housekeep(self):
        """Sweep aged per-session state files and drop idle table entries, at
        most once every HOUSEKEEPING_INTERVAL_SECONDS. Both are the price of a
        process that outlives its sessions: a render process left its state
        files behind and its tables died with it, while this one accumulates
        both until something sheds them."""
        now = self._clock()
        if now - self._last_housekeeping < HOUSEKEEPING_INTERVAL_SECONDS:
            return
        self._last_housekeeping = now
        self._housekeeper(self._state_directory, now)
        self._tables.drop_idle()

    def _log_job_error(self, error):
        """Record a pool job's traceback in this server's error log. Correct
        only from inside the except block still handling `error`, which is how
        WorkerPool calls it: log_traceback reads the live exception context
        rather than `error` itself."""
        del error
        log_traceback(self._error_log_path)


def serve(argv=None):
    """The entry point body statusline_server.py calls. Returns an exit code.

    One housekeeping sweep runs before the loop rather than waiting for the
    first request: the spec's "on start and hourly", and the reason a state
    directory can accumulate thousands of stale per-session caches that no
    process ever came back for.
    """
    del argv  # No options yet; the shape is fixed so adding one is additive.
    repository_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = Server(state_dir(), repository_root)
    try:
        server.bind()
        # _last_housekeeping starts at zero, so this first call always
        # sweeps; every later one is the hourly schedule.
        server._maybe_housekeep()
        server.serve_forever()
    except Exception:
        # This process is a detached spawn with nowhere to print, so an
        # escaping traceback is a server that dies invisibly and a client that
        # spawns another one just like it. Log, and say so in the exit code.
        log_traceback(server._error_log_path)
        return 1
    finally:
        # Inside the finally so a bind that failed half way still gives back
        # the refresh sink and the pool it took on the way in.
        server.close()
    return 0
