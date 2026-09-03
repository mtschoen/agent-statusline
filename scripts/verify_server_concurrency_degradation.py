"""Verify what the 2026-09-02 incident regression does when psutil is missing.

scripts/verify_server_concurrency.py counts processes to assert the incident's
headline symptom cannot recur: no orphan interpreters, and never a second
server. Both counts go through psutil, and on a machine without it both
degrade to nothing. The orphan check then compares 0 against 0 and the
duplicate-server check sees only this process, so neither can fail. That is
the brief's design, but a run in that state must say so out loud: a bare
`OK:` line in a CI log would otherwise imply an assertion nobody made.

So this file pins both arms. One check forces the seam to None and requires
the skip to be reported; the other hands it a stand-in psutil showing a second
server and requires the counters to see it and the report to stay silent about
skipping. Both arms therefore run on every machine, whatever is installed:
the one with psutil still covers the absent path, and the one without still
covers the present path.

Nothing here starts, inspects or waits on a real process, and no check reads
a clock. The stand-in is a fixed list of (pid, command line) pairs.

Split out of scripts/verify_server_concurrency.py rather than appended to it:
that script is 397 lines and the repository holds every file at or under 400.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import io
import os
import sys
import types

# The scripts directory, so the script under test is importable. Importing it
# is also what installs the isolated HOME and CLAUDE_STATE_DIR the whole server
# family runs under, so it has to happen before statusline_lib is touched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_server_concurrency as suite

# A pid the stand-in reports for its second server. Negative, so it can never
# collide with this process's own pid and can never name anything real.
_SECOND_SERVER_PID = -1


class _FakePsutil:
    """The two psutil entry points the counters use, over a fixed list of
    (pid, argument list) pairs.

    The argument list is a list of strings, the shape real psutil reports and
    the shape the counters join; a bare string would join character by
    character and silently match nothing. Every process in the list is
    reported alive, which is what a list psutil just enumerated means. No
    process is started, inspected or waited on, so both counters are a pure
    function of this list.
    """

    def __init__(self, processes):
        self.processes = [
            types.SimpleNamespace(
                info={"pid": pid, "name": "python.exe", "cmdline": list(arguments)}
            )
            for pid, arguments in processes
        ]

    def process_iter(self, attributes, ad_value=None):
        del attributes, ad_value
        return self.processes

    def pid_exists(self, pid):
        del pid
        return True


@contextlib.contextmanager
def _psutil_as(module):
    """Force what the script's _psutil() returns for the duration of the
    block, restoring the real resolution on the way out."""
    saved = suite._psutil_override
    suite._psutil_override = module
    try:
        yield
    finally:
        suite._psutil_override = saved


class _StateOnlyContext:
    """The one field _count_server_processes reads. No server runs here: these
    checks are about what the counters say, not about serving a render."""

    state_directory = suite._STATE_DIR


def _second_server_stand_in():
    """A psutil stand-in showing this process plus one second server started
    out of this checkout, which is exactly the shape the incident produced."""
    entry_point = os.path.join(suite._REPO, suite._SERVER_ENTRY_POINT)
    return _FakePsutil(
        [
            (os.getpid(), [sys.executable, os.path.abspath(__file__)]),
            (_SECOND_SERVER_PID, [sys.executable, entry_point]),
        ]
    )


def _reported(failures):
    """The lines _report prints for `failures`, captured rather than printed,
    with the exit code it returned."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = suite._report(failures)
    return buffer.getvalue().splitlines(), code


def check_the_absent_arm_reports_a_skip(failures):
    """Without psutil both counters are blind, so the run has to name the two
    assertions it did not really make."""
    with _psutil_as(None):
        orphans = suite._count_repository_python_processes()
        servers = suite._count_server_processes(_StateOnlyContext())
        lines, code = _reported([])
    if orphans != 0:
        failures.append(f"the orphan counter saw {orphans} without psutil, expected 0")
    if servers != 1:
        failures.append(
            f"the server counter saw {servers} without psutil; with no process"
            " scan it can only ever see this process, so it must report 1"
        )
    if suite._SKIP_LINE not in lines:
        failures.append(f"a psutil-less run must print the skip line: {lines}")
    if not lines or not lines[-1].endswith(suite._SKIPPED_SUFFIX):
        failures.append(f"a psutil-less run's OK line must name the gap: {lines}")
    if code != 0:
        failures.append(f"a psutil-less run with no failures must exit 0, got {code}")


def check_the_present_arm_counts_a_second_server(failures):
    """With psutil the counters have to see the thing the incident produced:
    a second server out of this checkout, and the interpreters running it."""
    with _psutil_as(_second_server_stand_in()):
        orphans = suite._count_repository_python_processes()
        servers = suite._count_server_processes(_StateOnlyContext())
        lines, code = _reported([])
    if orphans != 2:
        failures.append(f"the orphan counter saw {orphans} interpreters, expected 2")
    if servers != 2:
        failures.append(
            f"the server counter saw {servers} servers, expected 2;"
            " a duplicate server is the assertion the burst check rests on"
        )
    if lines != [suite._OK_LINE]:
        failures.append(f"a run with psutil must print a bare OK line: {lines}")
    if code != 0:
        failures.append(f"a passing run must exit 0, got {code}")


def check_a_failure_is_reported_whichever_arm_is_live(failures):
    """A skip is not an excuse: a real failure still prints and still exits
    non-zero, with or without psutil."""
    for module, name in ((None, "without psutil"), (_second_server_stand_in(), "with")):
        with _psutil_as(module):
            lines, code = _reported(["a deliberate failure"])
        if lines != ["FAIL: a deliberate failure"]:
            failures.append(f"{name}, a failing run printed {lines}")
        if code != 1:
            failures.append(f"{name}, a failing run exited {code}, expected 1")


def check(failures):
    check_the_absent_arm_reports_a_skip(failures)
    check_the_present_arm_counts_a_second_server(failures)
    check_a_failure_is_reported_whichever_arm_is_live(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: a missing psutil is reported rather than passed over")


if __name__ == "__main__":
    main()
