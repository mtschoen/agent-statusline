"""Verify the cold-start render scenarios that motivated the 2026-07-26
render-perf fix: a session whose caches (render-timer, git ref, beacons-
latest, bias-factor, session-count, pace/spend) are ALL simultaneously
absent must still render fast.

The actual 5.8s slow-render spike this suite guards against was root-caused
(via render-timer state, not guesswork) to a brand-new session's first-ever
render landing during session-startup process churn, NOT a warm session
whose cache had gone stale -- see PLAN.md. That distinction matters here:
check_first_render_ever_stays_fast reproduces the true first-render shape
(no beacon exists yet, so _beacon_line never reaches the bias-factor path
at all); check_bias_factor_cold_cache_stays_fast reproduces the specific
path the fix targeted (an ongoing session whose bias-factor cache alone has
gone cold while a beacon is active), which needs a pre-warmed beacons-latest
entry to reach.

These are real-subprocess end-to-end checks. They complement, but do not
replace, the decisive in-process regression proof in
verify_beacon_walker.py's _check_bias_cache_never_waits_on_slow_walker (an
artificially slow fake walker, which this machine's real claude-walker.exe
is too fast to exercise meaningfully here).

Split out of verify_render_budget.py (which owns the general render-path
sync-call and cold/warm budget checks) once this suite's growth pushed the
combined file over aislop's file-size gate.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _render_fixture_helpers import _REPO, build_fixture_home

# Tighter than verify_render_budget.py's _RENDER_BUDGET_SECONDS (8s, an
# incident-level "did it hang" check): a first render that misses every
# cache at once should still be dominated by fast local I/O plus a handful
# of fire-and-forget detached-refresh spawns, not by waiting on any
# subprocess result. 3s gives ~10x headroom over the observed ~0.1-0.3s cold
# render while still catching a regression where something blocks on even
# one 2s-capped subprocess call.
_COLD_START_FAST_BUDGET_SECONDS = 3.0


def _run_cold_render(env, payload, hard_kill_multiplier=3):
    """Shared subprocess-render helper for the two checks below. Returns
    (elapsed_seconds, CompletedProcess) or (None, None) on a hard kill (the
    caller appends the failure)."""
    start = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(_REPO, "statusline.py")],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=_COLD_START_FAST_BUDGET_SECONDS * hard_kill_multiplier,
        )
    except subprocess.TimeoutExpired:
        return None, None
    return time.perf_counter() - start, result


def check_first_render_ever_stays_fast(failures):
    """A genuinely brand-new session's first-ever render -- the deterministic
    worst case root-caused from the 2026-07-26 5.8s spike (render-timer
    state showed it was NOT a warm session with a stale cache; it was a
    fresh session id whose render-timer/beacon-latest/gitref/bias-factor/
    session-count caches were ALL simultaneously absent, landing during
    session-startup process churn). Every one of those cache misses must
    serve its neutral/absent default and spawn a detached refresh rather
    than block, so the render itself should stay fast regardless of how
    many caches miss at once.

    No beacon is seeded here (a session's first-ever render also has no
    progress-beacon in its transcript yet, so _beacon_line's cache lookup
    misses and returns immediately without ever reaching the bias-factor
    path -- see check_bias_factor_cold_cache_stays_fast for that path
    specifically, which needs a pre-warmed beacon-latest entry to reach).
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = os.path.join(tmp, "home")
        build_fixture_home(home, n_sessions=1, turns_per_session=1)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env.pop("CLAUDE_WALKER_BIN", None)
        payload = json.dumps(
            {
                "session_id": str(uuid.uuid4()),
                "cwd": _REPO,
                "workspace": {"current_dir": _REPO, "project_dir": _REPO},
                "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
            }
        )
        elapsed, result = _run_cold_render(env, payload)
        if elapsed is None:
            failures.append(
                f"first-ever render exceeded its"
                f" {_COLD_START_FAST_BUDGET_SECONDS * 3}s hard kill"
            )
            return
        if result.returncode != 0:
            failures.append(f"first-ever render exited {result.returncode}")
        if elapsed > _COLD_START_FAST_BUDGET_SECONDS:
            failures.append(
                f"first-ever render (zero warm caches) took {elapsed:.1f}s"
                f" (budget {_COLD_START_FAST_BUDGET_SECONDS}s) -- some cache"
                " miss is blocking the render instead of serving a neutral"
                " default and spawning a refresh"
            )


def check_bias_factor_cold_cache_stays_fast(failures):
    """The specific path the 2026-07-26 fix targeted: a render that reaches
    _beacon_line -> format_calibrated_eta -> _bias_factor_cached with an
    entirely cold bias-factor cache (an ongoing session, ttl-expired or
    never-populated .statusline-bias-cache.json), while the transcript
    genuinely carries an active begin-beacon with a positive eta_seconds --
    the exact condition that used to pay a real, in-render beacons-history
    subprocess call (2s cap). The beacons-latest cache is pre-seeded fresh
    (simulating an ongoing session, not a brand-new one -- see
    check_first_render_ever_stays_fast for the true first-ever-render case,
    which never reaches this path at all) so format_beacon actually walks
    through to the bias-factor lookup instead of short-circuiting on its own
    cache miss.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = os.path.join(tmp, "home")
        projects = build_fixture_home(home, n_sessions=1, turns_per_session=1)
        session_id = str(uuid.uuid4())
        transcript_path = os.path.join(projects, f"{session_id}.jsonl")
        beacon_line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-11T00:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<progress-beacon>"
                                '{"kind": "begin", "eta_seconds": 600,'
                                ' "summary": "working"}'
                                "</progress-beacon>"
                            ),
                        }
                    ],
                },
            }
        )
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(beacon_line + "\n")

        state_dir = os.path.join(home, ".claude", "state")
        os.makedirs(state_dir, exist_ok=True)
        from statusline_lib.beacon_cache import _beacon_latest_cache_path

        beacon_cache_path = _beacon_latest_cache_path(session_id, state_dir=state_dir)
        with open(beacon_cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "cached_at_unix": time.time(),
                    "data": {
                        "beacon": {
                            "kind": "begin",
                            "eta_seconds": 600,
                            "summary": "working",
                        },
                        "age_seconds": 1,
                    },
                },
                f,
            )
        # Deliberately leave .statusline-bias-cache.json entirely absent --
        # that is the cold cache under test.

        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env.pop("CLAUDE_WALKER_BIN", None)
        payload = json.dumps(
            {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "cwd": _REPO,
                "workspace": {"current_dir": _REPO, "project_dir": _REPO},
                "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
            }
        )
        elapsed, result = _run_cold_render(env, payload)
        if elapsed is None:
            failures.append(
                f"bias-factor cold-cache render exceeded its"
                f" {_COLD_START_FAST_BUDGET_SECONDS * 3}s hard kill"
            )
            return
        if result.returncode != 0:
            failures.append(f"bias-factor cold-cache render exited {result.returncode}")
        if "no begin" not in result.stdout and "turn" not in result.stdout:
            failures.append(
                f"fixture beacon should have rendered a beacon column; got {result.stdout!r}"
            )
        if elapsed > _COLD_START_FAST_BUDGET_SECONDS:
            failures.append(
                f"bias-factor cold-cache render took {elapsed:.1f}s"
                f" (budget {_COLD_START_FAST_BUDGET_SECONDS}s) -- the"
                " calibrated-ETA lookup is blocking the render again"
            )


def main():
    failures = []
    check_first_render_ever_stays_fast(failures)
    check_bias_factor_cold_cache_stays_fast(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: a first-ever render (zero warm caches) and a render hitting"
        " bias-factor's own cold cache both stay fast"
    )


if __name__ == "__main__":
    main()
