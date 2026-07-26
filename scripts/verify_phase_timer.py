"""Verify statusline_lib.rendertimer.PhaseTimer and its module-level
start_phase_timer()/current_phase_timer() pair -- the per-render diagnostic
checkpoint accumulator added 2026-07-26 after a real 5.8s slow-render log
entry had to be root-caused from scratch across five separate cache/state
files with no per-phase evidence in the log itself. statusline.py's main()
feeds these into _log_slow_render's breakdown; this file covers the
statusline_lib side (the 100%-coverage-gated half) in isolation.

Split out of verify_render_timer.py (which owns the separate previous-
render/peak-tracking concern -- see that module's docstring) once this
suite's growth pushed the combined file over aislop's file-size gate.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import statusline_lib.refresh as refresh
from statusline_lib.process_safe import run_captured
from statusline_lib.rendertimer import (
    PhaseTimer,
    current_phase_timer,
    start_phase_timer,
)


def check_phase_timer_empty_breakdown(failures):
    if PhaseTimer().breakdown() != "":
        failures.append("a PhaseTimer with no entries must render an empty breakdown")


def check_phase_timer_mark_sequence(failures):
    timer = PhaseTimer()
    timer._last -= 0.10  # pin the first interval so mark() has something to measure
    timer.mark("walk")
    timer._last -= 0.05
    timer.mark("gitref")
    timer.mark("beacon")  # a zero-ish interval must still produce a valid entry
    breakdown = timer.breakdown()
    parts = breakdown.split(", ")
    if len(parts) != 3:
        failures.append(f"expected 3 breakdown entries, got {parts!r}")
    if not parts[0].startswith("walk=0.1") or not parts[0].endswith("s"):
        failures.append(f"walk entry should read ~0.10s, got {parts[0]!r}")
    if not parts[1].startswith("gitref=0.0"):
        failures.append(f"gitref entry should read ~0.05s, got {parts[1]!r}")
    if not parts[2].startswith("beacon=0.0"):
        failures.append(f"beacon entry should be near-zero, got {parts[2]!r}")


def check_phase_timer_mark_detail(failures):
    timer = PhaseTimer()
    timer.mark("beacon", detail="bias-refresh-spawned")
    if (
        "beacon=" not in timer.breakdown()
        or "[bias-refresh-spawned]" not in timer.breakdown()
    ):
        failures.append(
            f"mark() detail must render as a bracketed suffix; got {timer.breakdown()!r}"
        )
    timer2 = PhaseTimer()
    timer2.mark("beacon", detail=None)
    if "[" in timer2.breakdown():
        failures.append(
            f"mark() with detail=None must not render a bracket; got {timer2.breakdown()!r}"
        )


def check_phase_timer_record(failures):
    timer = PhaseTimer()
    timer.record("spawns", 1.8, detail="3x")
    if timer.breakdown() != "spawns=1.80s[3x]":
        failures.append(
            f"record() must format as name=elapsed[detail]; got {timer.breakdown()!r}"
        )


def check_phase_timer_mixed_mark_and_record(failures):
    timer = PhaseTimer()
    timer.mark("walk")
    timer.record("spawns", 0.5, detail="2x")
    timer.mark("beacon")
    parts = timer.breakdown().split(", ")
    if len(parts) != 3 or not parts[1].startswith("spawns=0.50s"):
        failures.append(
            f"record() must interleave with mark() entries in call order; got {parts!r}"
        )


def check_start_phase_timer_returns_and_stores(failures):
    """start_phase_timer() both returns a fresh PhaseTimer and stashes it as
    the module's "current" one -- the __main__ block (which never holds
    main()'s local variable) reaches it later via current_phase_timer()."""
    timer = start_phase_timer()
    if not isinstance(timer, PhaseTimer):
        failures.append(f"start_phase_timer() must return a PhaseTimer; got {timer!r}")
    if current_phase_timer() is not timer:
        failures.append(
            "current_phase_timer() must return the same instance start_phase_timer() returned"
        )

    second = start_phase_timer()
    if current_phase_timer() is not second or current_phase_timer() is timer:
        failures.append(
            "a second start_phase_timer() call must replace the stored timer"
        )


def check_start_phase_timer_resets_spawn_log(failures):
    """start_phase_timer() must clear refresh.py's per-render spawn log --
    a prior render's spawns must never bleed into the next one's breakdown."""
    with tempfile.TemporaryDirectory() as tmp:
        saved_path = refresh._INFLIGHT_PATH
        saved_spawn = refresh.spawn_detached
        refresh._INFLIGHT_PATH = os.path.join(tmp, "inflight.json")
        refresh.spawn_detached = lambda command: None
        try:
            refresh.maybe_spawn_refresh("git-ref", "/some/repo")
            if not refresh.spawn_timings():
                failures.append("setup: expected a spawn to be recorded before reset")
            start_phase_timer()
            if refresh.spawn_timings():
                failures.append(
                    f"start_phase_timer() must reset the spawn log; got {refresh.spawn_timings()!r}"
                )
        finally:
            refresh._INFLIGHT_PATH = saved_path
            refresh.spawn_detached = saved_spawn


def check_current_phase_timer_before_start(failures):
    """current_phase_timer() reads as None until start_phase_timer() has
    ever run in this process -- exercised by importing rendertimer fresh in
    a subprocess so no earlier check in this suite has already set it."""
    code = (
        f"import sys; sys.path.insert(0, {REPO!r}); "
        "from statusline_lib.rendertimer import current_phase_timer; "
        "print(current_phase_timer())"
    )
    result = run_captured([sys.executable, "-c", code], timeout=30)
    if result.stdout.strip() != "None":
        failures.append(
            f"current_phase_timer() before any start_phase_timer() call should be"
            f" None; got {result.stdout!r} (stderr: {result.stderr!r})"
        )


def main():
    failures = []
    for check in (
        check_phase_timer_empty_breakdown,
        check_phase_timer_mark_sequence,
        check_phase_timer_mark_detail,
        check_phase_timer_record,
        check_phase_timer_mixed_mark_and_record,
        check_start_phase_timer_returns_and_stores,
        check_start_phase_timer_resets_spawn_log,
        check_current_phase_timer_before_start,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: PhaseTimer mark/record/breakdown and start_phase_timer/"
        "current_phase_timer all verified"
    )


if __name__ == "__main__":
    main()
