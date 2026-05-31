"""Stand-alone verifier for statusline_lib._walker_root_list and
_walk_pace_hourly multi-root behavior.

Run:
    python scripts/verify_multi_root_fallback.py

Builds a tmp filesystem layout, points HOME at it, asserts dollar totals across
both the default and an extra root.  Cleans up on exit even on failure.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def make_jsonl(path, model, output_tokens):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    entry = {
        "timestamp": iso,
        "message": {
            "role": "assistant",
            "id": f"msg-{os.path.basename(path)}",
            "model": model,
            "usage": {
                "input_tokens": 0,
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
    pace_module = None
    original_now_unix = None
    try:
        os.environ["HOME"] = tmp
        os.environ["USERPROFILE"] = tmp

        default_root = os.path.join(tmp, ".claude", "projects")
        extra_root = os.path.join(tmp, "extra-projects")

        # 1M claude-opus-4-8 output tokens = $25.00 per turn.
        # Use two turns in separate roots to verify multi-root aggregation.
        make_jsonl(
            os.path.join(default_root, "slug-default", "sess-d.jsonl"),
            "claude-opus-4-8",
            1_000_000,
        )  # $25.00
        make_jsonl(
            os.path.join(extra_root, "slug-extra", "sess-e.jsonl"),
            "claude-opus-4-8",
            1_000_000,
        )  # $25.00

        config_path = os.path.join(tmp, ".claude", "walker-roots.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"extra_roots": [extra_root]}, f)

        # Reload the package so module-level path constants pick up the
        # patched HOME/USERPROFILE set above.
        for mod_name in list(sys.modules):
            if mod_name == "statusline_lib" or mod_name.startswith("statusline_lib."):
                del sys.modules[mod_name]
        import statusline_lib
        import statusline_lib.pace as pace_module

        roots = statusline_lib._walker_root_list()
        assert os.path.realpath(default_root) in roots, f"default root missing: {roots}"
        assert os.path.realpath(extra_root) in roots, f"extra root missing: {roots}"

        # Win start far enough in the past that both transcripts (timestamped
        # "now") fall inside the window.
        win_start = datetime.now(UTC).timestamp() - 3600

        # Pin _now_unix so n_buckets covers the window.
        original_now_unix = pace_module._now_unix
        pace_module._now_unix = lambda: win_start + 3600

        hourly = pace_module._walk_pace_hourly(win_start)

        total = sum(hourly)
        expected = 50.0
        assert abs(total - expected) < 0.01, (
            f"multi-root total got ${total:.4f}, expected ${expected:.4f}"
        )
        print(
            f"OK: multi-root total=${total:.4f} across {len(hourly)} hourly bucket(s)"
        )
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_userprofile
        if pace_module is not None and original_now_unix is not None:
            pace_module._now_unix = original_now_unix
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
