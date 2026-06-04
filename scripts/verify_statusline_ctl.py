"""Verify statusline-ctl: set/reset round-trips through the prefs file, value
normalization per key, and that the status line's resolver sees the writes.

Points STATUSLINE_PREFS_PATH at a temp file so the real prefs file is untouched.
Run from anywhere.
"""

import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from statusline_lib.prefs import load_prefs, pref

# statusline_ctl.py is a root entry script (not a package module), so load it by
# path -- same pattern verify_hide_cost.py uses for statusline.py.
_spec = importlib.util.spec_from_file_location(
    "statusline_ctl", os.path.join(_ROOT, "statusline_ctl.py")
)
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)


def _run(args):
    """Invoke the CLI with argv `args`; return its exit code."""
    return ctl.main(args)


def _check_set_reset_roundtrip(failures):
    code = _run(["set", "compact", "always"])
    if code != 0:
        failures.append(f"set compact always should exit 0; got {code}")
    if load_prefs().get("STATUSLINE_COMPACT") != "always":
        failures.append("set should persist STATUSLINE_COMPACT=always to the file")
    if pref("STATUSLINE_COMPACT") != "always":
        failures.append("the resolver should see the written value")
    _run(["reset", "compact"])
    if "STATUSLINE_COMPACT" in load_prefs():
        failures.append("reset should remove the key from the prefs file")


def _check_cost_inversion(failures):
    # cost on = SHOW -> HIDE_COST 0; cost off = HIDE -> HIDE_COST 1.
    _run(["set", "cost", "off"])
    if load_prefs().get("STATUSLINE_HIDE_COST") != "1":
        failures.append("cost off should store HIDE_COST=1")
    _run(["set", "cost", "on"])
    if load_prefs().get("STATUSLINE_HIDE_COST") != "0":
        failures.append("cost on should store HIDE_COST=0")
    _run(["reset", "cost"])


def _check_target_rate_values(failures):
    cases = [("0.5", "0.5"), ("auto", "auto"), ("off", "0")]
    for given, stored in cases:
        _run(["set", "target-rate", given])
        got = load_prefs().get("STATUSLINE_TARGET_RATE")
        if got != stored:
            failures.append(f"target-rate {given!r} -> {got!r}, expected {stored!r}")
    # Bad input is rejected (nonzero exit) and does not change the stored value.
    _run(["set", "target-rate", "auto"])
    code = _run(["set", "target-rate", "banana"])
    if code == 0:
        failures.append("target-rate banana should be rejected (nonzero exit)")
    if load_prefs().get("STATUSLINE_TARGET_RATE") != "auto":
        failures.append("a rejected set must not overwrite the prior value")
    code = _run(["set", "target-rate", "-3"])
    if code == 0:
        failures.append("a non-positive target-rate should be rejected")
    _run(["reset", "target-rate"])


def _check_unknown_key_and_command(failures):
    if _run(["set", "nonsense", "x"]) == 0:
        failures.append("an unknown key should be rejected")
    if _run(["frobnicate"]) == 0:
        failures.append("an unknown command should be rejected")
    if _run(["list"]) != 0:
        failures.append("list should exit 0")
    if _run([]) != 0:
        failures.append("no args (help) should exit 0")


def check(failures):
    _check_set_reset_roundtrip(failures)
    _check_cost_inversion(failures)
    _check_target_rate_values(failures)
    _check_unknown_key_and_command(failures)


def main():
    fd, tmp = tempfile.mkstemp(prefix="statusline-ctl-prefs-", suffix=".json")
    os.close(fd)
    os.remove(tmp)  # start with no file; the CLI creates it on first set
    real = os.environ.get("STATUSLINE_PREFS_PATH")
    os.environ["STATUSLINE_PREFS_PATH"] = tmp
    failures = []
    try:
        check(failures)
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_PREFS_PATH", None)
        else:
            os.environ["STATUSLINE_PREFS_PATH"] = real
        if os.path.exists(tmp):
            os.remove(tmp)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: statusline-ctl set/reset/normalize round-trips through the prefs file")


if __name__ == "__main__":
    main()
