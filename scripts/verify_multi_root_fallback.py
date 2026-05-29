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
from datetime import UTC, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def make_jsonl(path, model, input_tokens, output_tokens):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
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
        os.environ["HOME"] = tmp
        os.environ["USERPROFILE"] = tmp

        default_root = os.path.join(tmp, ".claude", "projects")
        extra_root = os.path.join(tmp, "extra-projects")

        make_jsonl(
            os.path.join(default_root, "slug-default", "sess-d.jsonl"),
            "claude-opus-4-7",
            1000,
            500,
        )  # $0.0175
        make_jsonl(
            os.path.join(extra_root, "slug-extra", "sess-e.jsonl"),
            "claude-sonnet-4-6",
            2000,
            1000,
        )  # $0.021

        config_path = os.path.join(tmp, ".claude", "walker-roots.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"extra_roots": [extra_root]}, f)

        if "statusline_lib" in sys.modules:
            importlib.reload(sys.modules["statusline_lib"])
        import statusline_lib

        # Force the Python fallback path — don't let the native walker
        # point at the user's real ~/.claude.  Patch AFTER import/reload so
        # we're mutating the live module object that _walk_pace_buckets
        # will call.
        _original_find = statusline_lib._find_walker_binary
        statusline_lib._find_walker_binary = lambda: None

        roots = statusline_lib._walker_root_list()
        assert os.path.realpath(default_root) in roots, f"default root missing: {roots}"
        assert os.path.realpath(extra_root) in roots, f"extra root missing: {roots}"

        now = datetime.now(UTC).timestamp()
        trailing, window = statusline_lib._walk_pace_buckets(
            period_seconds=604800,
            win_start_unix=now - 86400,
        )

        expected = 0.0175 + 0.021
        assert abs(trailing - expected) < 0.001, (
            f"trailing got ${trailing:.4f}, expected ${expected:.4f}"
        )
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
        try:
            # Restore the real finder so subsequent imports in the same process
            # don't see the patched no-op.
            _sl = sys.modules.get("statusline_lib")
            if _sl is not None and "_original_find" in dir():
                _sl._find_walker_binary = _original_find  # type: ignore[attr-defined]
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
