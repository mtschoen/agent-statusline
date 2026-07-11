"""Generic JSON settings-file I/O: load-with-defaults and atomic write.

Shared by every installer that reads a possibly-missing/malformed JSON
settings object and writes it back without a partial-write window (Claude
Code, Antigravity CLI, and Qwen Code settings.json all use this shape; Codex
uses TOML and has its own text-based load/write in install.py).
"""

import json
import os


def load_settings(path):
    """Return parsed dict from `path`, {} if missing/empty, or raise."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} is not a JSON object (top-level type: {type(data).__name__})"
        )
    return data


def atomic_write_settings(path, data):
    """Write `data` to `path` as indented JSON via a same-directory temp file
    + rename, so a crash mid-write never leaves a truncated settings file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
