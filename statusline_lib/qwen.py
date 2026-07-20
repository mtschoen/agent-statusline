"""Qwen Code metric formatters and payload adapter for the Qwen Code statusline.

Split out of cost.py: these work with Qwen Code's
metrics.models.<id>.tokens and .api structures, a separate concern from the
Claude Code cost/cache walking that dominates cost.py.

Payload structure: {prompt, completion, total, cached, thoughts}
- `prompt` = total prompt tokens (includes cached reads)
- `cached` = cache_read_input_tokens (subset of prompt)
- `completion` = output tokens
- `thoughts` = reasoning/thinking tokens

Cache renders as `cached / hit%` -- unlike Claude Code, Qwen has no priced
cache-write side, so no write figure is shown (see format_qwen_cache).
Thinking tokens are appended to the context column as (thk NNNK).

`render_qwen_statusline` is the Qwen adapter proper (wave-3 canonical-model
fold, PLAN.md): it normalizes Qwen's payload shape into the same primitive
types (str model ids, int token counts) the shared formatters it calls
(badge.format_model_badge, badge.format_context) expect, then renders Qwen's
two-line layout. Moved here from qwen_statusline.py, which is now a thin
shim delegating into statusline.py's single entry point -- see that module's
docstring. `_safe_str`/`_safe_int` are the type-confusion guards: a
wrong-typed payload field (context_window_size as the string "x",
display_name as an int, metrics.models as a JSON array) degrades to an
honest default instead of crashing a path shared with the Claude adapter.
"""

from .badge import format_context, format_model_badge
from .base import (
    CTX_DENOM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    fmt,
    hostname,
)
from .cachefmt import format_cache_hit, format_cache_read
from .rendertimer import format_render_suffix
from .sessions import count_active_sessions, debounce_session_count


def _safe_str(value):
    """Coerce a payload field to a string for badge/context rendering. None
    (absent field) becomes "" ; every other type is stringified rather than
    left as-is, so a wrong-typed field (e.g. display_name as an int) can't
    reach a shared formatter's string-only operations (.lower(), regex
    search) and crash it."""
    if value is None:
        return ""
    return str(value)


def _safe_int(value, default=0):
    """Coerce a payload field to int, falling back to `default` on a
    wrong-typed value (e.g. context_window_size as the string "x") instead of
    letting int()/ValueError propagate into the render path."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_qwen_cache(cached, prompt):
    """Format cache as `cached / hit%` -- no Claude-style write figure.

    Qwen's implicit prompt cache bills cache-hit tokens at ~20% of the
    standard input rate (Alibaba Model Studio docs: "the portion of the input
    served from the cache is billed at 20% of the standard input token
    price"), so cached tokens are a real, priced cost signal. But Qwen has no
    priced cache-*write* side -- unlike Claude, there's no premium for
    populating the cache, so a write figure here would borrow Claude's
    write-cost visual language (CACHE_WRITE color) for a number that costs
    nothing. Hit rate = cached / prompt.
    Returns: e.g. `730.0K / 73%` or "" if no cached data.
    """
    if not cached or cached <= 0:
        return ""
    return f"{format_cache_read(cached)} / {format_cache_hit(cached, prompt)}"


def format_qwen_tokens(tokens):
    """Format Qwen Code token metrics as plain arrows (no emojis).

    Input: {"prompt": N, "completion": N, "total": N, "cached": N, "thoughts": N}
    Returns: colored string like "↑2.44M ↓35.2K" or "" if empty.
    Matches Claude Code statusline: ↑ for input, ↓ for output.
    """
    if not tokens:
        return ""
    prompt = _safe_int(tokens.get("prompt"))
    completion = _safe_int(tokens.get("completion"))

    parts = []
    if prompt:
        parts.append(f"↑{GREEN}{fmt(prompt)}{RESET}")
    if completion:
        parts.append(f"↓{YELLOW}{fmt(completion)}{RESET}")
    return " ".join(parts) if parts else ""


def format_qwen_thinking(tokens):
    """Extract thinking tokens from Qwen metrics for the context column.

    Input: {"prompt": N, "completion": N, "total": N, "cached": N, "thoughts": N}
    Returns: colored string like "(thk 10.1K)" or "" if no thinking tokens.
    """
    if not tokens:
        return ""
    thoughts = _safe_int(tokens.get("thoughts"))
    if thoughts <= 0:
        return ""
    return f"{CTX_DENOM}(thk {fmt(thoughts)}){RESET}"


def format_qwen_api_stats(api):
    """Format Qwen Code API stats: requests, errors, latency.

    Input: {"total_requests": N, "total_errors": N, "total_latency_ms": N}
    Returns: colored string like "10req 0err 5.0s" or "" if empty.
    """
    if not api:
        return ""
    requests = _safe_int(api.get("total_requests"))
    errors = _safe_int(api.get("total_errors"))
    latency_ms = _safe_int(api.get("total_latency_ms"))

    if not requests:
        return ""

    parts = [f"{requests}req"]
    if errors:
        parts.append(f"{RED}{errors}err{RESET}")
    if latency_ms:
        latency_s = latency_ms / 1000.0
        parts.append(f"{latency_s:.1f}s")
    return " ".join(parts)


def format_qwen_files(files):
    """Format Qwen Code file change stats: lines added/removed.

    Input: {"total_lines_added": N, "total_lines_removed": N}
    Returns: colored string like "+120/-30" or "" if no changes.
    """
    if not files:
        return ""
    added = _safe_int(files.get("total_lines_added"))
    removed = _safe_int(files.get("total_lines_removed"))

    if not added and not removed:
        return ""

    parts = []
    if added:
        parts.append(f"{GREEN}+{added}{RESET}")
    if removed:
        parts.append(f"{RED}-{removed}{RESET}")
    return "/".join(parts)


def _model_summaries(models):
    """Aggregate Qwen's per-model token/api counters into the cache, tokens,
    thinking, and API summaries (Qwen usually reports a single model).

    `models` is expected to be a dict keyed by model id (metrics.models in
    Qwen's payload); a wrong-typed payload can hand this a JSON array
    instead (metrics.models: [...]), which has no .values() -- guarded here
    rather than left to crash with AttributeError."""
    if not isinstance(models, dict):
        return "", "", "", ""
    all_tokens = {}
    all_api = {}
    for model_data in models.values():
        if not isinstance(model_data, dict):
            continue
        tokens = model_data.get("tokens") or {}
        api = model_data.get("api") or {}
        if not isinstance(tokens, dict):
            tokens = {}
        if not isinstance(api, dict):
            api = {}
        for key in ("prompt", "completion", "cached", "thoughts"):
            all_tokens[key] = all_tokens.get(key, 0) + _safe_int(tokens.get(key))
        for key in ("total_requests", "total_errors", "total_latency_ms"):
            all_api[key] = all_api.get(key, 0) + _safe_int(api.get(key))
    prompt = all_tokens.get("prompt") or 0
    cached = all_tokens.get("cached") or 0
    cache_summary = format_qwen_cache(cached, prompt) if cached and prompt > 0 else ""
    return (
        cache_summary,
        format_qwen_tokens(all_tokens),
        format_qwen_thinking(all_tokens),
        format_qwen_api_stats(all_api),
    )


def _qwen_line1(payload, cwd, spinner):
    """Render Qwen's line 1: `<spinner> [host] cwd (branch)`, plus the
    same `[N sessions]` badge the Claude adapter shows."""
    host = hostname()
    line1 = f"{spinner} [{host}] {cwd}"
    n_sessions = debounce_session_count(count_active_sessions(cwd), cwd)
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    git = payload.get("git") or {}
    if not isinstance(git, dict):
        git = {}
    branch = _safe_str(git.get("branch"))
    if branch:
        line1 = f"{line1} ({branch})"
    return line1


def render_qwen_statusline(payload, cwd, spinner):
    """Render Qwen Code's two-line statusline from its JSON payload. The
    Qwen adapter proper: extracts and type-normalizes Qwen's payload fields
    (context_window, model, metrics, vim), then calls the same shared
    formatters (format_model_badge, format_context) the Claude adapter uses.

    Returns (line1, line2); line2 may be "" when every field is absent.
    """
    line1 = _qwen_line1(payload, cwd, spinner)

    cw = payload.get("context_window") or {}
    if not isinstance(cw, dict):
        cw = {}
    window_size = _safe_int(cw.get("context_window_size"))
    current_usage = _safe_int(cw.get("current_usage"))

    model_obj = payload.get("model") or {}
    if not isinstance(model_obj, dict):
        model_obj = {}
    model_name = _safe_str(model_obj.get("display_name"))

    model_summary = format_model_badge(model_name)
    context_summary = format_context(current_usage, window_size, model_name)

    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    models = metrics.get("models") or {}
    cache_summary = tokens_summary = thinking_summary = api_summary = ""
    if models:
        cache_summary, tokens_summary, thinking_summary, api_summary = _model_summaries(
            models
        )

    files = metrics.get("files") or {}
    if not isinstance(files, dict):
        files = {}
    files_summary = format_qwen_files(files)

    vim = payload.get("vim") or {}
    if not isinstance(vim, dict):
        vim = {}
    vim_mode = _safe_str(vim.get("mode"))
    vim_summary = f"VIM:{vim_mode}" if vim_mode else ""

    # Qwen's payload carries no session id, so the render timer collapses
    # onto the shared key (see rendertimer.py).
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
    return line1, line2
