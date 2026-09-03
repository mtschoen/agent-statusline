"""Verify statusline_lib/transcript_summaries.py and its three consumers: the
beacon column's anchors, an Agent Teams teammate's cost, and a subagent row's
metrics all come out of memory rather than out of a transcript opened on the
resident server's receive thread.

Per lookup: one os.stat and nothing else on the calling thread; a miss with a
pool submits a job and answers None; a miss with no pool computes inline, so a
process that is not the resident server still renders correct numbers; a changed
file serves stale and asks for one recomputation per refresh interval. Every
clock is injected, nothing sleeps, and no check branches on the operating system.

Run from anywhere; imports from agent-statusline by path.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

_HOME = tempfile.mkdtemp(prefix="verify-transcript-summaries-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["CLAUDE_STATE_DIR"] = os.path.join(_HOME, "state")
os.environ["STATUSLINE_PREFS_PATH"] = os.devnull

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.beacon as beacon_module
import statusline_lib.render_subagent as subagent_module
import statusline_lib.server_jobs as server_jobs
import statusline_lib.transcript_summaries as summaries
from statusline_lib.teams import format_teammates

_TEXT_ENCODING = "utf-8"
# The injected clock; only the gaps between readings carry any meaning.
_CLOCK = 100.0
_RENDER_CLOCK = 1000.0
_WALK = "transcript-walk"
_ANCHORS = "beacon-anchors"
_MAXIMUM_PREF = "STATUSLINE_TRANSCRIPT_SUMMARY_ENTRIES"
_REFRESH_PREF = "STATUSLINE_TRANSCRIPT_SUMMARY_REFRESH_SECONDS"

# A begin beacon and one assistant turn, so both refreshers have a non-empty
# answer and "served the value" reads differently from "served None".
_BEGIN_TIMESTAMP = "2026-05-01T10:00:00.000Z"
_BEACON_TEXT = (
    "<progress-beacon>"
    + json.dumps({"kind": "begin", "eta_seconds": 120})
    + "</progress-beacon>"
)


def _assistant_turn(message_id="m1"):
    return {
        "type": "assistant",
        "timestamp": _BEGIN_TIMESTAMP,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 100, "output_tokens": 200},
            "content": [{"type": "text", "text": _BEACON_TEXT}],
        },
    }


def _write_transcript(path, turns=1):
    with open(path, "w", encoding=_TEXT_ENCODING) as f:
        for index in range(turns):
            f.write(json.dumps(_assistant_turn(f"m{index}")) + "\n")


def _append_turn(path, message_id):
    with open(path, "a", encoding=_TEXT_ENCODING) as f:
        f.write(json.dumps(_assistant_turn(message_id)) + "\n")


class _RecordingSink:
    """The resident server's worker pool, minus the pool: records what would be
    submitted and runs nothing, so a check says when a refresh happens."""

    def __init__(self):
        self.jobs = []

    def __call__(self, kind, argument):
        self.jobs.append((kind, argument))
        return True


class _Sink:
    """Installs a recording sink for the body of a `with`, and empties the
    summary table on both ends so no check inherits another's warm entries."""

    def __init__(self, install=True):
        self.sink = _RecordingSink()
        self._install = install
        self._previous = None

    def __enter__(self):
        summaries.reset_transcript_summaries()
        self._previous = server_jobs.set_refresh_sink(
            self.sink if self._install else None
        )
        return self.sink

    def __exit__(self, *exception):
        server_jobs.set_refresh_sink(self._previous)
        summaries.reset_transcript_summaries()
        return False


class _Loud:
    """Both refreshers' readers, replaced by something that fails when called:
    how a check proves a render never opened the transcript at all, rather than
    merely never used what it found."""

    def __init__(self):
        self._saved = {}

    def _explode(self, path, **keywords):
        del keywords
        raise AssertionError(f"the render path opened {path}")

    def __enter__(self):
        for name in ("walk_transcript", "_scan_beacon_anchors"):
            self._saved[name] = getattr(summaries, name)
            setattr(summaries, name, self._explode)
        return self

    def __exit__(self, *exception):
        for name, original in self._saved.items():
            setattr(summaries, name, original)
        return False


def _transcript(directory, name="session.jsonl", turns=1):
    path = os.path.join(directory, name)
    _write_transcript(path, turns)
    return path


def check_a_path_with_nothing_to_read_is_none_without_a_job(failures):
    """A missing file and an empty path fail the stat; a directory passes it
    and opens nowhere, on Windows and on POSIX alike, which is the shape of a
    transcript deleted between the stat and the read."""
    with _Sink() as sink:
        for path in (os.path.join(_HOME, "no-such-file.jsonl"), ""):
            if summaries.summary_for(_WALK, path, now=_CLOCK) is not None:
                failures.append(f"summaries.summary_for({path!r}) must be None")
        if sink.jobs:
            failures.append(f"a path with no file must submit nothing: {sink.jobs}")
    with tempfile.TemporaryDirectory() as directory, _Sink(install=False):
        if summaries.summary_for(_ANCHORS, directory, now=_CLOCK) is not None:
            failures.append("an unreadable path must be None")


def check_the_first_lookup_submits_one_job_and_serves_nothing(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        if summaries.summary_for(_WALK, path, now=_CLOCK) is not None:
            failures.append("an unseen path must serve None while the pool works")
        if summaries.summary_for(_WALK, path, now=_CLOCK) is not None:
            failures.append("a second lookup before the refresh must still be None")
        if sink.jobs != [(_WALK, path), (_WALK, path)]:
            failures.append(f"expected a job per lookup, got {sink.jobs}")


def check_a_refreshed_summary_is_served_from_memory(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        summaries.summary_for(_WALK, path, now=_CLOCK)
        summaries.refresh_transcript_walk(path, now=_CLOCK + 1)
        del sink.jobs[:]
        with _Loud():
            walk = summaries.summary_for(_WALK, path, now=_CLOCK + 2)
        if walk is None or walk["assistant_turns"] != 1:
            failures.append(f"the refreshed walk must be served from memory: {walk}")
        if sink.jobs:
            failures.append(f"an unchanged file must submit nothing, got {sink.jobs}")


def check_a_grown_transcript_serves_stale_until_the_refresh_interval(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        summaries.refresh_transcript_walk(path, now=_CLOCK)
        _append_turn(path, "m2")
        del sink.jobs[:]
        with _Loud():
            early = summaries.summary_for(_WALK, path, now=_CLOCK + 2)
            late = summaries.summary_for(_WALK, path, now=_CLOCK + 6)
        if early is None or early["assistant_turns"] != 1:
            failures.append(f"a grown file must serve its stale summary: {early}")
        if late is not early:
            failures.append("the stale summary must keep being served until refreshed")
        if sink.jobs != [(_WALK, path)]:
            failures.append(
                f"one refresh once the interval passed, and not before: {sink.jobs}"
            )
        summaries.refresh_transcript_walk(path, now=_CLOCK + 10)
        if summaries.summary_for(_WALK, path, now=_CLOCK + 11)["assistant_turns"] != 2:
            failures.append("the refreshed walk must replace the stale one")


def check_without_a_pool_the_summary_is_computed_inline(failures):
    """Every process that is not the resident server: a verify script, a
    statusline-ctl subcommand, anything rendering without a server. There is
    nothing to hand the work to, so answering None would answer None forever."""
    with tempfile.TemporaryDirectory() as directory, _Sink(install=False):
        path = _transcript(directory)
        walk = summaries.summary_for(_WALK, path, now=_CLOCK)
        if walk is None or walk["assistant_turns"] != 1:
            failures.append(f"with no pool the walk must be computed inline: {walk}")
        state = summaries.summary_for(_ANCHORS, path, now=_CLOCK)
        if state is None or state["begin_ts"] != _BEGIN_TIMESTAMP:
            failures.append(
                f"with no pool the anchors must be computed inline: {state}"
            )
        with _Loud():
            again = summaries.summary_for(_WALK, path, now=_CLOCK + 1)
        if again is not walk:
            failures.append("the inline result must be published for the next lookup")


def check_a_raising_refresher_leaves_the_previous_summary(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        summaries.refresh_transcript_walk(path, now=_CLOCK)
        _append_turn(path, "m2")
        saved = summaries.walk_transcript

        def raising(_path, include_subagents=False):
            raise ValueError("refresher exploded")

        summaries.walk_transcript = raising
        raised = False
        try:
            summaries.refresh_transcript_walk(path, now=_CLOCK + 10)
        except ValueError:
            raised = True
        finally:
            summaries.walk_transcript = saved
        if not raised:
            failures.append("a refresher's exception must propagate to the pool")
        del sink.jobs[:]
        stale = summaries.summary_for(_WALK, path, now=_CLOCK + 11)
        if stale is None or stale["assistant_turns"] != 1:
            failures.append(f"a failed refresh must leave the old entry: {stale}")


def check_the_table_sheds_its_least_recently_served_entries(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink():
        first = _transcript(directory, "first.jsonl")
        second = _transcript(directory, "second.jsonl")
        third = _transcript(directory, "third.jsonl")
        os.environ[_MAXIMUM_PREF] = "2"
        try:
            summaries.refresh_transcript_walk(first, now=_CLOCK)
            summaries.refresh_transcript_walk(second, now=_CLOCK + 1)
            # Serving `first` makes `second` the least recently served, so the
            # eviction has to follow the reads and not the writes.
            summaries.summary_for(_WALK, first, now=_CLOCK + 2)
            summaries.refresh_transcript_walk(third, now=_CLOCK + 3)
        finally:
            del os.environ[_MAXIMUM_PREF]
        kept = [
            path
            for path in (first, second, third)
            if summaries.summary_for(_WALK, path, now=_CLOCK + 4) is not None
        ]
        if kept != [first, third]:
            failures.append(f"the oldest served entry must go first: {kept}")


def check_the_refresh_interval_is_overridable(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        summaries.refresh_transcript_walk(path, now=_CLOCK)
        _append_turn(path, "m2")
        del sink.jobs[:]
        os.environ[_REFRESH_PREF] = "1"
        try:
            with _Loud():
                summaries.summary_for(_WALK, path, now=_CLOCK + 1.5)
        finally:
            del os.environ[_REFRESH_PREF]
        if sink.jobs != [(_WALK, path)]:
            failures.append(f"a shortened refresh interval must apply: {sink.jobs}")


def check_the_beacon_column_never_opens_the_transcript(failures):
    """beacon._find_beacon_anchors is reached from every claude render, so its
    whole-file scan is the one that must never run on the receive thread."""
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        path = _transcript(directory)
        original = beacon_module._find_session_jsonl
        beacon_module._find_session_jsonl = lambda _session_id: path
        try:
            with _Loud():
                cold = beacon_module._find_beacon_anchors("any-session")
            if cold != (None, None, None):
                failures.append(f"a cold beacon lookup must render bare: {cold}")
            if sink.jobs != [(_ANCHORS, path)]:
                failures.append(f"expected one beacon-anchors job: {sink.jobs}")
            server_jobs.run_refresh(_ANCHORS, path)
            with _Loud():
                warm = beacon_module._find_beacon_anchors("any-session")
            if warm != (_BEGIN_TIMESTAMP, None, 120.0):
                failures.append(f"the pool's scan must fill the anchors: {warm}")
        finally:
            beacon_module._find_session_jsonl = original


def _team_fixture(directory, transcript_path):
    subagents = os.path.splitext(transcript_path)[0] + "/subagents"
    os.makedirs(subagents, exist_ok=True)
    teammate = os.path.join(subagents, "agent-awatchme-abc123.jsonl")
    _write_transcript(teammate)
    team_dir = os.path.join(directory, "teams", "session-b213becd")
    os.makedirs(team_dir, exist_ok=True)
    with open(os.path.join(team_dir, "config.json"), "w", encoding=_TEXT_ENCODING) as f:
        json.dump({"members": [{"name": "watchme", "model": "sonnet"}]}, f)
    return teammate


def check_a_teammate_row_never_opens_the_transcript(failures):
    session_id = "b213becd-6513-43f3-95a1-e4d51c47cb39"
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        transcript = os.path.join(directory, "session.jsonl")
        _write_transcript(transcript)
        teammate = _team_fixture(directory, transcript)
        with _Loud():
            cold = format_teammates(session_id, transcript, directory, _RENDER_CLOCK)
        if "watchme" not in cold or "$" in cold:
            failures.append(f"a cold teammate row must render without cost: {cold!r}")
        if sink.jobs != [(_WALK, teammate)]:
            failures.append(f"expected one teammate walk job: {sink.jobs}")
        server_jobs.run_refresh(_WALK, teammate)
        with _Loud():
            warm = format_teammates(session_id, transcript, directory, _RENDER_CLOCK)
        if "$" not in warm:
            failures.append(f"the pool's walk must fill the teammate cost: {warm!r}")


def check_a_subagent_row_never_opens_the_transcript(failures):
    with tempfile.TemporaryDirectory() as directory, _Sink() as sink:
        transcript = os.path.join(directory, "session.jsonl")
        _write_transcript(transcript)
        subagents = os.path.join(directory, "session", "subagents")
        os.makedirs(subagents, exist_ok=True)
        _write_transcript(os.path.join(subagents, "agent-task-1.jsonl"))
        task = {"id": "task-1", "status": "running", "description": "a task"}
        # The row's own spelling of the path, which is also the summary's key:
        # the module joins the subagents directory with a forward slash.
        agent = subagent_module._agent_jsonl_path(transcript, "task-1")
        with _Loud():
            parts, model_id = subagent_module._metrics_for_task(
                task, transcript, "sid", False, 1000.0
            )
        if parts or model_id:
            failures.append(f"a cold subagent row must render bare: {parts} {model_id}")
        if sink.jobs != [(_WALK, agent)]:
            failures.append(f"expected one subagent walk job: {sink.jobs}")
        server_jobs.run_refresh(_WALK, agent)
        with _Loud():
            parts, model_id = subagent_module._metrics_for_task(
                task, transcript, "sid", False, 1000.0
            )
        if not parts or model_id != "claude-sonnet-4-5":
            failures.append(f"the pool's walk must fill the row: {parts} {model_id}")


def check(failures):
    check_a_path_with_nothing_to_read_is_none_without_a_job(failures)
    check_the_first_lookup_submits_one_job_and_serves_nothing(failures)
    check_a_refreshed_summary_is_served_from_memory(failures)
    check_a_grown_transcript_serves_stale_until_the_refresh_interval(failures)
    check_without_a_pool_the_summary_is_computed_inline(failures)
    check_a_raising_refresher_leaves_the_previous_summary(failures)
    check_the_table_sheds_its_least_recently_served_entries(failures)
    check_the_refresh_interval_is_overridable(failures)
    check_the_beacon_column_never_opens_the_transcript(failures)
    check_a_teammate_row_never_opens_the_transcript(failures)
    check_a_subagent_row_never_opens_the_transcript(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: transcript summaries and their three consumers verified")


if __name__ == "__main__":
    main()
