"""Kimi Code CLI payload adapter for the Kimi statusline.

Contract (kimi-code source, commit 67dd03149, "status_line config" #2255):
the TUI spawns `[status_line] command` from ~/.kimi-code/tui.toml via
`cmd /d /s /c <command>` (Windows) / `sh -c <command>` (POSIX) with
KIMI_CODE_STATUS_LINE=1 in the env and a JSON payload on stdin, kills the
process tree after 300ms, and throttles to one run per second. Only the
FIRST stdout line is rendered (it replaces footer line 1); the exit code
must be 0 and the first line non-empty or the TUI falls back to the
built-in layout. `render_kimi_statusline` therefore returns a single line,
unlike Qwen's two-line adapter.

Payload shape (camelCase, exact keys):
    {model, cwd, gitBranch, permissionMode, planMode, contextUsage,
     contextTokens, maxContextTokens, sessionId, version}
`gitBranch` may be null; `contextUsage` is a float fraction (0.047 == 4.7%,
redundant with contextTokens/maxContextTokens, which we render instead). The payload
carries NO cost/cache/transcript/rate-limit data, so this adapter never
walks transcripts or renders cost. `gitBranch` comes straight from the
payload, and the working-tree badge (`+A -B ↑x ↓y`, matching kimi-code's
built-in footer, which computes it in-process and never puts it in the
payload) comes from the SWR-cached gitref counters -- a cached read plus
a detached refresh spawn, never a synchronous git subprocess, which is
what fits the 300ms kill window.

The `_safe_str`/`_safe_int` type-confusion guards are shared with the Qwen
adapter (imported from .qwen, where they were introduced for the 2026-07-19
hardening): a wrong-typed field (contextTokens as the string "x", model as
an int, planMode as a string) degrades to an honest default instead of
crashing a path that must exit 0.

The `[N sessions]` badge reuses the same SWR-cached count_active_sessions /
debounce_session_count pair the Claude and Qwen line-1s use -- cached reads
only, never a synchronous psutil scan on the render path. Caveat: the
underlying process classifier (statusline_lib/sessions.py) only recognizes
claude/qwen runtimes today, so the badge reflects those neighbors sharing
the cwd, not concurrent kimi sessions.
"""

from .badge import format_context, format_model_badge
from .base import CTX_DENOM, GREEN, RED, RESET, YELLOW, fmt, hostname
from .gitref import git_working_tree_cached
from .qwen import _safe_int, _safe_str
from .sessions import count_active_sessions, debounce_session_count

# Same short session-id badge style as statusline.py's _append_session_id:
# the first 8 chars disambiguate concurrent sessions without eating width.
_SESSION_ID_COLOR = "\x1b[38;5;67m"  # muted steel blue
_SESSION_ID_LEN = 8

# Muted grey, matching statusline.py's session-name label: the CLI version is
# constant for the life of the process, so it should read as secondary text.
_VERSION_COLOR = "\x1b[38;5;245m"

# planMode and non-default permissionMode are identity badges, not
# thresholds: PLAN keeps the neutral mauve denominator hue, while a
# permission mode stricter/looser than the default reads as a caution
# signal (yellow) -- except yolo, which bypasses permission checks entirely
# and earns the danger red.
_PLAN_COLOR = CTX_DENOM

_DEFAULT_PERMISSION_MODE = "manual"


def _session_badge(session_id):
    """Short `[<8 chars>]` session-id badge, or "" when absent/blank."""
    sid = _safe_str(session_id).strip()
    if not sid:
        return ""
    return f"{_SESSION_ID_COLOR}[{sid[:_SESSION_ID_LEN]}]{RESET}"


def _permission_badge(mode):
    """Render a non-default permissionMode; the default "manual" is noise
    and omitted. yolo (no permission checks at all) renders red, any other
    non-default mode (auto, plan-enforced variants, ...) yellow."""
    mode_str = _safe_str(mode).strip()
    if not mode_str or mode_str == _DEFAULT_PERMISSION_MODE:
        return ""
    color = RED if mode_str == "yolo" else YELLOW
    return f"{color}{mode_str}{RESET}"


def _plan_badge(plan_mode):
    """`PLAN` badge when planMode is true. Only a real boolean True counts --
    a wrong-typed truthy-looking value (the string "false", a list) must not
    light the badge, since asserting plan mode when the harness says
    otherwise is worse than omitting it."""
    if plan_mode is not True:
        return ""
    return f"{_PLAN_COLOR}PLAN{RESET}"


def _version_badge(version):
    """Dim `vX.Y.Z` CLI-version badge, or "" when absent/blank. Unlike the
    session-id badge (which stringifies any type), only scalars render: a
    wrong-typed container stringified into the badge ("v['0', '29']") would
    be noise, so a list/dict version drops instead. A leading "v" in the
    payload is stripped first so we never render "vv0.29.2"."""
    if isinstance(version, bool) or not isinstance(version, (str, int, float)):
        return ""
    ver = str(version).strip()
    if ver.startswith("v"):
        ver = ver[1:]
    if not ver:
        return ""
    return f"{_VERSION_COLOR}v{ver}{RESET}"


def _working_tree_badge(cwd):
    """`+A -B ↑x ↓y` working-tree badge matching kimi-code's built-in footer
    (`branch [+2 -2 ↑58]` -- diff vs HEAD plus upstream sync), minus the
    built-in's square brackets: our branch already sits inside parens, so
    nesting `[...]` there reads as double punctuation. Data comes from the
    SWR gitref cache (never an inline git call -- the 300ms kill window
    forbids it), so the badge can lag a change by one refresh cycle and
    reads as absent on a cold cache, both honest degrades. Green/red mirror
    diffstat.format_lines; the sync counter is yellow as a "push/pull
    pending" caution."""
    added, deleted, ahead, behind = git_working_tree_cached(cwd)
    parts = []
    if added or deleted:
        parts.append(f"{GREEN}+{fmt(added)}{RESET} {RED}-{fmt(deleted)}{RESET}")
    sync = ""
    if ahead:
        sync += f"↑{ahead}"
    if behind:
        sync += f"↓{behind}"
    if sync:
        parts.append(f"{YELLOW}{sync}{RESET}")
    return f" {' '.join(parts)}" if parts else ""


def _kimi_line_prefix(payload, cwd, spinner):
    """`<spinner> [host] cwd (branch) [sid]`, plus the shared `[N sessions]`
    badge -- the same shape as the Qwen/Claude line-1 prefix, except the
    branch comes from the payload's gitBranch instead of a git subprocess."""
    host = hostname()
    prefix = f"{spinner} [{host}] {cwd}"
    n_sessions = debounce_session_count(count_active_sessions(cwd), cwd)
    if n_sessions >= 2:
        prefix = f"{prefix} {RED}[{n_sessions} sessions]{RESET}"
    branch = _safe_str(payload.get("gitBranch")).strip()
    if branch:
        prefix = f"{prefix} ({branch}{_working_tree_badge(cwd)})"
    badge = _session_badge(payload.get("sessionId"))
    if badge:
        prefix = f"{prefix} {badge}"
    return prefix


def render_kimi_statusline(payload, cwd, spinner):
    """Render Kimi Code CLI's single-line statusline from its JSON payload.

    Normalizes kimi's camelCase payload into the primitive types the shared
    formatters expect, then composes `<spinner> [host] cwd (branch) [sid]`
    with ` | `-joined fields: model badge, context usage, permission mode
    (non-default only), PLAN badge, CLI-version badge. Returns the one line
    (never empty -- spinner/host/cwd always render, which the TUI's
    non-empty-first-line contract requires).
    """
    line = _kimi_line_prefix(payload, cwd, spinner)

    model_name = _safe_str(payload.get("model"))
    model_summary = format_model_badge(model_name)
    context_summary = format_context(
        _safe_int(payload.get("contextTokens")),
        _safe_int(payload.get("maxContextTokens")),
        model_name,
    )

    parts = [
        s
        for s in (
            model_summary,
            context_summary,
            _permission_badge(payload.get("permissionMode")),
            _plan_badge(payload.get("planMode")),
            _version_badge(payload.get("version")),
        )
        if s
    ]
    if parts:
        line = f"{line} {' | '.join(parts)}"
    return line
