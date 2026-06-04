"""Verify the live-prefs resolver: prefs-file > env > default precedence,
JSON-null as absent, the bool parser, and graceful {} on a broken file.

Writes a real temp prefs file and points STATUSLINE_PREFS_PATH at it, so the
resolver's file read is exercised end to end. Run from anywhere.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.prefs as prefs


def _with_prefs(file_contents, env, fn):
    """Run fn() with the prefs file holding `file_contents` (raw str, or None to
    leave no file) and `env` applied to the relevant STATUSLINE_* vars (value
    None pops the var). Restores both afterward."""
    fd, path = tempfile.mkstemp(prefix="statusline-prefs-", suffix=".json")
    os.close(fd)
    if file_contents is None:
        os.remove(path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(file_contents)
    touched = {"STATUSLINE_PREFS_PATH": os.environ.get("STATUSLINE_PREFS_PATH")}
    for k in env:
        touched[k] = os.environ.get(k)
    os.environ["STATUSLINE_PREFS_PATH"] = path
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return fn()
    finally:
        for k, v in touched.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if os.path.exists(path):
            os.remove(path)


def _check_precedence(failures):
    # Prefs file wins over env.
    got = _with_prefs(
        json.dumps({"STATUSLINE_TARGET_RATE": "0.5"}),
        {"STATUSLINE_TARGET_RATE": "2"},
        lambda: prefs.pref("STATUSLINE_TARGET_RATE"),
    )
    if got != "0.5":
        failures.append(f"prefs file should win over env; got {got!r}")
    # Env used when the key is absent from the prefs file.
    got = _with_prefs(
        json.dumps({"STATUSLINE_COMPACT": "always"}),
        {"STATUSLINE_TARGET_RATE": "2"},
        lambda: prefs.pref("STATUSLINE_TARGET_RATE"),
    )
    if got != "2":
        failures.append(f"env should be used when prefs omits the key; got {got!r}")
    # Default used when neither has it.
    got = _with_prefs(
        json.dumps({}),
        {"STATUSLINE_TARGET_RATE": None},
        lambda: prefs.pref("STATUSLINE_TARGET_RATE", "1.0"),
    )
    if got != "1.0":
        failures.append(f"default should apply when unset everywhere; got {got!r}")


def _check_null_is_absent(failures):
    # A JSON null in the prefs file means "fall through to env", not "the value
    # is None" -- so a writer can re-expose the baseline without deleting keys.
    got = _with_prefs(
        json.dumps({"STATUSLINE_TARGET_RATE": None}),
        {"STATUSLINE_TARGET_RATE": "2"},
        lambda: prefs.pref("STATUSLINE_TARGET_RATE"),
    )
    if got != "2":
        failures.append(f"null prefs value should fall through to env; got {got!r}")


def _check_non_string_coerced(failures):
    # JSON numbers/bools are coerced to str so downstream float()/truthy parsing
    # (which expects env-style strings) is unchanged.
    got = _with_prefs(
        json.dumps({"STATUSLINE_DAILY_BUDGET": 100}),
        {"STATUSLINE_DAILY_BUDGET": None},
        lambda: prefs.pref("STATUSLINE_DAILY_BUDGET"),
    )
    if got != "100":
        failures.append(f"numeric prefs value should coerce to '100'; got {got!r}")


def _check_pref_bool(failures):
    cases = [
        ("on", True),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("off", False),
        ("0", False),
        ("no", False),
    ]
    for raw, expected in cases:
        got = _with_prefs(
            json.dumps({"STATUSLINE_BEACON": raw}),
            {},
            lambda: prefs.pref_bool("STATUSLINE_BEACON", default=True),
        )
        if got is not expected:
            failures.append(f"pref_bool({raw!r}) -> {got!r}, expected {expected!r}")
    # Unset -> the given default; unrecognized -> the given default.
    unset = _with_prefs(
        json.dumps({}),
        {"STATUSLINE_BEACON": None},
        lambda: prefs.pref_bool("STATUSLINE_BEACON", default=True),
    )
    if unset is not True:
        failures.append(f"unset pref_bool should use default=True; got {unset!r}")
    junk = _with_prefs(
        json.dumps({"STATUSLINE_BEACON": "maybe"}),
        {},
        lambda: prefs.pref_bool("STATUSLINE_BEACON", default=False),
    )
    if junk is not False:
        failures.append(f"unrecognized pref_bool should use default; got {junk!r}")


def _check_broken_file_is_empty(failures):
    # Malformed JSON, a non-object top level, and a missing file all degrade to
    # {} (env/default still resolve) rather than raising mid-render.
    for contents, why in [
        ("{not json", "malformed"),
        ("[1,2,3]", "non-object"),
        (None, "missing file"),
    ]:
        got = _with_prefs(
            contents,
            {"STATUSLINE_TARGET_RATE": "2"},
            lambda: (prefs.load_prefs(), prefs.pref("STATUSLINE_TARGET_RATE")),
        )
        if got[0] != {} or got[1] != "2":
            failures.append(f"{why} prefs should degrade to env; got {got!r}")


def check(failures):
    _check_precedence(failures)
    _check_null_is_absent(failures)
    _check_non_string_coerced(failures)
    _check_pref_bool(failures)
    _check_broken_file_is_empty(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: prefs resolver honors prefs>env>default, null-as-absent, bool parse")


if __name__ == "__main__":
    main()
