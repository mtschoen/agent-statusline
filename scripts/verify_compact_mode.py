"""Verify compact-mode flag resolution: env modes, $COLUMNS auto-drop in
priority order, graceful fallback when $COLUMNS is unset."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Isolate _mode()'s pref() read from any real ~/.claude/.statusline-prefs.json
# so the STATUSLINE_COMPACT env these tests set is what resolves.
os.environ["STATUSLINE_PREFS_PATH"] = os.devnull
from statusline_lib.base import GREEN, RED, RESET
from statusline_lib.compact import (
    DROP_ORDER,
    resolve_flags,
    visible_width,
)


# Stub renderer: base width 50, each enabled flag adds 10 visible chars, wrapped
# in ANSI so visible_width's stripping is exercised too.
def _stub_render(flags):
    body = "x" * 50 + "y" * (10 * sum(1 for v in flags.values() if v))
    return f"{RED}{body}{RESET}"


def _with_env(overrides, fn):
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _check_visible_width(failures):
    if visible_width(f"{GREEN}abc{RESET}") != 3:
        failures.append("visible_width must strip ANSI")


def _check_force_modes(failures):
    full = _with_env({"STATUSLINE_COMPACT": "0"}, lambda: resolve_flags(_stub_render))
    if not all(full.values()):
        failures.append(f"mode 0 must keep all flags on; got {full}")
    compact = _with_env(
        {"STATUSLINE_COMPACT": "1"}, lambda: resolve_flags(_stub_render)
    )
    if any(compact.values()):
        failures.append(f"mode 1 must turn all flags off; got {compact}")


def _check_auto_unset_columns(failures):
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": None},
        lambda: resolve_flags(_stub_render),
    )
    if not all(flags.values()):
        failures.append(f"auto with no $COLUMNS must stay full; got {flags}")


def _check_auto_drops_in_order(failures):
    # Full width 50 + 11*10 = 160; COLUMNS=75 sheds the first nine (down to 70).
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": "75"},
        lambda: resolve_flags(_stub_render),
    )
    expected = {
        "cache_output": False,
        "cache_input": False,
        "lines": False,
        "cache_costs": False,
        "burn_target": False,
        "cache_hit": False,
        "quota_pace": False,
        "ttl_wasted": False,
        "burn_rate": False,
        "context_pct": True,
        "context_denom": True,
    }
    if flags != expected:
        failures.append(f"auto drop order wrong; got {flags}, want {expected}")


def _check_auto_super_minimal(failures):
    # Width 55 forces the super-minimal tier: every droppable flag off (base 50).
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": "55"},
        lambda: resolve_flags(_stub_render),
    )
    if any(flags.values()):
        failures.append(f"super-minimal width must drop every flag; got {flags}")


def _check_never_drops_protected(failures):
    # Protected items are simply absent from DROP_ORDER. The live rate is NOT
    # among them: it drops in the super-minimal tier via burn_rate.
    for protected in ("ttl_count", "cost", "context", "model"):
        if protected in DROP_ORDER:
            failures.append(f"{protected} must never be droppable")


def _check_columns_invalid_string(failures):
    # compact.py lines 61-62: _columns() returns None when COLUMNS is non-numeric.
    # Exercised via resolve_flags in auto mode: with an invalid COLUMNS value the
    # width check cannot proceed, so all flags stay on (same as unset).
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": "notanumber"},
        lambda: resolve_flags(_stub_render),
    )
    if not all(flags.values()):
        failures.append(
            f"invalid COLUMNS string should behave as unset (all flags on); got {flags}"
        )


def _check_columns_zero_or_negative(failures):
    # compact.py line 63: _columns() returns None when cols <= 0. Exercised the
    # same way as the invalid-string path: all flags stay on.
    for value in ("0", "-1", "-100"):
        flags = _with_env(
            {"STATUSLINE_COMPACT": "auto", "COLUMNS": value},
            lambda: resolve_flags(_stub_render),
        )
        if not all(flags.values()):
            failures.append(
                f"COLUMNS={value!r} (non-positive) should behave as unset; got {flags}"
            )


def _check_terminal_columns(failures):
    # compact.py line 72: terminal_columns() is the public accessor for _columns().
    from statusline_lib.compact import terminal_columns

    result_unset = _with_env({"COLUMNS": None}, terminal_columns)
    if result_unset is not None:
        failures.append(
            f"terminal_columns() with no COLUMNS should return None; got {result_unset!r}"
        )
    result_set = _with_env({"COLUMNS": "200"}, terminal_columns)
    if result_set != 200:
        failures.append(
            f"terminal_columns() with COLUMNS=200 should return 200; got {result_set!r}"
        )


def _check_payload_width_fallback_when_columns_unset(failures):
    # No $COLUMNS at all (e.g. Antigravity CLI's harness, which carries width
    # in the stdin payload instead): payload_width should be used as if it
    # were $COLUMNS.
    from statusline_lib.compact import terminal_columns

    result = _with_env({"COLUMNS": None}, lambda: terminal_columns(payload_width=100))
    if result != 100:
        failures.append(
            f"terminal_columns: should fall back to payload_width, got {result!r}"
        )


def _check_columns_env_wins_over_payload_width(failures):
    from statusline_lib.compact import terminal_columns

    result = _with_env({"COLUMNS": "200"}, lambda: terminal_columns(payload_width=100))
    if result != 200:
        failures.append(
            f"terminal_columns: $COLUMNS must win over payload_width when both set, got {result!r}"
        )


def _check_payload_width_invalid_or_absent(failures):
    from statusline_lib.compact import terminal_columns

    if (
        _with_env({"COLUMNS": None}, lambda: terminal_columns(payload_width=None))
        is not None
    ):
        failures.append(
            "terminal_columns: no $COLUMNS and no payload_width should return None"
        )
    if (
        _with_env(
            {"COLUMNS": None}, lambda: terminal_columns(payload_width="not-a-number")
        )
        is not None
    ):
        failures.append(
            "terminal_columns: non-numeric payload_width should return None"
        )
    if (
        _with_env({"COLUMNS": None}, lambda: terminal_columns(payload_width=0))
        is not None
    ):
        failures.append(
            "terminal_columns: non-positive payload_width should return None"
        )


def _check_resolve_flags_uses_payload_width(failures):
    # Same drop-order fixture as _check_auto_drops_in_order, but sourced from
    # payload_width instead of $COLUMNS.
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": None},
        lambda: resolve_flags(_stub_render, payload_width=75),
    )
    expected = {
        "cache_output": False,
        "cache_input": False,
        "lines": False,
        "cache_costs": False,
        "burn_target": False,
        "cache_hit": False,
        "quota_pace": False,
        "ttl_wasted": False,
        "burn_rate": False,
        "context_pct": True,
        "context_denom": True,
    }
    if flags != expected:
        failures.append(
            f"resolve_flags: payload_width drop order wrong; got {flags}, want {expected}"
        )


def check(failures):
    _check_visible_width(failures)
    _check_force_modes(failures)
    _check_auto_unset_columns(failures)
    _check_auto_drops_in_order(failures)
    _check_auto_super_minimal(failures)
    _check_never_drops_protected(failures)
    _check_columns_invalid_string(failures)
    _check_columns_zero_or_negative(failures)
    _check_terminal_columns(failures)
    _check_payload_width_fallback_when_columns_unset(failures)
    _check_columns_env_wins_over_payload_width(failures)
    _check_payload_width_invalid_or_absent(failures)
    _check_resolve_flags_uses_payload_width(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: compact-mode flags resolve by env mode and $COLUMNS in drop order")


if __name__ == "__main__":
    main()
