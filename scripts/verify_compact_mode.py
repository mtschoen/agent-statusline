"""Verify compact-mode flag resolution: env modes, $COLUMNS auto-drop in
priority order, graceful fallback when $COLUMNS is unset."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    # Full width = 50 + 5*10 = 100. COLUMNS=75 -> drop cache_costs(90),
    # burn_target(80), cache_hit(70<=75 stop). First three off, last two on.
    flags = _with_env(
        {"STATUSLINE_COMPACT": "auto", "COLUMNS": "75"},
        lambda: resolve_flags(_stub_render),
    )
    expected = {
        "cache_costs": False,
        "burn_target": False,
        "cache_hit": False,
        "quota_pace": True,
        "ttl_wasted": True,
    }
    if flags != expected:
        failures.append(f"auto drop order wrong; got {flags}, want {expected}")


def _check_never_drops_protected(failures):
    # The protected items (ttl count, cost, context, model, rate) are simply not
    # members of DROP_ORDER -> they can never be dropped.
    for protected in ("ttl_count", "cost", "context", "model", "rate"):
        if protected in DROP_ORDER:
            failures.append(f"{protected} must never be droppable")


def check(failures):
    _check_visible_width(failures)
    _check_force_modes(failures)
    _check_auto_unset_columns(failures)
    _check_auto_drops_in_order(failures)
    _check_never_drops_protected(failures)


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
