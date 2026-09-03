"""Anthropic Claude Fable-tier weekly quota display: `fable: P% ±Hh` on line 2.

Data model:
Schoen-lab's inference_manager dashboard exposes provider quota pools via
`POST http://<dashboard-host>:8001/api/quota/observed` (backed by
`core/quota_status.py`'s `quota_report()`). AnthropicSubscriptionReader emits
a separate `fable` scoped pool alongside the existing `default` pool.

Claude Code hands the statusline the `default` pool's own windows for free on
every render, as the `rate_limits` object on stdin. The observed endpoint
folds that pushed data into the dashboard's cached `default` pool and returns
the same report shape `GET /api/quota/providers` returns, so one request both
feeds and reads: the render never has to pay a second metered upstream call
for data it already has for free. The `fable` pool is scoped separately and
is never present in `rate_limits`, so the request still has to happen to read
it. A dashboard that predates the observed endpoint answers 404/405, in which
case this module falls back to the older `GET /api/quota/providers` route.
Missing or malformed `rate_limits` is not an error on either path -- the POST
carries no `rate_limits` key and the response is unaffected.

This module extracts the `anthropic-sub` (or `anthropic`) provider's `fable`
pool (utilization percent and seven-day reset timestamp) from that response,
and renders a compact field e.g. `fable: P% ±Hh` using pace.py's `_project_pace`
and base.py's `color_high_bad`.

To obey the render-budget invariant (no inline HTTP calls in the render path),
the render path reads a stale-while-revalidate TTL disk cache and hands
recomputation to the resident server's worker pool as a "fable-quota" job
(statusline_lib/server_jobs.py, request_refresh), passing the session's
`rate_limits` through as the refresh argument so the job can push it.
Failures are negative-cached to avoid requesting a fresh refresh on every
render when the endpoint is unreachable.

Host resolution:
The quota dashboard host comes from `pref("STATUSLINE_FABLE_QUOTA_HOST")`
(prefs JSON > env var) and nowhere else. There is deliberately NO default:
this field talks to a machine on the operator's own fleet, and baking one
fleet's hostname into the source meant every unconfigured machine -- including
work machines on unrelated networks -- silently issued HTTP requests to a
stranger's box and rendered that fleet's quota figure. Unset means the field
is simply off.

(A previous revision also consulted `schoen_fleet.get_host("llamabox")`. That
was dead code -- `schoen_fleet` exposes no `get_host` -- and it hard-coded the
same hostname it was meant to indirect away from. If fleet-aware resolution is
wanted later, look the dashboard up by ROLE, e.g. a `quota_src` role resolved
through `schoen_fleet`'s `RoleRecord`/`HostRegistry`, never by host name.)
"""

import contextlib
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
from .server_jobs import request_refresh
from .ttlcache import read_raw_cache, write_ttl_cache

_WEEK_SECONDS = 7 * 86400
# The displayed datum is a seven-day (weekly) quota pool that moves roughly
# 1% per 100 minutes, so a short poll interval buys no visible freshness.
# The upstream dashboard endpoint triggers a live, metered Anthropic API call
# per fetch, so polling faster than its own 60s cache TTL only burns quota
# for a number that will not have moved. 300s (5 minutes) keeps the field
# comfortably fresh relative to the weekly window while cutting needless
# round-the-clock upstream load.
_QUOTA_TTL_SECONDS = 300
_QUOTA_FAILURE_TTL_SECONDS = 60
_DEFAULT_PORT = 8001
_DEFAULT_SCHEME = "http"
_ENDPOINT_PATH_PROVIDERS = "/api/quota/providers"
_ENDPOINT_PATH_OBSERVED = "/api/quota/observed"
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
    """The configured quota dashboard host, or None when unconfigured.

    Configuration is the only source (see the module docstring): there is no
    default, so an unconfigured machine makes no request at all.
    """
    override = pref(_PREF_HOST)
    if override is None:
        return None
    val = override.strip()
    return val or None


def _dashboard_url(host, path=_ENDPOINT_PATH_PROVIDERS):
    """Build the full URL for `path` from <host[:port]> or URL, or None when
    `host` carries no usable authority. Defaults to the legacy
    GET /api/quota/providers path; callers pass _ENDPOINT_PATH_OBSERVED for the
    push-capable POST route."""
    h = (host or "").strip()
    if not h:
        return None
    scheme = _DEFAULT_SCHEME
    if "://" in h:
        parsed = urllib.parse.urlsplit(h)
        scheme = parsed.scheme or _DEFAULT_SCHEME
        netloc = parsed.netloc or parsed.path.split("/")[0]
        if not netloc:
            return None
        if ":" not in netloc:
            netloc = f"{netloc}:{_DEFAULT_PORT}"
        return f"{scheme}://{netloc}{path}"
    # Bare host or host:port - strip any leading/trailing paths if passed
    host_port = h.split("/")[0].strip()
    if not host_port:
        return None
    if ":" not in host_port:
        host_port = f"{host_port}:{_DEFAULT_PORT}"
    return f"{scheme}://{host_port}{path}"


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


def _post_quota_observed(url, rate_limits, timeout=2.0):
    """Perform HTTP POST to /api/quota/observed, pushing the session's
    rate_limits (the Claude Code stdin object, or falsy when unavailable)
    alongside reading the current report back in the same response.

    Returns (payload, need_providers_fallback):
      - success: (parsed dict, False)
      - endpoint missing on an older dashboard (HTTP 404/405): (None, True),
        signaling the caller to retry against the legacy
        GET /api/quota/providers route.
      - any other failure (timeout, connection refused, malformed body):
        (None, False) -- retrying against the legacy route would not help
        when the dashboard itself is unreachable, so the caller negative-caches
        directly instead of paying a second failed round trip."""
    body = json.dumps({"rate_limits": rate_limits} if rate_limits else {}).encode(
        "utf-8"
    )
    try:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "agent-statusline/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None, False
            response_body = resp.read().decode("utf-8")
            return json.loads(response_body), False
    except urllib.error.HTTPError as error:
        return None, error.code in (404, 405)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        Exception,
    ):
        return None, False


def _write_failure_cache():
    """Negative-cache a failed refresh without erasing the last good value.

    The failure marker only exists to bound refresh requests to one per
    _QUOTA_FAILURE_TTL_SECONDS; a stale value beats a blank field, so merge
    failed=True into the existing entry and fall back to a null marker only
    when no entry exists. write_ttl_cache re-stamps cached_at_unix, so the
    merged entry restarts the failure TTL."""
    existing = read_raw_cache(_quota_cache_path())
    if existing is None:
        existing = {"used_percentage": None, "resets_at_unix": None}
    write_ttl_cache(_quota_cache_path(), {**existing, "failed": True})


def refresh_fable_quota_cache(rate_limits):
    """Worker-pool recompute for server_jobs.py's 'fable-quota' kind.
    POSTs the session's rate_limits (falsy when unavailable) to the quota
    dashboard's observed endpoint and updates the local TTL cache from the
    response. Falls back to the legacy GET route when the observed endpoint
    is missing (an older, not-yet-upgraded dashboard). Runs on the server's
    worker pool, so network latency never blocks a render. Unreachable
    endpoint or malformed responses are negative-cached under
    _QUOTA_FAILURE_TTL_SECONDS so off-fleet machines do not request a fresh
    refresh on every render; the last good value is preserved so the field
    keeps serving stale data through transient dashboard failures."""
    host = _dashboard_host()
    if host is None or host.strip().lower() in _DISABLED_VALUES:
        return
    observed_url = _dashboard_url(host, path=_ENDPOINT_PATH_OBSERVED)
    if observed_url is None:
        return
    payload, need_providers_fallback = _post_quota_observed(
        observed_url, rate_limits, timeout=2.0
    )
    if payload is None and need_providers_fallback:
        providers_url = _dashboard_url(host, path=_ENDPOINT_PATH_PROVIDERS)
        payload = _fetch_quota_payload(providers_url, timeout=2.0)
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


def _fable_quota_cached(now_unix, rate_limits=None):
    """SWR cache read: serve the entry stale-or-fresh and hand recomputation
    to the server's worker pool when it is missing or past the TTL.
    `rate_limits` is threaded through only to carry it to the refresh job
    (the read itself never touches it). Returns None on a true miss."""
    entry = read_raw_cache(_quota_cache_path())
    if entry is not None:
        ttl = _QUOTA_FAILURE_TTL_SECONDS if entry.get("failed") else _QUOTA_TTL_SECONDS
        if now_unix - entry.get("cached_at_unix", 0) >= ttl:
            request_refresh("fable-quota", rate_limits)
        return entry
    request_refresh("fable-quota", rate_limits)
    return None


def format_fable_quota(rate_limits=None, show_pace=True):
    """Line-2 field `fable: P% ±Hh` from the Anthropic Fable weekly quota pool.
    `rate_limits` is the Claude Code stdin payload's `rate_limits` object (the
    `default` pool's windows), pushed through to the dashboard so a fetch it
    would make anyway for the `fable` pool also feeds the `default` pool at no
    extra cost. None on harnesses that carry no such field (Kimi, Qwen, and
    before the first API response). Returns '' when disabled, unreachable, or
    cold cache."""
    if not pref_bool(_PREF_ENABLED, default=True):
        return ""
    # Unconfigured is off: there is no default host, so there is nothing to ask.
    # _dashboard_host() folds unset and empty-string to None.
    host = _dashboard_host()
    if host is None or host.strip().lower() in _DISABLED_VALUES:
        return ""
    now_unix = _now_unix()
    entry = _fable_quota_cached(now_unix, rate_limits)
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
