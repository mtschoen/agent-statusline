"""Qwen Code statusline entry point. Reads Qwen Code's JSON payload from stdin
and prints two lines:
  line 1: [host] cwd (branch)
  line 2: model | ctx | cache | tokens | api | files | previous-render duration + peak

Adapted from statusline.py for Qwen Code's payload structure.
"""

import contextlib
import json
import os
import socket
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from statusline_lib import (
    RED,
    RESET,
    count_active_sessions,
    debounce_session_count,
    format_context,
    format_model_badge,
    format_qwen_api_stats,
    format_qwen_cache,
    format_qwen_files,
    format_qwen_thinking,
    format_qwen_tokens,
)
from statusline_lib.rendertimer import format_render_suffix, record_render

_INPUT_LOG = os.path.expanduser("~/.qwen/.qwen-statusline-input.log")
_RAW_LOG = os.path.expanduser("~/.qwen/.qwen-statusline-input-raw.json")
_ERROR_LOG = os.path.expanduser("~/.qwen/.qwen-statusline-error.log")

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _safe_write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        # Best-effort debug dump: a failed log write must never break the statusline.
        pass


def _hostname():
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


def _line1(d, cwd, spinner):
    host = _hostname()
    line1 = f"{spinner} [{host}] {cwd}"
    n_sessions = debounce_session_count(count_active_sessions(cwd), cwd)
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    branch = (d.get("git") or {}).get("branch") or ""
    if branch:
        line1 = f"{line1} ({branch})"
    return line1


def _model_summaries(models):
    """Aggregate Qwen's per-model token/api counters into the cache, tokens,
    thinking, and API summaries (Qwen usually reports a single model)."""
    all_tokens = {}
    all_api = {}
    for model_data in models.values():
        if not isinstance(model_data, dict):
            continue
        tokens = model_data.get("tokens") or {}
        api = model_data.get("api") or {}
        for key in ("prompt", "completion", "cached", "thoughts"):
            all_tokens[key] = all_tokens.get(key, 0) + int(tokens.get(key) or 0)
        for key in ("total_requests", "total_errors", "total_latency_ms"):
            all_api[key] = all_api.get(key, 0) + int(api.get(key) or 0)
    prompt = int(all_tokens.get("prompt") or 0)
    cached = int(all_tokens.get("cached") or 0)
    cache_summary = format_qwen_cache(cached, prompt) if cached and prompt > 0 else ""
    return (
        cache_summary,
        format_qwen_tokens(all_tokens),
        format_qwen_thinking(all_tokens),
        format_qwen_api_stats(all_api),
    )


def main():
    raw = sys.stdin.read()
    _safe_write(_INPUT_LOG, raw)
    _safe_write(_RAW_LOG, raw)  # Raw JSON dump for inspection
    print("RAW_JSON:" + raw, file=sys.stderr)  # Also stderr for debugging

    try:
        d = json.loads(raw)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        # Valid JSON that isn't an object (e.g. a bare `null` or `[]`) parses
        # without raising, so the except above never fires for it.
        d = {}

    cwd = (d.get("workspace") or {}).get("current_dir") or ""

    cw = d.get("context_window") or {}
    window_size = int(cw.get("context_window_size") or 0)
    current_usage = int(cw.get("current_usage") or 0)
    model_name = (d.get("model") or {}).get("display_name") or ""

    model_summary = format_model_badge(model_name)
    context_summary = format_context(current_usage, window_size, model_name)

    # All metrics from a single pass over Qwen's live snapshot
    metrics = d.get("metrics") or {}
    models = metrics.get("models") or {}
    cache_summary = tokens_summary = thinking_summary = api_summary = ""
    if models:
        cache_summary, tokens_summary, thinking_summary, api_summary = _model_summaries(
            models
        )

    files = metrics.get("files") or {}
    files_summary = format_qwen_files(files)

    vim = d.get("vim") or {}
    vim_mode = vim.get("mode") or ""
    vim_summary = f"VIM:{vim_mode}" if vim_mode else ""

    spinner = _SPINNER_FRAMES[int(time.time() * 4) % len(_SPINNER_FRAMES)]
    line1 = _line1(d, cwd, spinner)

    # Qwen's payload carries no session id, so the render timer collapses
    # onto the shared key (see statusline_lib/rendertimer.py).
    render_suffix = format_render_suffix(None)

    parts = [
        s
        for s in (
            model_summary,
            context_summary,
            cache_summary,
            tokens_summary,
            api_summary,
            thinking_summary,
            files_summary,
            vim_summary,
            render_suffix,
        )
        if s
    ]
    line2 = " | ".join(parts)

    sys.stdout.write(line1)
    if line2:
        sys.stdout.write("\n" + line2)


def _log_error():
    try:
        import traceback

        with open(_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exc(file=f)
    except OSError:
        # The error log itself is unwritable; nowhere left to report, so stay silent.
        pass


if __name__ == "__main__":
    _started = time.monotonic()
    try:
        main()
    except Exception:
        _log_error()
        with contextlib.suppress(Exception):
            sys.stdout.write(
                f"{RED}STATUSLINE ERROR{RESET} — see ~/.qwen/.qwen-statusline-error.log"
            )
    _elapsed_ms = (time.monotonic() - _started) * 1000
    # Excludes interpreter+import startup, same "warm core" scope as
    # statusline.py's slow-render clock -- see statusline_lib/rendertimer.py.
    record_render(_elapsed_ms, None)
