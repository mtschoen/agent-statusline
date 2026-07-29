"""Structural guards against long-running sync work in the render path.

Three production incidents, one disease: a synchronous call inside a render
that can block for many seconds (2026-07-02: SMB per-file stats + walker
timeout stalls, 20s renders; 2026-07-10: psutil attr expansion, 11s renders;
2026-07-11: beacons-history over an SMB root, 5s timeout stalls). These
checks make the invariant mechanical instead of tribal:

  - Static: every subprocess call reachable from a render must carry an
    explicit numeric ``timeout=`` no greater than ``_MAX_SUBPROCESS_TIMEOUT``
    seconds, whether passed at the call site or defaulted in the wrapper.
    ``time.sleep`` is banned outright in the render path.
  - Dynamic: a cold-cache end-to-end render against a self-built fixture
    corpus must finish inside ``_RENDER_BUDGET_SECONDS`` wall-clock. The
    budget is deliberately loose (healthy renders are ~10x faster) so CI
    variance never trips it, while every historical incident (11-20s) would.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.rendertimer import render_timer_path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The render path: everything importable from a statusline render. install.py
# and friends are excluded -- installers may run long.
_RENDER_PATH_FILES = [
    os.path.join(_REPO, "statusline.py"),
    os.path.join(_REPO, "subagent_statusline.py"),
    os.path.join(_REPO, "qwen_statusline.py"),
    os.path.join(_REPO, "wrap_nudge.py"),
]
_RENDER_PATH_FILES += [
    os.path.join(_REPO, "statusline_lib", f)
    for f in sorted(os.listdir(os.path.join(_REPO, "statusline_lib")))
    if f.endswith(".py")
    and f
    not in (
        "codex_install.py",
        "nudge_install.py",
        # process_safe.py implements the bounded-timeout replacement for
        # subprocess.run/Popen this scan exists to enforce elsewhere (its
        # Popen call is the sanctioned kill-then-abandon-reader pattern, not
        # the unbounded raw usage the ban targets) -- scanning it would flag
        # the fix as the violation.
        "process_safe.py",
    )
]

_MAX_SUBPROCESS_TIMEOUT = 2.0
_RENDER_BUDGET_SECONDS = float(os.environ.get("STATUSLINE_TEST_RENDER_BUDGET", "8"))
# Warm-core conformance: median in-process render (payload -> string, caches
# warm, fixture corpus) must beat this. Ratchet plan lives in PLAN.md.
# Evidence 2026-07-11 (steps 1+2, TTL-caching _git_ref and beacons-latest):
# pre-cache median ~48-51ms -> ~2-3ms, budget lowered to 100ms for headroom.
# Evidence 2026-07-19 (step 3, PLAN.md: git-ref/beacons-latest/session-count
# moved from "TTL-cached but a miss still recomputes inline" onto the same
# stale-while-revalidate + detached-refresher pattern as the pace/spend
# walks -- statusline_lib/refresh.py -- so a cache miss or TTL expiry never
# blocks the render on git, the walker subprocess, or a psutil process-tree
# scan, which measured ~120ms uncached): this machine's measured median
# across 30 repeated in-process runs is ~1-6ms (25-sample batch: min 1.09ms,
# median 2.02ms, p90 4.60ms, max 5.95ms), with 30/30 repeated runs of this
# exact check passing at the 10ms budget -- ~2-5x margin over the observed
# median. Budget lowered to the Pi bridge's per-keypress target, 10ms.
_CORE_BUDGET_MS = float(os.environ.get("STATUSLINE_TEST_CORE_BUDGET_MS", "10"))
# One 9-render child is a single sample, and CI runners share cores with other
# jobs: runs #113 (2026-07-19) and #115 (2026-07-28) both reported "warm core
# median 11ms exceeds 10ms" while runs #114/#116 and every local run passed at
# ~2-5ms. Take the BEST of this many independent child measurements instead of
# trusting one. A real regression (blocking work back in the render path) is
# reproducible and misses the budget on every attempt; a scheduler blip spoils
# one. The loop stops at the first attempt inside budget, so the healthy case
# still spawns exactly one child.
_CORE_MEDIAN_ATTEMPTS = 3


def _numeric_value(node):
    """Return the numeric value of a Constant/negated-Constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    return None


def _subprocess_timeout_violations(path):
    """Yield (lineno, message) for subprocess calls without a bounded timeout."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # A wrapper that takes timeout as a parameter must bound its DEFAULT.
            args = node.args
            defaults = dict(
                zip(
                    [a.arg for a in args.args[len(args.args) - len(args.defaults) :]],
                    args.defaults,
                    strict=True,
                )
            )
            kwdefaults = {
                a.arg: d
                for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if d is not None
            }
            for name, default in {**defaults, **kwdefaults}.items():
                if name == "timeout":
                    val = _numeric_value(default)
                    if val is None or val > _MAX_SUBPROCESS_TIMEOUT:
                        yield (
                            node.lineno,
                            f"{node.name}() defaults timeout={ast.dump(default)}"
                            f" (must be numeric <= {_MAX_SUBPROCESS_TIMEOUT})",
                        )
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in ("run", "check_output", "check_call", "call", "Popen")
        )
        if is_subprocess and func.attr == "Popen":
            yield (node.lineno, "subprocess.Popen is banned in the render path")
            continue
        timeout_kw = next((k for k in node.keywords if k.arg == "timeout"), None)
        if is_subprocess:
            if timeout_kw is None:
                yield (node.lineno, f"subprocess.{func.attr} without timeout=")
                continue
            val = _numeric_value(timeout_kw.value)
            # A Name (forwarded parameter) is allowed: the wrapper's default
            # is checked above, and explicit call-site overrides are caught
            # by the constant check below when literal.
            if isinstance(timeout_kw.value, ast.Name):
                continue
            if val is None or val > _MAX_SUBPROCESS_TIMEOUT:
                yield (
                    node.lineno,
                    f"subprocess.{func.attr} timeout must be numeric <="
                    f" {_MAX_SUBPROCESS_TIMEOUT}",
                )
        elif timeout_kw is not None:
            # Any other call passing a literal timeout (e.g. a walker wrapper)
            # must also stay within the cap.
            val = _numeric_value(timeout_kw.value)
            if val is not None and val > _MAX_SUBPROCESS_TIMEOUT:
                yield (
                    node.lineno,
                    f"call passes timeout={val} > {_MAX_SUBPROCESS_TIMEOUT}",
                )
        is_sleep = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "time"
            and func.attr == "sleep"
        )
        if is_sleep:
            yield (node.lineno, "time.sleep is banned in the render path")


def check_render_path_sync_calls(failures):
    for path in _RENDER_PATH_FILES:
        rel = os.path.relpath(path, _REPO)
        for lineno, msg in _subprocess_timeout_violations(path):
            failures.append(f"{rel}:{lineno}: {msg}")


def _build_fixture_home(root, n_sessions=8, turns_per_session=40):
    """Synthetic ~/.claude with enough transcript bulk to make walks real."""
    projects = os.path.join(root, ".claude", "projects", "C--fixture-proj")
    os.makedirs(projects, exist_ok=True)
    now_iso = "2026-07-11T00:00:00.000Z"
    for _ in range(n_sessions):
        sid = str(uuid.uuid4())
        lines = []
        for t in range(turns_per_session):
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": now_iso,
                        "message": {
                            "model": "claude-opus-4-8",
                            "usage": {
                                "input_tokens": 10 + t,
                                "output_tokens": 20 + t,
                                "cache_read_input_tokens": 1000,
                                "cache_creation_input_tokens": 50,
                            },
                        },
                    }
                )
            )
        with open(os.path.join(projects, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return projects


def check_cold_render_budget(failures):
    """End-to-end render with cold caches and a fixture corpus must finish
    inside the budget. Every historical incident (11-20s) violates this;
    healthy renders are ~10x under it.

    ignore_cleanup_errors: a render with cold caches spawns a detached
    refresh child (statusline_lib/refresh.py) that briefly holds the fixture
    transcripts open; on Windows an open file can't be unlinked, so teardown
    racing a straggler child raises WinError 32. The leftover tempdir is a
    few KB and the OS temp cleaner's problem; the check's assertions are
    unaffected."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = os.path.join(tmp, "home")
        _build_fixture_home(home)
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
        start = time.perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(_REPO, "statusline.py")],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=_RENDER_BUDGET_SECONDS * 3,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                f"cold render exceeded {_RENDER_BUDGET_SECONDS * 3}s hard kill"
            )
            return
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            failures.append(f"cold render exited {result.returncode}")
        if elapsed > _RENDER_BUDGET_SECONDS:
            failures.append(
                f"cold render took {elapsed:.1f}s"
                f" (budget {_RENDER_BUDGET_SECONDS}s) -- a long sync call is"
                " back in the render path"
            )


# Tighter than _RENDER_BUDGET_SECONDS (8s, an incident-level "did it hang"
# check): a first render that misses every cache at once should still be
# dominated by fast local I/O plus a handful of fire-and-forget detached-
# refresh spawns, not by waiting on any subprocess result. 3s gives ~10x
# headroom over the observed ~0.1-0.3s cold render while still catching a
# regression where something blocks on even one 2s-capped subprocess call.
_COLD_START_FAST_BUDGET_SECONDS = 3.0


def _run_cold_render(env, payload, hard_kill_multiplier=3):
    """Shared subprocess-render helper for the two cold-start checks below.
    Returns (elapsed_seconds, CompletedProcess) or (None, None) on a hard
    kill (the caller appends the failure)."""
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
        _build_fixture_home(home, n_sessions=1, turns_per_session=1)
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
        projects = _build_fixture_home(home, n_sessions=1, turns_per_session=1)
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


_CORE_TIMER_SNIPPET = """
import contextlib, io, json, sys, time
sys.path.insert(0, {repo!r})
import statusline
payload = {payload!r}
times = []
for i in range(9):
    sys.stdin = io.StringIO(payload)
    with contextlib.redirect_stdout(io.StringIO()):
        t0 = time.perf_counter()
        with contextlib.suppress(SystemExit):
            statusline.main()
        times.append((time.perf_counter() - t0) * 1000)
times.sort()
print(times[len(times) // 2])
"""


def _measure_warm_core_median(code, env):
    """Run one warm-core timing child and return (median_ms, error_message).

    Exactly one of the two is None. Callers retry on a slow-but-valid
    measurement (see _CORE_MEDIAN_ATTEMPTS) but abort on an error, which
    signals a broken child rather than a loaded runner.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=_RENDER_BUDGET_SECONDS * 6,
        )
    except subprocess.TimeoutExpired:
        return None, "warm-core timing child exceeded its hard kill"
    if result.returncode != 0:
        return None, (
            f"warm-core timing child exited {result.returncode}:"
            f" {result.stderr[-200:]!r}"
        )
    return float(result.stdout.strip()), None


def check_warm_core_median(failures):
    """Median warm in-process render (the 'core': payload -> rendered string,
    interpreter+imports excluded) must beat _CORE_BUDGET_MS in the fixture
    environment. Each child interpreter renders 9 times and reports the
    median, so spawn/import cost and first-render cache warming are excluded
    from the figure -- this is the number the async-refresher work ratchets.
    The best of up to _CORE_MEDIAN_ATTEMPTS such children is the verdict; see
    that constant for why one sample is not enough on a shared CI runner.

    The child calls statusline.main() directly (see _CORE_TIMER_SNIPPET), never
    the `if __name__ == "__main__":` block -- so record_render() (which WRITES
    the render-timer state) never runs here, on any of the 9 renders. Left
    unaddressed, format_render_suffix()'s read always hit the "no prior state"
    branch, so this benchmark never paid for the warm json.load() a real second
    render does. Seeding one render-timer entry up front (using rendertimer's
    own path function, not a re-derived path, so this can't drift from the
    production layout) makes every one of the 9 in-process renders exercise
    the real warm-read branch.

    ignore_cleanup_errors: same detached-refresh-child teardown race as
    check_cold_render_budget (WinError 32 on Windows; see that docstring).
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = os.path.join(tmp, "home")
        _build_fixture_home(home)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env.pop("CLAUDE_WALKER_BIN", None)

        session_id = str(uuid.uuid4())
        state_dir = os.path.join(home, ".claude", "state")
        os.makedirs(state_dir, exist_ok=True)
        seed_path = render_timer_path(session_id, state_dir=state_dir)
        with open(seed_path, "w", encoding="utf-8") as f:
            json.dump({"last_ms": 5.0, "peak_ms": 5.0}, f)

        payload = json.dumps(
            {
                "session_id": session_id,
                "cwd": _REPO,
                "workspace": {"current_dir": _REPO, "project_dir": _REPO},
                "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
                "context_window": {
                    "context_window_size": 200000,
                    "total_input_tokens": 50000,
                    "total_output_tokens": 5000,
                    "current_usage": {
                        "input_tokens": 10,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 40000,
                    },
                },
                "cost": {
                    "total_cost_usd": 1.5,
                    "total_duration_ms": 600000,
                    "total_api_duration_ms": 300000,
                    "total_lines_added": 10,
                    "total_lines_removed": 2,
                },
            }
        )
        code = _CORE_TIMER_SNIPPET.format(repo=_REPO, payload=payload)
        best_ms = None
        for _ in range(_CORE_MEDIAN_ATTEMPTS):
            median_ms, error = _measure_warm_core_median(code, env)
            if error is not None:
                failures.append(error)
                return
            if best_ms is None or median_ms < best_ms:
                best_ms = median_ms
            if best_ms <= _CORE_BUDGET_MS:
                break
        if best_ms > _CORE_BUDGET_MS:
            failures.append(
                f"warm core median {best_ms:.0f}ms (best of"
                f" {_CORE_MEDIAN_ATTEMPTS} attempts) exceeds"
                f" {_CORE_BUDGET_MS:.0f}ms -- blocking work crept into the"
                " happy-path render"
            )


def main():
    failures = []
    check_render_path_sync_calls(failures)
    check_cold_render_budget(failures)
    check_first_render_ever_stays_fast(failures)
    check_bias_factor_cold_cache_stays_fast(failures)
    check_warm_core_median(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: render path is free of unbounded sync calls, inside budget, and"
        " cold-start (first-ever render, and bias-factor's own cold cache)"
        " stays fast"
    )


if __name__ == "__main__":
    main()
