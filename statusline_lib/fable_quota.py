"""Anthropic Claude Fable-tier weekly quota display: `fable: P% ±Hh` on line 2.

Data model:
Schoen-lab's inference_manager dashboard exposes provider quota pools via
`GET http://<dashboard-host>:8001/api/quota/providers` (backed by
`core/quota_status.py`'s `quota_report()`). AnthropicSubscriptionReader emits
a separate `fable` scoped pool alongside the existing `default` pool.

This module consumes that endpoint, extracts the `anthropic-sub` (or `anthropic`)
provider's `fable` pool (utilization percent and seven-day reset timestamp),
and renders a compact field e.g. `fable: P% ±Hh` using pace.py's `_project_pace`
and base.py's `color_high_bad`.

To obey the render-budget invariant (no inline HTTP calls in the render path),
the render path reads a stale-while-revalidate TTL disk cache and hands
recomputation to a detached "fable-quota" refresh child
(statusline_lib/refresh.py, maybe_spawn_refresh). Failures are negative-cached
to avoid respawning detached children on every render when the endpoint is
unreachable.

Host resolution:
Checks `pref("STATUSLINE_FABLE_QUOTA_HOST")` first (prefs JSON > env var),
falls back to `schoen_fleet.get_host("llamabox")` if importable,
and defaults to "llamabox:8001".
"""

import contextlib
import importlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .base import app_dir, color_high_bad
from .pace import _project_pace
from .prefs import pref, pref_bool
from .refresh import maybe_spawn_refresh
from .ttlcache import read_raw_cache, write_ttl_cache

_WEEK_SECONDS = 7 * 86400
_QUOTA_TTL_SECONDS = 15
_QUOTA_FAILURE_TTL_SECONDS = 60
_DEFAULT_DASHBOARD_HOST = "llamabox:8001"
_DEFAULT_SCHEME = "http"
_ENDPOINT_PATH = "/api/quota/providers"
_PREF_HOST = "STATUSLINE_FABLE_QUOTA_HOST"
_PREF_ENABLED = "STATUSLINE_FABLE_QUOTA"
_DISABLED_VALUES = ("0", "off", "false", "no")
_WEEKLY_WINDOW_NAMES = ("seven_day", "weekly", "weekly_scoped", "7d")


def _now_unix():
    """Current unix time. Seam so tests can pin the window/freshness clock."""
    return time.time()


def _quota_cache_path():
    return os.path.join(app_dir(), ".statusline-fable-quota-cache.json")


def _safe_float(value):
    """Safely convert a value to float, or return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _is_weekly_window_name(name):
    """True when window name is one of the recognized weekly window names."""
    return str(name or "").strip().lower() in _WEEKLY_WINDOW_NAMES


def _extract_window_metric(window_dict):
    """Extract used percentage float from a window dict, or None."""
    if not isinstance(window_dict, dict):
        return None
    if window_dict.get("headroom_fraction") is not None:
        hf = _safe_float(window_dict.get("headroom_fraction"))
        if hf is not None:
            return (1.0 - hf) * 100.0
    for key in ("used_percentage", "utilization", "used_percent"):
        if window_dict.get(key) is not None:
            val = _safe_float(window_dict.get(key))
            if val is not None:
                return val
    return None


def _dashboard_host():
    """Resolve the quota dashboard host: pref/env override > schoen_fleet registry > default."""
    override = pref(_PREF_HOST)
    if override is not None:
        val = override.strip()
        if val:
            return val
    with contextlib.suppress(Exception):
        schoen_fleet = importlib.import_module("schoen_fleet")
        if hasattr(schoen_fleet, "get_host"):
            host = schoen_fleet.get_host("llamabox")
            if host:
                return str(host).strip()
    return _DEFAULT_DASHBOARD_HOST


def _dashboard_url(host=None):
    """Build the full URL for GET /api/quota/providers from <host[:port]> or URL."""
    h = (_dashboard_host() if host is None else host) or ""
    h = h.strip()
    if not h:
        h = _DEFAULT_DASHBOARD_HOST
    scheme = _DEFAULT_SCHEME
    if "://" in h:
        parsed = urllib.parse.urlsplit(h)
        scheme = parsed.scheme or _DEFAULT_SCHEME
        netloc = parsed.netloc or parsed.path.split("/")[0] or _DEFAULT_DASHBOARD_HOST
        if ":" not in netloc:
            netloc = f"{netloc}:8001"
        return f"{scheme}://{netloc}{_ENDPOINT_PATH}"
    # Bare host or host:port - strip any leading/trailing paths if passed
    host_port = h.split("/")[0].strip() or _DEFAULT_DASHBOARD_HOST
    if ":" not in host_port:
        host_port = f"{host_port}:8001"
    return f"{scheme}://{host_port}{_ENDPOINT_PATH}"


def _parse_timestamp(ts_val):
    """Parse a float/int unix timestamp or ISO string into a unix timestamp float, or None."""
    if ts_val is None:
        return None
    if isinstance(ts_val, (int, float)):
        return float(ts_val)
    if isinstance(ts_val, str):
        val = ts_val.strip()
        if not val:
            return None
        with contextlib.suppress(ValueError):
            return float(val)
        with contextlib.suppress(ValueError, TypeError):
            return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    return None


def _extract_entry_metrics(entry):
    """Extract (used_pct, resets_at_unix) from a single provider entry dict."""
    windows = entry.get("windows")
    used_pct = None
    resets_at_raw = None
    is_weekly = False

    if isinstance(windows, list):
        for w in windows:
            if not isinstance(w, dict) or not _is_weekly_window_name(w.get("name")):
                continue
            used_pct = _extract_window_metric(w)
            if used_pct is not None:
                resets_at_raw = w.get("resets_at")
                is_weekly = True
                break

        if used_pct is None:
            for w in windows:
                if not isinstance(w, dict):
                    continue
                used_pct = _extract_window_metric(w)
                if used_pct is not None:
                    resets_at_raw = w.get("resets_at")
                    is_weekly = _is_weekly_window_name(w.get("name"))
                    break

    if used_pct is None and entry.get("headroom_fraction") is not None:
        hf = _safe_float(entry.get("headroom_fraction"))
        if hf is not None:
            used_pct = (1.0 - hf) * 100.0
            is_weekly = True

    if resets_at_raw is None and is_weekly and entry.get("resets_at") is not None:
        resets_at_raw = entry.get("resets_at")

    if used_pct is not None:
        resets_at_unix = _parse_timestamp(resets_at_raw) if is_weekly else None
        return float(used_pct), resets_at_unix

    return None, None


def _extract_fable_metrics(providers_payload):
    """Extract (used_percent, resets_at_unix) for the Anthropic fable pool from
    the /api/quota/providers JSON payload. Returns (None, None) if absent or invalid.
    If the matched window is not weekly, resets_at_unix is returned as None to avoid
    pacing a non-weekly window against a 7-day budget."""
    if not isinstance(providers_payload, dict):
        return None, None
    providers = providers_payload.get("providers")
    if not isinstance(providers, list):
        return None, None

    for entry in providers:
        if not isinstance(entry, dict):
            continue
        provider_name = str(entry.get("provider") or "").strip().lower()
        pool_name = str(entry.get("pool") or "").strip().lower()
        if pool_name != "fable" or not (
            provider_name == "anthropic" or provider_name.startswith("anthropic")
        ):
            continue

        used_pct, resets_at_unix = _extract_entry_metrics(entry)
        if used_pct is not None:
            return used_pct, resets_at_unix

    return None, None


def _fetch_quota_payload(url, timeout=2.0):
    """Perform HTTP GET request to fetch quota providers JSON.
    Returns parsed dict or None on any error/timeout/non-200 response."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "agent-statusline/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
        Exception,
    ):
        return None


def _write_failure_cache():
    """Negative-cache a failed refresh without erasing the last good value.

    The failure marker only exists to bound detached-child respawns to one per
    _QUOTA_FAILURE_TTL_SECONDS; a stale value beats a blank field, so merge
    failed=True into the existing entry and fall back to a null marker only
    when no entry exists. write_ttl_cache re-stamps cached_at_unix, so the
    merged entry restarts the failure TTL."""
    existing = read_raw_cache(_quota_cache_path())
    if existing is None:
        existing = {"used_percentage": None, "resets_at_unix": None}
    write_ttl_cache(_quota_cache_path(), {**existing, "failed": True})


def refresh_fable_quota_cache(_argument):
    """Detached-child recompute for refresh.py's 'fable-quota' kind.
    Fetches the quota dashboard endpoint and updates the local TTL cache.
    Runs out of process, so network latency never blocks a render.
    Unreachable endpoint or malformed responses are negative-cached under
    _QUOTA_FAILURE_TTL_SECONDS so off-fleet machines do not re-spawn a child on
    every render; the last good value is preserved so the field keeps serving
    stale data through transient dashboard failures."""
    host = _dashboard_host()
    if host.strip().lower() in _DISABLED_VALUES:
        return
    url = _dashboard_url(host)
    payload = _fetch_quota_payload(url, timeout=2.0)
    if payload is None:
        _write_failure_cache()
        return
    used_pct, resets_at_unix = _extract_fable_metrics(payload)
    if used_pct is None:
        _write_failure_cache()
        return
    write_ttl_cache(
        _quota_cache_path(),
        {
            "used_percentage": used_pct,
            "resets_at_unix": resets_at_unix,
            "failed": False,
        },
    )


def _fable_quota_cached(now_unix):
    """SWR cache read: serve the entry stale-or-fresh and hand recomputation
    to a detached child when it is missing or past the TTL. Returns None on a true miss."""
    entry = read_raw_cache(_quota_cache_path())
    if entry is not None:
        ttl = _QUOTA_FAILURE_TTL_SECONDS if entry.get("failed") else _QUOTA_TTL_SECONDS
        if now_unix - entry.get("cached_at_unix", 0) >= ttl:
            maybe_spawn_refresh("fable-quota", 0)
        return entry
    maybe_spawn_refresh("fable-quota", 0)
    return None


def format_fable_quota(show_pace=True):
    """Line-2 field `fable: P% ±Hh` from the Anthropic Fable weekly quota pool.
    Returns '' when disabled, unreachable, or cold cache."""
    if not pref_bool(_PREF_ENABLED, default=True):
        return ""
    raw_host = pref(_PREF_HOST)
    if raw_host is not None and raw_host.strip().lower() in _DISABLED_VALUES:
        return ""
    now_unix = _now_unix()
    entry = _fable_quota_cached(now_unix)
    if entry is None:
        return ""
    util = entry.get("used_percentage")
    if util is None:
        return ""
    resets_at_unix = entry.get("resets_at_unix")
    pace_part = (
        _project_pace(util, resets_at_unix, _WEEK_SECONDS)
        if show_pace and resets_at_unix
        else ""
    )
    return f"fable: {color_high_bad(util, 75, 90)}{pace_part}"
