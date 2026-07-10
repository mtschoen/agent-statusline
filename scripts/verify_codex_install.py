"""Verify the Codex config.toml status-line preset merge."""

import os
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.codex_install as codex_install
from statusline_lib.codex_install import (
    CODEX_STATUS_LINE_ITEMS,
    codex_config_current,
    merge_codex_config,
)


def _expect_value_error(failures, label, text, message):
    try:
        merge_codex_config(text)
    except ValueError as exc:
        if message not in str(exc):
            failures.append(f"{label}: wrong error: {exc}")
    else:
        failures.append(f"{label}: expected ValueError")


def check(failures):
    try:
        codex_install._array_value_end("[", 0, 1)
    except ValueError as exc:
        if "end of a status_line array" not in str(exc):
            failures.append(f"array guard: wrong error: {exc}")
    else:
        failures.append("array guard: unterminated array should fail")

    empty = merge_codex_config("")
    empty_data = tomllib.loads(empty)
    if tuple(empty_data["tui"]["status_line"]) != CODEX_STATUS_LINE_ITEMS:
        failures.append("empty config: preset was not appended")
    if not codex_config_current(empty):
        failures.append("empty config: merged result should be current")

    existing = (
        'model = "gpt-5.5"\r\n'
        "\r\n"
        "[tui] # preserve this section\r\n"
        "animations = false\r\n"
        "status_line = [\r\n"
        '  ["nested legacy"], # comment with ]\r\n'
        "  'single quoted',\r\n"
        '  "escaped \\" value",\r\n'
        "] # old preset\r\n"
        "status_line_use_colors = false\r\n"
        "\r\n"
        "[history]\r\n"
        'persistence = "none"\r\n'
    )
    merged = merge_codex_config(existing)
    data = tomllib.loads(merged)
    if data["model"] != "gpt-5.5" or data["history"]["persistence"] != "none":
        failures.append("section merge: unrelated settings changed")
    if data["tui"]["animations"] is not False:
        failures.append("section merge: sibling tui setting changed")
    if "\r\n" not in merged or "nested legacy" in merged:
        failures.append("section merge: line endings or old array not replaced")

    dotted = (
        'tui.status_line = "legacy"\n'
        "tui.status_line_use_colors = false\n"
        'model = "gpt-5.5"\n'
    )
    dotted_merged = merge_codex_config(dotted)
    if "tui.status_line = [" not in dotted_merged:
        failures.append("dotted merge: dotted form was not retained")
    if not codex_config_current(dotted_merged):
        failures.append("dotted merge: result should be current")

    nested = (
        '[tui.model_availability_nux]\n"gpt-5.5" = 4\n\n'
        '[history]\npersistence = "save-all"\n'
    )
    nested_merged = merge_codex_config(nested)
    nested_data = tomllib.loads(nested_merged)
    if nested_data["tui"]["model_availability_nux"]["gpt-5.5"] != 4:
        failures.append("nested table: existing child setting changed")
    if not codex_config_current(nested_merged):
        failures.append("nested table: parent [tui] preset was not appended")

    if codex_config_current('[tui]\nstatus_line = ["model"]\n'):
        failures.append("current check: incomplete preset reported current")

    _expect_value_error(failures, "invalid", "[tui\n", "invalid TOML")
    _expect_value_error(
        failures,
        "inline table",
        "tui = { animations = false }\n",
        "unsupported inline-table form",
    )

    real_settings_text = codex_install._settings_text
    try:
        codex_install._settings_text = lambda _newline, dotted=False: (
            f'{"tui." if dotted else ""}status_line = ["model"]\n'
        )
        _expect_value_error(
            failures,
            "integrity guard",
            "[tui]\n",
            "did not retain the Codex status-line preset",
        )
    finally:
        codex_install._settings_text = real_settings_text


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: Codex native status-line preset merges idempotently into config.toml")


if __name__ == "__main__":
    main()
