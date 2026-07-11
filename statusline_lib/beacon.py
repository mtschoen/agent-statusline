"""Beacon scanning, format_beacon, format_calibrated_eta.

Imports:
  base   -- for color constants, _json_loads
  walker -- for _walker_subcommand (beacons-latest, beacons-history)
"""

import glob
import json
import os
import re as _re
from datetime import UTC, datetime

from .base import GREEN, RED, RESET, YELLOW, _json_loads, app_dir
from .walker import _walker_subcommand

_BEACON_DRIFT_COLOR = {"nominal": GREEN, "moderate": YELLOW, "material": RED}
_BEACON_STALE_SECONDS = 300

# Drift thresholds. ratio = (elapsed_so_far + current_eta) / original_begin_eta.
# Anchored on observed reality, not the agent's self-assessment -- historical
# data showed agents never self-reported moderate or material, even on
# lifecycles that ended up 2-10x over the begin estimate (the lowballed-and-
# kept-lowballing pattern). 30-min elapsed cap matches the original SKILL
# guidance: long absolute durations are material regardless of ratio.
_DRIFT_MODERATE_RATIO = 1.5
_DRIFT_MATERIAL_RATIO = 2.0
_DRIFT_MATERIAL_ELAPSED_SECONDS = 1800


def _compute_objective_drift(begin_ts, begin_eta_seconds, current_eta_seconds):
    """Classify drift from elapsed + current eta vs original begin eta.

    Returns "nominal" / "moderate" / "material". Falls back to "nominal"
    when inputs are insufficient (no begin anchor, no begin eta, or eta
    not parseable) -- better to under-color than to flash red on missing
    data.
    """
    if not begin_ts or not begin_eta_seconds or begin_eta_seconds <= 0:
        return "nominal"
    try:
        normalized = (
            begin_ts.replace("Z", "+00:00") if begin_ts.endswith("Z") else begin_ts
        )
        begin_dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return "nominal"
    if begin_dt.tzinfo is None:
        begin_dt = begin_dt.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - begin_dt).total_seconds()
    if elapsed < 0:
        elapsed = 0
    if elapsed > _DRIFT_MATERIAL_ELAPSED_SECONDS:
        return "material"
    try:
        eta = float(current_eta_seconds or 0)
    except (TypeError, ValueError):
        eta = 0.0
    ratio = (elapsed + max(0.0, eta)) / begin_eta_seconds
    if ratio >= _DRIFT_MATERIAL_RATIO:
        return "material"
    if ratio >= _DRIFT_MODERATE_RATIO:
        return "moderate"
    return "nominal"


_BEACON_BLOCK_RE = _re.compile(
    r"<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>", _re.DOTALL
)


def _find_session_jsonl(session_id):
    """Locate the JSONL transcript for `session_id` across project dirs."""
    if not session_id:
        return None
    home = os.path.expanduser("~")
    # For Antigravity CLI:
    antigravity_path = os.path.join(
        home,
        ".gemini",
        "antigravity-cli",
        "brain",
        session_id,
        ".system_generated",
        "logs",
        "transcript.jsonl",
    )
    if os.path.exists(antigravity_path):
        return antigravity_path
    # For Claude Code:
    pattern = os.path.join(home, ".claude", "projects", "*", f"{session_id}.jsonl")
    for path in glob.glob(pattern):
        return path
    return None


def _iter_beacons_in_text(text):
    """Yield parsed beacon dicts embedded in one assistant text chunk."""
    if "<progress-beacon>" not in text:
        return
    for match in _BEACON_BLOCK_RE.finditer(text):
        try:
            beacon = _json_loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(beacon, dict):
            yield beacon


def _iter_assistant_beacons(entry):
    """Yield (timestamp, beacon_dict) for every progress-beacon in a JSONL
    assistant entry. No-op for non-assistant / malformed entries."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return
    ts = entry.get("timestamp")
    if not ts:
        return
    content = (entry.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return
    for chunk in content:
        if not isinstance(chunk, dict) or chunk.get("type") != "text":
            continue
        for beacon in _iter_beacons_in_text(chunk.get("text") or ""):
            yield ts, beacon


def _apply_beacon(beacon, ts, state):
    """Fold one beacon into the (begin_ts, report_ts, begin_eta) anchor state."""
    kind = beacon.get("kind")
    if kind == "begin":
        state["begin_ts"] = ts
        # New begin resets the step anchor -- any reports before this begin
        # belonged to a closed lifecycle.
        state["report_ts"] = None
        eta = beacon.get("eta_seconds")
        try:
            eta_val = float(eta) if eta is not None else 0.0
        except (TypeError, ValueError):
            eta_val = 0.0
        state["begin_eta"] = eta_val if eta_val > 0 else None
    elif kind == "report":
        # Only track reports within the current begin's lifecycle.
        if state["begin_ts"] is not None:
            state["report_ts"] = ts
    elif kind == "end":
        state["begin_ts"] = None
        state["report_ts"] = None
        state["begin_eta"] = None


def _scan_beacon_anchors(path):
    """One forward pass over the JSONL, folding every beacon into anchor state."""
    state = {"begin_ts": None, "report_ts": None, "begin_eta": None}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                evt = _json_loads(line)
            except (ValueError, TypeError):
                continue
            for ts, beacon in _iter_assistant_beacons(evt):
                _apply_beacon(beacon, ts, state)
    return state


def _find_beacon_anchors(session_id):
    """Scan the session's JSONL for the active lifecycle's anchors.

    Returns (turn_anchor_ts, step_anchor_ts, begin_eta_seconds):
      turn_anchor_ts -- ISO-8601 timestamp of the most recent kind=begin beacon,
        or None if the session never emitted one. Surfaced by the status line
        as an explicit `no begin` error rather than silently anchoring to the
        first non-begin beacon (that fallback masked agents skipping begin).
      step_anchor_ts -- ISO-8601 timestamp of the most recent kind=report
        beacon that was emitted AFTER turn_anchor_ts. None if no report has
        fired in the current lifecycle. Drives the "step HH:MM (Mm)" mid-turn
        anchor so the user sees motion as the agent progresses through
        sub-tasks within a turn.
      begin_eta_seconds -- `eta_seconds` from the most recent kind=begin beacon,
        used as the original-estimate denominator when the status line
        computes objective drift from elapsed-vs-original. None if no begin
        is in flight or it carried a non-positive eta.

    Walker only exposes the LATEST beacon, but for the status line we want
    wall-clock anchors. Doing the scan in Python keeps walker's surface
    stable; the cost is one forward pass over the JSONL per render. JSONLs
    cap at single-digit MB in practice, so the scan is sub-100ms even on
    big sessions.
    """
    path = _find_session_jsonl(session_id)
    if not path:
        return (None, None, None)
    try:
        state = _scan_beacon_anchors(path)
    except OSError:
        return (None, None, None)
    return (state["begin_ts"], state["report_ts"], state["begin_eta"])


def _format_clock_and_elapsed(begin_ts):
    """Convert an ISO-8601 begin timestamp to "HH:MM (Nm)" using local time.

    Returns None if the timestamp can't be parsed.
    """
    if not begin_ts:
        return None
    try:
        # Python's fromisoformat accepts the trailing Z suffix on 3.11+.
        normalized = (
            begin_ts.replace("Z", "+00:00") if begin_ts.endswith("Z") else begin_ts
        )
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()
    elapsed = (datetime.now(UTC) - dt).total_seconds()
    if elapsed < 0:
        elapsed = 0
    elapsed_min = max(0, int(elapsed) // 60)
    return f"{local:%H:%M} ({elapsed_min}m)"


def format_beacon(session_id):
    """Render the live beacon column for `session_id`.

    Returns (rendered_str | None, beacon_dict | None). None means the
    column should be hidden (no session, no beacon, kind=end, or walker
    unavailable). Stale beacons (>5 min old) render as "⏱ stale Nm" in
    red so the user can tell the agent has gone quiet on its own promise.
    """
    if not session_id:
        return (None, None)
    data = _walker_subcommand("beacons-latest", "--session-id", session_id)
    if not data:
        return (None, None)
    beacon = data.get("beacon")
    if not beacon or beacon.get("kind") == "end":
        return (None, None)

    age = data.get("age_seconds")
    if age is not None and age > _BEACON_STALE_SECONDS:
        minutes = max(0, int(age) // 60)
        return (f"{RED}⏱ stale {minutes}m{RESET}", beacon)

    # Defensive float() coercion, matching _apply_beacon's handling of the
    # same field: a malformed transcript can carry a non-numeric eta_seconds,
    # which must degrade gracefully rather than crash the render.
    try:
        eta_seconds = float(beacon.get("eta_seconds") or 0)
    except (TypeError, ValueError):
        eta_seconds = 0.0
    eta_min = max(1, int(eta_seconds // 60))
    summary = (beacon.get("summary") or "")[:60]

    turn_ts, step_ts, begin_eta = _find_beacon_anchors(session_id)
    drift = _compute_objective_drift(turn_ts, begin_eta, eta_seconds)
    color = _BEACON_DRIFT_COLOR.get(drift, RESET)
    turn_anchor = _format_clock_and_elapsed(turn_ts)
    step_anchor = _format_clock_and_elapsed(step_ts)
    if turn_anchor and step_anchor:
        return (
            f"{color}⏱ turn {turn_anchor} · step {step_anchor} · ~{eta_min}m · {summary}{RESET}",
            beacon,
        )
    if turn_anchor:
        return (f"{color}⏱ turn {turn_anchor} · ~{eta_min}m · {summary}{RESET}", beacon)
    return (f"{RED}⏱ no begin · ~{eta_min}m · {summary}{RESET}", beacon)


_BIAS_CACHE_PATH = os.path.join(app_dir(), ".statusline-bias-cache.json")
_BIAS_CACHE_TTL_SECONDS = 60
# A walker failure (missing binary, timeout on a slow/mounted root) is cached
# too, and for longer: beacons-history walks the full fleet, so retrying it on
# every render turns one slow root into a multi-second stall per render.
_BIAS_FAILURE_TTL_SECONDS = 300
_CALIBRATION_MIN_PAIRS = 20


def _bias_factor_cached(period_seconds):
    """Return (n_pairs, bias_factor) from beacons-history, file-cached.

    Beacons-history walks the full fleet, so per-render calls are wasteful.
    Cache TTL is short enough that fresh end-beacons influence the next
    render without a manual flush. Failures are negative-cached under the
    longer _BIAS_FAILURE_TTL_SECONDS so a slow walker degrades the calibrated
    ETA (which is optional) instead of stalling every render.

    The cache file holds one entry PER PERIOD (keyed by the string period, so
    the 5h and weekly windows -- or any other caller -- each keep their own
    fresh entry). A single shared entry would have a fresh call for period B
    overwrite period A's still-fresh entry outright, forcing a recompute on
    every call whenever callers alternate between periods.
    """
    key = str(int(period_seconds))
    cache = {}
    try:
        with open(_BIAS_CACHE_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, ValueError):
        pass

    c = cache.get(key)
    if isinstance(c, dict):
        age = datetime.now(UTC).timestamp() - c.get("computed_at_unix", 0)
        ttl = _BIAS_FAILURE_TTL_SECONDS if c.get("failed") else _BIAS_CACHE_TTL_SECONDS
        if age < ttl:
            return c.get("n_pairs", 0), c.get("bias_factor")

    data = _walker_subcommand(
        "beacons-history",
        "--period",
        key,
        "--win-start",
        "0",
        timeout=5,
    )
    entry = {
        "computed_at_unix": datetime.now(UTC).timestamp(),
        "period_seconds": period_seconds,
        "n_pairs": (data or {}).get("n_pairs", 0),
        "bias_factor": (data or {}).get("bias_factor"),
    }
    if not data:
        entry["failed"] = True
    cache[key] = entry
    try:
        with open(_BIAS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        # Best-effort cache write; failure just means we recompute next time.
        pass
    return entry["n_pairs"], entry["bias_factor"]


def format_calibrated_eta(raw_eta_seconds, period_seconds=604800):
    """Render the calibrated-ETA line, or None if too few pairs to calibrate.

    Multiplies `raw_eta_seconds` by a bias factor derived from a 7-day
    median of (actual_elapsed / begin_eta) ratios across the user's fleet.
    Gated on n_pairs >= 20 so a handful of outlier sessions can't bias
    the figure on a fresh install.
    """
    if not raw_eta_seconds or raw_eta_seconds <= 0:
        return None
    n_pairs, bias = _bias_factor_cached(period_seconds)
    if n_pairs < _CALIBRATION_MIN_PAIRS or bias is None:
        return None
    calibrated = float(raw_eta_seconds) * float(bias)
    cal_min = max(1, int(calibrated // 60))
    # The U+00D7 multiplication sign is deliberately rendered in the
    # status-line ETA badge; ASCII 'x' would change user-facing output.
    return f"~{cal_min}m calibrated ({float(bias):.1f}×)"  # noqa: RUF001


# Muted grey: session timing is ambient context on line 3, not a warning.
_SESSION_TIMING_COLOR = "\x1b[38;5;245m"


def _fmt_duration_ms(milliseconds):
    """Human duration from milliseconds: '45s' / '12m' / '1h08m'. '' for
    None/non-numeric/<=0 so an absent figure simply drops out."""
    try:
        seconds = int(milliseconds) // 1000
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_min = divmod(minutes, 60)
    return f"{hours}h{rem_min:02d}m"


def format_session_timing(cost):
    """`⏳ <wall> · <api> api` from the payload's `cost` durations, or "".

    Wall = total_duration_ms (clock time the session has existed); api =
    total_api_duration_ms (time spent in model calls), so the pair shows how
    compute-bound the session is. Returns "" until a wall figure exists (brand
    new session), and drops the `· <api>` tail when that figure is absent. The
    ⏳ hourglass is deliberately distinct from the beacon's ⏱ turn timer so a
    session total never reads as a live per-turn clock.
    """
    if not isinstance(cost, dict):
        return ""
    wall = _fmt_duration_ms(cost.get("total_duration_ms"))
    if not wall:
        return ""
    api = _fmt_duration_ms(cost.get("total_api_duration_ms"))
    body = f"⏳ {wall} · {api} api" if api else f"⏳ {wall}"
    return f"{_SESSION_TIMING_COLOR}{body}{RESET}"
