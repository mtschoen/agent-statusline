"""Walker binary discovery and root resolution.

Imports:
  base  -- for _json_loads (used in _walker_subcommand JSON parse)
"""

import json
import os
import shutil
import subprocess

from .base import _json_loads

_WALKER_BIN_ENV = "CLAUDE_WALKER_BIN"


def _find_walker_binary():
    """Locate the optional native walker. Returns absolute path or None.

    Search order: $CLAUDE_WALKER_BIN, the canonical claude-walker C++ build
    location under $HOME, then PATH (in case the user installed it elsewhere).
    """
    override = os.environ.get(_WALKER_BIN_ENV)
    if override and os.path.isfile(override):
        return override
    home = os.path.expanduser("~")
    for relative in (
        # MSVC multi-config (default on Windows)
        os.path.join("claude-walker", "cpp", "build", "Release", "walker.exe"),
        # Ninja/MinGW single-config on Windows
        os.path.join("claude-walker", "cpp", "build", "walker.exe"),
        # Single-config on Linux/macOS
        os.path.join("claude-walker", "cpp", "build", "walker"),
    ):
        path = os.path.join(home, relative)
        if os.path.isfile(path):
            return path
    # Canonical install name (`claude-walker`) takes precedence over the
    # legacy `walker` lookup so a system-installed binary wins over an old
    # checkout that happens to be on PATH.
    for name in ("claude-walker.exe", "claude-walker", "walker.exe", "walker"):
        which = shutil.which(name)
        if which:
            return which
    return None


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
        # No extra-roots config file is the normal case; use the defaults.
        pass
    except (OSError, ValueError) as exc:
        # A malformed config IS worth surfacing (unlike the missing-file case
        # above); logging.warning routes to stderr even with no handler set.
        import logging

        logging.getLogger(__name__).warning(
            "statusline_lib: ignoring malformed %s: %s",
            _WALKER_ROOTS_CONFIG_PATH,
            exc,
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


def _walker_subcommand(subcommand, *args, timeout=2):
    """Invoke a claude-walker subcommand. Return parsed JSON or None on any error."""
    bin_path = _find_walker_binary()
    if not bin_path:
        return None
    try:
        result = subprocess.run(
            [bin_path, subcommand, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return _json_loads(result.stdout)
    except (ValueError, TypeError):
        return None
