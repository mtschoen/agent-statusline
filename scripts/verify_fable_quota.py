"""Verify statusline_lib/fable_quota.py - Anthropic Fable weekly quota pool display.

Covers:
  - Host resolution: prefs override, schoen_fleet fallback, default llamabox:8001
  - URL building: schemes, ports, defaults, path stripping
  - Payload extraction: ISO/unix timestamps, headroom/utilization formats,
    error states, malformed shapes, non-weekly isolation, malformed numeric values
  - Safe float parsing: valid numbers, invalid strings, non-numeric types, None
  - HTTP fetcher: 200, 204, 500, network errors, timeout handling
  - Observed push: rate_limits carried in the POST body, missing rate_limits
    still succeeds, 404/405 falls back to GET /api/quota/providers, other
    failures negative-cache directly without a fallback attempt
  - SWR cache: fresh, stale, missing, corrupt, failure negative-caching with backoff
  - Pace projection: on-target, surplus, deficit, zero utilization, boundary crossings
  - Format output: full with pace, compact without pace, disabled toggles
  - Adapter integration: Claude (statusline.py), Qwen (qwen.py), Kimi (kimi.py)
  - Refresher dispatch: run_refresh("fable-quota", ...)
"""

import contextlib
import http.server
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import types
from datetime import UTC, datetime
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline
import statusline_lib.fable_quota as fable_quota
import statusline_lib.pace as pace
from statusline_lib.fable_quota import (
    _dashboard_host,
    _dashboard_url,
    _extract_fable_metrics,
    _fable_quota_cached,
    _fetch_quota_payload,
    _parse_timestamp,
    _quota_cache_path,
    _safe_float,
    format_fable_quota,
    refresh_fable_quota_cache,
)
from statusline_lib.kimi import render_kimi_statusline
from statusline_lib.pace import _project_pace
from statusline_lib.qwen import render_qwen_statusline
from statusline_lib.refresh import maybe_spawn_refresh, run_refresh

_HTTP_OK = 200
NOW = 1787227200.0
RESET_AT_MIDWEEK = NOW + 3.5 * 86400
RESET_AT_MIDWEEK_ISO = datetime.fromtimestamp(RESET_AT_MIDWEEK, tz=UTC).isoformat()
_REAL_NOW_UNIX = fable_quota._now_unix
_PATCHED_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "STATUSLINE_PLATFORM",
    "STATUSLINE_PREFS_PATH",
    "STATUSLINE_FABLE_QUOTA",
    "STATUSLINE_FABLE_QUOTA_HOST",
)


class _Fixture:
    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="fable-quota-test-")
        self.prefs_path = os.path.join(self.home, "prefs.json")
        self._saved_env = {}
        self._saved_fable_now = None
        self._saved_pace_now = None

    def __enter__(self):
        for key in _PATCHED_ENV_KEYS:
            self._saved_env[key] = os.environ.pop(key, None)
        os.environ["HOME"] = self.home
        os.environ["USERPROFILE"] = self.home
        os.environ["STATUSLINE_PREFS_PATH"] = self.prefs_path
        self._saved_fable_now = fable_quota._now_unix
        self._saved_pace_now = pace._now_unix
        fable_quota._now_unix = lambda: NOW
        pace._now_unix = lambda: NOW
        return self

    def __exit__(self, *_exc_info):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        fable_quota._now_unix = self._saved_fable_now
        pace._now_unix = self._saved_pace_now

    def set_env(self, **kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value

    def write_cache(
        self, used_percentage, resets_at_unix=None, cached_at_unix=None, failed=False
    ):
        payload = {
            "used_percentage": used_percentage,
            "resets_at_unix": resets_at_unix,
            "cached_at_unix": cached_at_unix if cached_at_unix is not None else NOW,
            "failed": failed,
        }
        path = _quota_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)


def _check_quota_ttl_floor(failures):
    """The poll TTL must never drop back below the dashboard's own 60s cache
    TTL: the datum is a seven-day pool that cannot move enough in under a
    minute to justify polling faster than the server refreshes it, and each
    poll below that floor buys nothing while still triggering a live, metered
    Anthropic API call on the server."""
    server_cache_ttl_seconds = 60
    if server_cache_ttl_seconds > fable_quota._QUOTA_TTL_SECONDS:
        failures.append(
            "_QUOTA_TTL_SECONDS "
            f"({fable_quota._QUOTA_TTL_SECONDS}) must be at least the "
            f"server-side cache TTL ({server_cache_ttl_seconds}s)"
        )


def _check_now_unix_and_paths(failures):
    val = _REAL_NOW_UNIX()
    if not isinstance(val, (int, float)) or val <= 0:
        failures.append(f"_now_unix() seam returned {val!r}")
    if ".statusline-fable-quota-cache.json" not in _quota_cache_path():
        failures.append("cache path unexpected")


def _check_safe_float(failures):
    cases = [
        (None, None),
        (42.5, 42.5),
        ("12.75", 12.75),
        ("invalid", None),
        ([], None),
        ({}, None),
    ]
    for v, exp in cases:
        if _safe_float(v) != exp:
            failures.append(f"_safe_float({v!r}): expected {exp!r}")
    if fable_quota._extract_window_metric("invalid") is not None:
        failures.append("_extract_window_metric(non-dict) must return None")


def _check_dashboard_host_resolution(failures):
    with _Fixture() as fx:
        if _dashboard_host() != "llamabox:8001":
            failures.append(f"default host: got {_dashboard_host()!r}")
        fx.set_env(STATUSLINE_FABLE_QUOTA_HOST="fleetbox:9000")
        if _dashboard_host() != "fleetbox:9000":
            failures.append(f"env host override: got {_dashboard_host()!r}")
        fx.set_env(STATUSLINE_FABLE_QUOTA_HOST="")
        mock_fleet = types.ModuleType("schoen_fleet")
        mock_fleet.get_host = lambda n: (
            "resolved-fleet:8001" if n == "llamabox" else None
        )
        sys.modules["schoen_fleet"] = mock_fleet
        try:
            if _dashboard_host() != "resolved-fleet:8001":
                failures.append(f"schoen_fleet resolution: got {_dashboard_host()!r}")
            mock_fleet.get_host = lambda _n: None
            if _dashboard_host() != "llamabox:8001":
                failures.append("schoen_fleet None fallback mismatch")
            mock_fleet.get_host = lambda _n: 1 / 0
            if _dashboard_host() != "llamabox:8001":
                failures.append("schoen_fleet error fallback mismatch")
        finally:
            sys.modules.pop("schoen_fleet", None)


def _check_dashboard_url_formatting(failures):
    cases = [
        ("https://fleet.net/api/quota", "https://fleet.net:8001/api/quota/providers"),
        (
            "http://fleet.net:9000/api/quota/providers",
            "http://fleet.net:9000/api/quota/providers",
        ),
        ("barehost", "http://barehost:8001/api/quota/providers"),
        ("host:9999", "http://host:9999/api/quota/providers"),
        ("/path", "http://llamabox:8001/api/quota/providers"),
        ("http://", "http://llamabox:8001/api/quota/providers"),
        ("", "http://llamabox:8001/api/quota/providers"),
        (None, "http://llamabox:8001/api/quota/providers"),
    ]
    for host, exp in cases:
        if _dashboard_url(host) != exp:
            failures.append(f"_dashboard_url({host!r}): expected {exp!r}")


def _check_parse_timestamp(failures):
    cases = [
        (None, None),
        (1700000000, 1700000000.0),
        ("1700000000.5", 1700000000.5),
        ("2026-08-20T12:00:00Z", 1787227200.0),
        ("2026-08-20T12:00:00+00:00", 1787227200.0),
        ("", None),
        ("invalid", None),
        ([], None),
    ]
    for v, exp in cases:
        if _parse_timestamp(v) != exp:
            failures.append(f"_parse_timestamp({v!r}): expected {exp!r}")


def _check_extract_captured_payload(failures):
    sample = {
        "providers": [
            {
                "provider": "google-sub",
                "pool": "default",
                "windows": [{"name": "gemini-5h", "headroom_fraction": 0.5}],
            },
            {
                "provider": "anthropic-sub",
                "pool": "default",
                "windows": [{"name": "seven_day", "headroom_fraction": 0.2}],
            },
            {
                "provider": "anthropic-sub",
                "pool": "fable",
                "windows": [
                    {
                        "name": "seven_day",
                        "headroom_fraction": 0.46,
                        "resets_at": RESET_AT_MIDWEEK_ISO,
                    }
                ],
            },
        ]
    }
    if _extract_fable_metrics(sample) != (54.0, RESET_AT_MIDWEEK):
        failures.append(
            f"captured payload extraction mismatch: got {_extract_fable_metrics(sample)!r}"
        )


def _check_extract_fallback_metrics(failures):
    for w_name, key, val, exp in (
        ("weekly", "used_percentage", 30.0, 30.0),
        ("weekly_scoped", "utilization", 42.0, 42.0),
        ("7d", "used_percent", 15.0, 15.0),
        ("seven_day", "headroom_fraction", 0.75, 25.0),
        ("seven_day", "used_percentage", 33.0, 33.0),
        ("seven_day", "utilization", 44.0, 44.0),
        ("seven_day", "used_percent", 55.0, 55.0),
    ):
        p = {
            "providers": [
                {
                    "provider": "anthropic",
                    "pool": "fable",
                    "windows": [
                        {"name": w_name, key: val, "resets_at": RESET_AT_MIDWEEK}
                    ],
                }
            ]
        }
        if _extract_fable_metrics(p) != (exp, RESET_AT_MIDWEEK):
            failures.append(
                f"window {w_name}/{key}: expected {(exp, RESET_AT_MIDWEEK)}"
            )

    for key, val, exp in (
        ("headroom_fraction", 0.6, 40.0),
        ("used_percentage", 40.0, 40.0),
        ("utilization", 45.0, 45.0),
        ("used_percent", 50.0, 50.0),
    ):
        p = {
            "providers": [
                {
                    "provider": "anthropic",
                    "pool": "fable",
                    "windows": [
                        "invalid",
                        {"name": "five_hour", key: val, "resets_at": RESET_AT_MIDWEEK},
                    ],
                }
            ]
        }
        if _extract_fable_metrics(p) != (exp, None):
            failures.append(f"non-weekly fallback for {key}: expected {(exp, None)}")

    non_weekly_entry_reset = {
        "providers": [
            {
                "provider": "anthropic",
                "pool": "fable",
                "resets_at": RESET_AT_MIDWEEK,
                "windows": [{"name": "five_hour", "used_percentage": 47.0}],
            }
        ]
    }
    if _extract_fable_metrics(non_weekly_entry_reset) != (47.0, None):
        failures.append(
            "entry-level resets_at must not pace a non-weekly window against"
            " the weekly budget"
        )

    weekly_entry_reset = {
        "providers": [
            {
                "provider": "anthropic",
                "pool": "fable",
                "resets_at": RESET_AT_MIDWEEK,
                "windows": [{"name": "seven_day", "used_percentage": 47.0}],
            }
        ]
    }
    if _extract_fable_metrics(weekly_entry_reset) != (47.0, RESET_AT_MIDWEEK):
        failures.append(
            "weekly window without its own resets_at must fall back to the"
            " entry-level resets_at"
        )


def _check_extract_malformed_payloads(failures):
    for bad_w in (
        {"name": "seven_day", "headroom_fraction": "soon"},
        {"name": "seven_day", "headroom_fraction": [1, 2]},
        {"name": "seven_day", "used_percentage": "not-a-number"},
        {"name": "seven_day", "utilization": {}},
        {"name": "seven_day", "used_percent": None},
    ):
        p = {
            "providers": [
                {"provider": "anthropic", "pool": "fable", "windows": [bad_w]}
            ]
        }
        if _extract_fable_metrics(p) != (None, None):
            failures.append(f"bad numeric window {bad_w!r} must return (None, None)")

    top_level = {
        "providers": [
            {
                "provider": "anthropic-sub",
                "pool": "fable",
                "headroom_fraction": 0.40,
                "resets_at": RESET_AT_MIDWEEK,
                "windows": [],
            }
        ]
    }
    if _extract_fable_metrics(top_level) != (60.0, RESET_AT_MIDWEEK):
        failures.append("top-level headroom fallback failed")

    with_none = {
        "providers": [
            {
                "provider": "anthropic-sub",
                "pool": "fable",
                "windows": [
                    None,
                    "x",
                    {
                        "name": "seven_day",
                        "headroom_fraction": 0.46,
                        "resets_at": RESET_AT_MIDWEEK_ISO,
                    },
                ],
            }
        ]
    }
    if _extract_fable_metrics(with_none) != (54.0, RESET_AT_MIDWEEK):
        failures.append("windows with non-dict items failed")

    for invalid in (
        None,
        {},
        {"providers": "no"},
        {"providers": [None, "x", 1]},
        {"providers": [{"provider": "openai", "pool": "fable"}]},
    ):
        if _extract_fable_metrics(invalid) != (None, None):
            failures.append(f"invalid payload {invalid!r} must return (None, None)")


def _check_pace_projection(failures):
    week_sec = 7 * 86400
    with _Fixture():
        if "+0.0h" not in _project_pace(50.0, RESET_AT_MIDWEEK, week_sec):
            failures.append("50% mid-week pace expected +0.0h")
        if "+168.0h" not in _project_pace(25.0, RESET_AT_MIDWEEK, week_sec):
            failures.append("25% mid-week surplus pace expected +168.0h")
        if "-56.0h" not in _project_pace(75.0, RESET_AT_MIDWEEK, week_sec):
            failures.append("75% mid-week deficit pace expected -56.0h")
        for u, r in [
            (0.0, RESET_AT_MIDWEEK),
            (-5.0, RESET_AT_MIDWEEK),
            (None, RESET_AT_MIDWEEK),
            (50.0, None),
            (50.0, NOW - 10),
            (50.0, NOW + 8 * 86400),
        ]:
            if _project_pace(u, r, week_sec) != "":
                failures.append(f"edge case pace ({u!r}, {r!r}) must be empty")


def _check_format_fable_quota(failures):
    with _Fixture() as fx:
        if format_fable_quota() != "":
            failures.append("cold cache must render empty")
        fx.write_cache(54.0, resets_at_unix=RESET_AT_MIDWEEK)
        out = format_fable_quota(show_pace=True)
        if "fable:" not in out or "54%" not in out:
            failures.append(f"warm cache output missing components: {out!r}")
        out_no_pace = format_fable_quota(show_pace=False)
        if (
            "fable:" not in out_no_pace
            or "54%" not in out_no_pace
            or "h" in out_no_pace
        ):
            failures.append(f"show_pace=False output invalid: {out_no_pace!r}")
        for k, v in [
            ("STATUSLINE_FABLE_QUOTA", "off"),
            ("STATUSLINE_FABLE_QUOTA_HOST", "off"),
            ("STATUSLINE_FABLE_QUOTA_HOST", "0"),
        ]:
            fx.set_env(**{k: v})
            if format_fable_quota() != "":
                failures.append(f"{k}={v} must disable output")


class _TestHttpHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the dashboard. GET /api/quota/providers is the legacy
    route; POST /api/quota/observed is the push-capable route the module now
    tries first. By default POST mirrors GET's response_code/response_body
    (matching the real endpoint's "identical report shape" contract), so
    every pre-existing test that only sets response_code/response_body keeps
    exercising a real success/failure response without change. Setting
    observed_status_override simulates a dashboard that predates the observed
    route (a bare 404/405, independent of the providers response), which is
    what exercises the fallback path. Every POST body received is recorded in
    received_observed_bodies so tests can assert rate_limits was pushed."""

    response_code = _HTTP_OK
    response_body = b"{}"
    observed_status_override = None
    received_observed_bodies: ClassVar[list] = []

    def do_GET(self):
        if self.path == "/api/quota/providers":
            self.send_response(self.response_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.response_body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/api/quota/observed":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with contextlib.suppress(ValueError):
            _TestHttpHandler.received_observed_bodies.append(
                json.loads(raw.decode("utf-8")) if raw else {}
            )
        if _TestHttpHandler.observed_status_override is not None:
            self.send_response(_TestHttpHandler.observed_status_override)
            self.end_headers()
            return
        self.send_response(self.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *args):
        pass


def _check_fetch_and_refresh_with_http_server(failures):
    with socketserver.TCPServer(("127.0.0.1", 0), _TestHttpHandler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            valid = {
                "providers": [
                    {
                        "provider": "anthropic-sub",
                        "pool": "fable",
                        "windows": [
                            {
                                "name": "seven_day",
                                "headroom_fraction": 0.46,
                                "resets_at": RESET_AT_MIDWEEK_ISO,
                            }
                        ],
                    }
                ]
            }
            _TestHttpHandler.response_code = _HTTP_OK
            _TestHttpHandler.response_body = json.dumps(valid).encode("utf-8")
            url = f"http://127.0.0.1:{port}/api/quota/providers"
            if _fetch_quota_payload(url, timeout=2.0) != valid:
                failures.append("_fetch_quota_payload success mismatch")

            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(0)
                if "54%" not in format_fable_quota():
                    failures.append(
                        "refresh_fable_quota_cache failed to populate cache"
                    )

            for code, body in [(204, b""), (500, b"error")]:
                _TestHttpHandler.response_code = code
                _TestHttpHandler.response_body = body
                if _fetch_quota_payload(url, timeout=2.0) is not None:
                    failures.append(f"{code} response must return None")

            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(0)
                if format_fable_quota() != "":
                    failures.append("refresh on HTTP error must leave cache empty")
                cached = _fable_quota_cached(NOW)
                if cached is None or not cached.get("failed"):
                    failures.append("refresh on HTTP error must write failure cache")

            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST="off")
                refresh_fable_quota_cache(0)
                if format_fable_quota() != "":
                    failures.append("refresh with disabled host must do nothing")

            _TestHttpHandler.response_code = _HTTP_OK
            _TestHttpHandler.response_body = b'{"providers": []}'
            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(0)
                if format_fable_quota() != "":
                    failures.append("refresh with no fable pool must not write cache")
                cached = _fable_quota_cached(NOW)
                if cached is None or not cached.get("failed"):
                    failures.append(
                        "refresh with no fable pool must write failure cache"
                    )
        finally:
            server.shutdown()


def _check_failure_preserves_stale_value(failures):
    """Stale-beats-blank: a failed refresh over a previously good entry must
    keep serving the stale value while the failure marker bounds detached-child
    respawns."""
    with socketserver.TCPServer(("127.0.0.1", 0), _TestHttpHandler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for code, body, label in (
                (500, b"error", "failed refresh"),
                (_HTTP_OK, b'{"providers": []}', "fable-pool-absent refresh"),
            ):
                _TestHttpHandler.response_code = code
                _TestHttpHandler.response_body = body
                with _Fixture() as fx:
                    fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                    fx.write_cache(54.0, resets_at_unix=RESET_AT_MIDWEEK)
                    refresh_fable_quota_cache(0)
                    out = format_fable_quota()
                    if "54%" not in out:
                        failures.append(
                            f"{label} must keep serving the stale value: {out!r}"
                        )
                    cached = _fable_quota_cached(NOW)
                    if cached is None or not cached.get("failed"):
                        failures.append(f"{label} must still mark the entry failed")
        finally:
            server.shutdown()


def _check_observed_push_and_fallback(failures):
    """POST /api/quota/observed is the primary route now: the pushed
    rate_limits must reach the request body, a missing rate_limits must still
    succeed (no rate_limits key, no error), a dashboard that answers 404/405
    on the observed route (predates it) must fall back to the legacy
    GET /api/quota/providers route, and the response is parsed by the exact
    same extraction path either way."""
    with socketserver.TCPServer(("127.0.0.1", 0), _TestHttpHandler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            valid = {
                "providers": [
                    {
                        "provider": "anthropic-sub",
                        "pool": "fable",
                        "windows": [
                            {
                                "name": "seven_day",
                                "headroom_fraction": 0.46,
                                "resets_at": RESET_AT_MIDWEEK_ISO,
                            }
                        ],
                    }
                ]
            }
            _TestHttpHandler.response_code = _HTTP_OK
            _TestHttpHandler.response_body = json.dumps(valid).encode("utf-8")
            _TestHttpHandler.observed_status_override = None

            sample_rate_limits = {
                "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
            }

            # The pushed rate_limits reach the POST body untouched.
            _TestHttpHandler.received_observed_bodies.clear()
            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(sample_rate_limits)
                if "54%" not in format_fable_quota():
                    failures.append("observed push: refresh failed to populate cache")
            if _TestHttpHandler.received_observed_bodies != [
                {"rate_limits": sample_rate_limits}
            ]:
                failures.append(
                    "observed push: rate_limits not carried in POST body, got "
                    f"{_TestHttpHandler.received_observed_bodies!r}"
                )

            # Missing rate_limits: no key in the body, still succeeds.
            _TestHttpHandler.received_observed_bodies.clear()
            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(None)
                if "54%" not in format_fable_quota():
                    failures.append(
                        "observed push: missing rate_limits must still succeed"
                    )
            if _TestHttpHandler.received_observed_bodies != [{}]:
                failures.append(
                    "observed push: missing rate_limits must send no "
                    f"rate_limits key, got {_TestHttpHandler.received_observed_bodies!r}"
                )

            # 404 on the observed route falls back to the legacy GET route.
            for status in (404, 405):
                _TestHttpHandler.observed_status_override = status
                _TestHttpHandler.received_observed_bodies.clear()
                with _Fixture() as fx:
                    fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                    refresh_fable_quota_cache(sample_rate_limits)
                    if "54%" not in format_fable_quota():
                        failures.append(
                            f"observed {status}: must fall back to GET /providers"
                        )
                if not _TestHttpHandler.received_observed_bodies:
                    failures.append(
                        f"observed {status}: POST must still have been attempted"
                    )
            _TestHttpHandler.observed_status_override = None

            # A non-404/405 observed failure must not retry GET: point the
            # legacy route at a payload that would succeed if it were reached,
            # confirming the failure was negative-cached directly instead.
            _TestHttpHandler.observed_status_override = 500
            _TestHttpHandler.received_observed_bodies.clear()
            with _Fixture() as fx:
                fx.set_env(STATUSLINE_FABLE_QUOTA_HOST=f"127.0.0.1:{port}")
                refresh_fable_quota_cache(sample_rate_limits)
                if format_fable_quota() != "":
                    failures.append(
                        "observed 500: must not fall back to GET /providers"
                    )
                cached = _fable_quota_cached(NOW)
                if cached is None or not cached.get("failed"):
                    failures.append("observed 500: must write failure cache")
            _TestHttpHandler.observed_status_override = None
        finally:
            server.shutdown()


def _check_observed_post_transport_failures(failures):
    """The POST helper must treat a returned non-200 response and a transport
    timeout as direct failures that do not request the legacy GET fallback."""
    saved_urlopen = fable_quota.urllib.request.urlopen
    try:
        response = types.SimpleNamespace(status=204)
        fable_quota.urllib.request.urlopen = lambda *_arguments, **_keyword_arguments: (
            contextlib.nullcontext(response)
        )
        result = fable_quota._post_quota_observed(
            "http://dashboard/api/quota/observed", None
        )
        if result != (None, False):
            failures.append(f"observed non-200 response: got {result!r}")

        def raise_timeout(*_arguments, **_keyword_arguments):
            raise TimeoutError("dashboard request timed out")

        fable_quota.urllib.request.urlopen = raise_timeout
        result = fable_quota._post_quota_observed(
            "http://dashboard/api/quota/observed", None
        )
        if result != (None, False):
            failures.append(f"observed transport timeout: got {result!r}")
    finally:
        fable_quota.urllib.request.urlopen = saved_urlopen


def _check_unreachable_endpoint_handling(failures):
    start = time.monotonic()
    if (
        _fetch_quota_payload("http://127.0.0.1:1/api/quota/providers", timeout=0.5)
        is not None
    ):
        failures.append("unreachable endpoint must return None")
    if (time.monotonic() - start) > 2.0:
        failures.append("unreachable endpoint timed out")


def _check_swr_cache_mechanics(failures):
    spawned = []
    with _Fixture() as fx:
        saved_spawn = maybe_spawn_refresh
        fable_quota.maybe_spawn_refresh = lambda kind, arg: spawned.append((kind, arg))
        try:
            if _fable_quota_cached(NOW) is not None or spawned != [
                ("fable-quota", None)
            ]:
                failures.append("missing cache must spawn and return None")
            spawned.clear()

            fx.write_cache(50.0, cached_at_unix=NOW - 5)
            fresh = _fable_quota_cached(NOW)
            if fresh is None or fresh.get("used_percentage") != 50.0 or spawned:
                failures.append("fresh cache must return entry without spawn")
            spawned.clear()

            fx.write_cache(50.0, cached_at_unix=NOW - 305)
            stale = _fable_quota_cached(NOW)
            if (
                stale is None
                or stale.get("used_percentage") != 50.0
                or spawned != [("fable-quota", None)]
            ):
                failures.append("stale cache must return entry and spawn")
            spawned.clear()

            fx.write_cache(None, cached_at_unix=NOW - 30, failed=True)
            fail_fresh = _fable_quota_cached(NOW)
            if fail_fresh is None or not fail_fresh.get("failed") or spawned:
                failures.append("fresh failure cache must not spawn")
            spawned.clear()

            fx.write_cache(None, cached_at_unix=NOW - 65, failed=True)
            fail_stale = _fable_quota_cached(NOW)
            if (
                fail_stale is None
                or not fail_stale.get("failed")
                or spawned != [("fable-quota", None)]
            ):
                failures.append("stale failure cache must spawn")
        finally:
            fable_quota.maybe_spawn_refresh = saved_spawn


def _check_adapter_integrations(failures):
    with _Fixture() as fx:
        fx.write_cache(54.0, resets_at_unix=RESET_AT_MIDWEEK)

        _l1, qwen_l2 = render_qwen_statusline(
            {"model": "qwen-max", "contextTokens": 1000, "maxContextTokens": 32000},
            "/cwd",
            "⠋",
        )
        if "fable:" not in qwen_l2 or "54%" not in qwen_l2:
            failures.append(f"qwen statusline missing fable: {qwen_l2!r}")

        kimi_l = render_kimi_statusline(
            {"model": "moonshot-v1", "contextTokens": 500, "maxContextTokens": 32000},
            "/cwd",
            "⠋",
        )
        if "fable:" not in kimi_l or "54%" not in kimi_l:
            failures.append(f"kimi statusline missing fable: {kimi_l!r}")

        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(
            json.dumps(
                {"session_id": "s1", "cwd": "/cwd", "model": {"id": "claude-opus-4-8"}}
            )
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            statusline.main()
        sys.stdin = saved_stdin
        output = buf.getvalue()
        if "fable:" not in output or "54%" not in output:
            failures.append("statusline.py missing fable")


def _check_refresher_dispatch(failures):
    calls = []
    saved = fable_quota.refresh_fable_quota_cache
    fable_quota.refresh_fable_quota_cache = lambda arg: calls.append(arg)
    with _Fixture():
        try:
            run_refresh("fable-quota", 0)
        finally:
            fable_quota.refresh_fable_quota_cache = saved
    if calls != [0]:
        failures.append(f"run_refresh dispatch: expected [0], got {calls!r}")


def main():
    failures = []
    _check_quota_ttl_floor(failures)
    _check_now_unix_and_paths(failures)
    _check_safe_float(failures)
    _check_dashboard_host_resolution(failures)
    _check_dashboard_url_formatting(failures)
    _check_parse_timestamp(failures)
    _check_extract_captured_payload(failures)
    _check_extract_fallback_metrics(failures)
    _check_extract_malformed_payloads(failures)
    _check_pace_projection(failures)
    _check_format_fable_quota(failures)
    _check_fetch_and_refresh_with_http_server(failures)
    _check_failure_preserves_stale_value(failures)
    _check_observed_push_and_fallback(failures)
    _check_observed_post_transport_failures(failures)
    _check_unreachable_endpoint_handling(failures)
    _check_swr_cache_mechanics(failures)
    _check_adapter_integrations(failures)
    _check_refresher_dispatch(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: fable_quota module verified (host resolution, payload extraction, HTTP fetching, SWR cache, pace math, adapter integrations)"
    )


if __name__ == "__main__":
    main()
