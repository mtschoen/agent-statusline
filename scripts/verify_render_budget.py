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
# Evidence 2026-07-11: pre-cache fixture-environment median was ~48-51ms;
# TTL-caching _git_ref and beacons-latest (this machine's measured baseline
# for the ratchet's steps 1+2) dropped that to ~2-3ms median (8 of the 9
# in-process renders hit the warm TTL cache after the first miss), well
# under the <50ms target -- budget lowered to 100ms for headroom. Remaining
# step: the wave-3 async-refresher split (-> <10ms cached path, which is
# also the Pi bridge's keypress budget).
_CORE_BUDGET_MS = float(os.environ.get("STATUSLINE_TEST_CORE_BUDGET_MS", "100"))


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


def check_warm_core_median(failures):
    """Median warm in-process render (the 'core': payload -> rendered string,
    interpreter+imports excluded) must beat _CORE_BUDGET_MS in the fixture
    environment. One child interpreter renders 9 times and reports the
    median, so spawn/import cost and first-render cache warming are excluded
    from the figure -- this is the number the async-refresher work ratchets.

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
            failures.append("warm-core timing child exceeded its hard kill")
            return
        if result.returncode != 0:
            failures.append(
                f"warm-core timing child exited {result.returncode}:"
                f" {result.stderr[-200:]!r}"
            )
            return
        median_ms = float(result.stdout.strip())
        if median_ms > _CORE_BUDGET_MS:
            failures.append(
                f"warm core median {median_ms:.0f}ms exceeds"
                f" {_CORE_BUDGET_MS:.0f}ms -- blocking work crept into the"
                " happy-path render"
            )


def main():
    failures = []
    check_render_path_sync_calls(failures)
    check_cold_render_budget(failures)
    check_warm_core_median(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: render path is free of unbounded sync calls and inside budget")


if __name__ == "__main__":
    main()
