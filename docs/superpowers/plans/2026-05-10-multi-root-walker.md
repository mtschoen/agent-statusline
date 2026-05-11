# Multi-root walker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `claude-walker` (native C++ binary) and the Python fallback in `statusline_lib.py` to walk a configurable set of project roots so the sliding-window pace projection sees both machines' transcripts when the same subscription is used from chonkers and llamabox.

**Architecture:** Adds a per-host config file `~/.claude/walker-roots.json` listing extra project roots. The walker (cost mode, beacons-history, beacons-latest) merges the default root with extras at discovery time, deduped via `fs::canonical`, and walks them all under the same grouping/dedup rules. Aggregated output shape is unchanged so callers (statusline, conformance) don't have to know multi-root exists. The Python fallback mirrors the same config-file read.

**Tech Stack:** C++17 + simdjson (`~/claude-walker/cpp/`), Python 3 (`~/schoen-claude-status/statusline_lib.py`), JSON config, existing conformance harness at `~/claude-walker/shared/conformance.py`.

**Cross-repo scope:** The work spans two git repos:
- `C:\Users\mtsch\claude-walker` — native binary, SPEC, conformance, fixtures.
- `C:\Users\mtsch\schoen-claude-status` — Python fallback.

Each phase commits in its own repo. Cross-repo coordination point: Phase 7 verifies the integrated behavior end-to-end.

## Phase 7: Python fallback in statusline_lib.py

### Task 7.1: Add `_walker_root_list()` helper

**Files:**
- Modify: `C:\Users\mtsch\schoen-claude-status\statusline_lib.py`

- [x] **Step 1: Add the helper near the other private file helpers**

Insert after `_find_walker_binary()` (around line 568):

```python
_WALKER_ROOTS_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "walker-roots.json"
)


def _walker_root_list():
    """Default root + extras from walker-roots.json. Mirrors C++ resolve_roots.

    Failure modes match the SPEC: missing file => no extras; malformed JSON =>
    stderr message + no extras. Only directories that exist on disk make it
    into the result. Realpath-deduped.
    """
    home = os.path.expanduser("~")
    default = os.path.join(home, ".claude", "projects")
    all_paths = [default]
    try:
        with open(_WALKER_ROOTS_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        extras = cfg.get("extra_roots") or []
        if isinstance(extras, list):
            all_paths.extend(str(p) for p in extras if isinstance(p, str) and p)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        # Malformed JSON: log, fall through. Don't crash the statusline.
        print(
            f"statusline_lib: ignoring malformed {_WALKER_ROOTS_CONFIG_PATH}: {exc}",
            file=__import__("sys").stderr,
        )

    seen = set()
    result = []
    for p in all_paths:
        try:
            canon = os.path.realpath(p)
        except OSError:
            canon = os.path.normpath(p)
        if not os.path.isdir(canon):
            continue
        if canon in seen:
            continue
        seen.add(canon)
        result.append(canon)
    return result
```

### Task 7.2: Use the helper in `_walk_pace_buckets`

**Files:**
- Modify: `C:\Users\mtsch\schoen-claude-status\statusline_lib.py` (`_walk_pace_buckets` at lines 667-760)

- [x] **Step 1: Replace the `proj_root` lookup + discovery glob loop**

Inside `_walk_pace_buckets`, replace lines 694-724 (from `home = os.path.expanduser("~")` through the second `for path in glob.glob(sub_pattern):` loop) with a roots-loop wrapper:

```python
    roots = _walker_root_list()
    if not roots:
        return 0.0, 0.0
    now = datetime.now(timezone.utc).timestamp()
    period_cutoff = now - period_seconds
    earliest = min(period_cutoff, win_start_unix)

    # Group by (slug, session_id) so each work unit owns its own dedup set.
    groups = {}
    for proj_root in roots:
        for path in glob.glob(os.path.join(proj_root, "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < earliest:
                    continue
            except OSError:
                continue
            slug = os.path.basename(os.path.dirname(path))
            session_id = os.path.splitext(os.path.basename(path))[0]
            groups.setdefault((slug, session_id), []).append(path)
        sub_pattern = os.path.join(proj_root, "*", "*", "subagents", "agent-*.jsonl")
        for path in glob.glob(sub_pattern):
            try:
                if os.path.getmtime(path) < earliest:
                    continue
            except OSError:
                continue
            sub_dir = os.path.dirname(path)
            session_dir = os.path.dirname(sub_dir)
            session_id = os.path.basename(session_dir)
            slug = os.path.basename(os.path.dirname(session_dir))
            groups.setdefault((slug, session_id), []).append(path)
```

(The rest of the function — parallel pool dispatch — is unchanged.)

### Task 7.3: Bump pace-cache filename to invalidate stale entries

**Files:**
- Modify: `C:\Users\mtsch\schoen-claude-status\statusline_lib.py`

- [x] **Step 1: Rename the constant**

Replace line 488-489 (the `_PACE_CACHE_PATH = ...` assignment):

```python
_PACE_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-pace-cache-v2.json"
)
```

Why v2: the v1 cache key didn't include roots, so a stale v1 entry on disk would serve incorrect numbers (single-root sums) until the TTL expired. The rename forces a one-time miss.

### Task 7.4: Add a small Python self-test script

**Files:**
- Create: `C:\Users\mtsch\schoen-claude-status\.claude\scripts\verify_multi_root_fallback.py`

- [x] **Step 1: Write the test**

The test sets HOME (USERPROFILE on Windows) to a tmp dir so `~` expansion lands inside that tmp world. Builds a default-root + extra-root layout there, drops a `walker-roots.json` next to them, runs the walker pace function, asserts the dollar total.

```python
"""Stand-alone verifier for statusline_lib._walker_root_list and
_walk_pace_buckets multi-root behavior.

Run:
    python .claude/scripts/verify_multi_root_fallback.py

Builds a tmp filesystem layout, points HOME at it, asserts dollar totals.
Cleans up on exit even on failure.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def make_jsonl(path, model, input_tokens, output_tokens):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = {
        "timestamp": iso,
        "message": {
            "role": "assistant",
            "id": f"msg-{os.path.basename(path)}",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    tmp = tempfile.mkdtemp(prefix="walker-fallback-test-")
    old_home = os.environ.get("HOME")
    old_userprofile = os.environ.get("USERPROFILE")
    try:
        # Point ~ at tmp so default-root resolution lands in our sandbox.
        os.environ["HOME"] = tmp
        os.environ["USERPROFILE"] = tmp

        default_root = os.path.join(tmp, ".claude", "projects")
        extra_root = os.path.join(tmp, "extra-projects")

        make_jsonl(
            os.path.join(default_root, "slug-default", "sess-d.jsonl"),
            "claude-opus-4-7", 1000, 500,
        )  # $0.0175
        make_jsonl(
            os.path.join(extra_root, "slug-extra", "sess-e.jsonl"),
            "claude-sonnet-4-6", 2000, 1000,
        )  # $0.021

        config_path = os.path.join(tmp, ".claude", "walker-roots.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"extra_roots": [extra_root]}, f)

        # Import (or reload) statusline_lib AFTER HOME is reset so any
        # module-level path resolution uses the sandboxed home.
        if "statusline_lib" in sys.modules:
            importlib.reload(sys.modules["statusline_lib"])
        import statusline_lib

        roots = statusline_lib._walker_root_list()
        assert os.path.realpath(default_root) in roots, \
            f"default root missing: {roots}"
        assert os.path.realpath(extra_root) in roots, \
            f"extra root missing: {roots}"

        now = datetime.now(timezone.utc).timestamp()
        trailing, window = statusline_lib._walk_pace_buckets(
            period_seconds=604800,
            win_start_unix=now - 86400,
        )

        expected = 0.0175 + 0.021
        assert abs(trailing - expected) < 0.001, \
            f"trailing got ${trailing:.4f}, expected ${expected:.4f}"
        print(f"OK: trailing=${trailing:.4f} window=${window:.4f}")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_userprofile
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Run the test**

Run: `python C:\Users\mtsch\schoen-claude-status\.claude\scripts\verify_multi_root_fallback.py`
Expected: `OK: trailing=$0.0385 window=$0.0210` (or similar — the precise window value depends on `win_start_unix` vs entry timestamp).

- [x] **Step 3: Commit**

```bash
cd C:\Users\mtsch\schoen-claude-status
git add statusline_lib.py .claude/scripts/verify_multi_root_fallback.py
git commit -m "feat: multi-root walker support in Python fallback"
```

---

## Phase 8: End-to-end verification

Confirm the production wiring works on the dev box.

### Task 8.1: Live verify on dev box

- [ ] **Step 1: Write a real walker-roots.json**

Create `C:\Users\mtsch\.claude\walker-roots.json`:

```json
{
  "extra_roots": [
    "Y:\\.claude\\projects"
  ]
}
```

(`Y:\` is the SMB mount of llamabox on chonkers per the user's CLAUDE.md.)

- [ ] **Step 2: Run the walker manually**

Run: `C:\Users\mtsch\claude-walker\cpp\build\Release\walker.exe --period 604800 --win-start 0`
Expected: exit 0, JSON line. `files_walked` count is higher than the no-extras baseline (capture the baseline by running once with `--no-config` for comparison).

- [ ] **Step 3: Confirm bias-factor recompute**

Run: `C:\Users\mtsch\claude-walker\cpp\build\Release\walker.exe beacons-history --period 604800 --win-start 0`
Expected: `session_count` is higher than `--no-config` run. `bias_factor` may change.

- [ ] **Step 4: Verify statusline picks it up**

Open a fresh Claude Code session, observe the `wk:` line. Compared to a session before this change, the projected `+Xh` should reflect both machines' burn (typically a tighter / more conservative projection if both machines were active).

- [ ] **Step 5: Remove the local config (cleanup if testing only) OR leave it in place**

Decision is the user's at runtime — the walker-roots.json is the deployment artifact, not a code change.

---

## Self-review notes

Spec coverage check:
- **Problem / Architecture sections** of the spec → covered by Phases 1-7 (walker changes) and Phase 7 (Python fallback).
- **Config schema** → Phase 1 implementation + Phase 5 SPEC.md docs.
- **Failure modes** (missing/malformed/unreachable) → Phase 1 helper + Phase 6 fixture 11 (unreachable).
- **CLI flags** (`--extra-projects-root`, `--no-config`) → Phases 2/3/4 (one per subcommand).
- **Effective root set** order → Phase 1 helper + Phase 5 docs.
- **Discovery & walking** → Phases 2/3/4 sub-tasks.
- **SPEC.md update** → Phase 5.
- **Python fallback** → Phase 7.
- **Conformance & tests** → Phase 6.
- **Risks: mount latency on the hot path** → flagged in spec as deferred. Not addressed in this plan; would be a follow-up if symptoms appear.

Type / naming consistency:
- `walker::resolve_roots(primary, cli_extras, read_config)` — same signature used from `main.cpp:run_cost`, `beacons.cpp:run_history`, `beacons.cpp:run_latest`.
- `--no-config` and `--extra-projects-root` spelled consistently across three argument parsers (cost, history, latest).
- Python `_walker_root_list()` mirrors C++ behavior; cache filename bumped to `_v2` to invalidate stale entries.

Out-of-scope reminders (also flagged in spec):
- `cost-estimator` skill is NOT updated here. Retrospective spend analysis remains single-machine until a follow-up.
