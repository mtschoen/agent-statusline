"""psutil resolution and the process-table snapshot the session scan reads.

Two modules need psutil and neither owns it: the session-count scan
(statusline_lib/sessions.py) enumerates agent runtimes, and the resident
server's liveness probe (statusline_lib/server_info.py) asks whether a
recorded pid is still alive. Both resolve it through `_resolve_psutil` here,
so a machine without psutil degrades through one code path rather than two
copies of the same try/except.

`_LazySnapshot` is the pid -> (ppid, name, create_time) mapping the process
tree walk reads. It lives here rather than in sessions.py so that module,
already near this repository's file-size gate, is not the only place the
package's psutil handling can be found.

Imports: standard library only, plus the lazy psutil import below.
"""


def _resolve_psutil():
    """Import psutil if available; return None if not installed."""
    try:
        import psutil

        return psutil
    except ImportError:
        return None


class _LazySnapshot:
    """pid -> (ppid, name, create_time) mapping, filled on demand.

    Names come from the same cheap process_iter(["name"]) pass the candidate
    pre-filter uses (one toolhelp snapshot on Windows, ~20ms for ~600 procs);
    ppid/create_time cost an OpenProcess per pid there (~11s observed when
    requested as process_iter attrs), so they are fetched only for the pids
    the tree walk actually visits -- candidates plus their ancestor chains,
    a handful.
    """

    def __init__(self, psutil, names):
        self._psutil = psutil
        self._names = names
        self._rows = {}

    def get(self, pid, default=None):
        if pid is None:
            return default
        if pid not in self._rows:
            self._rows[pid] = self._fetch(pid)
        row = self._rows[pid]
        return default if row is None else row

    def _fetch(self, pid):
        psutil = self._psutil
        try:
            proc = psutil.Process(pid)
            name = self._names.get(pid)
            if name is None:
                name = proc.name()
            return (proc.ppid(), name, proc.create_time())
        except psutil.NoSuchProcess:
            return None  # dead pid: absent, same as a vanished parent
        except psutil.AccessDenied:
            # Unreadable but alive: present and non-agent, so the walk ends
            # here without tripping the orphan rule (a candidate is only an
            # orphan when its parent is *gone*, not merely opaque).
            return (None, self._names.get(pid), 0.0)
