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


def _check_preset_fields(failures):
    # Ids confirmed against openai/codex codex-rs/tui/src/bottom_pane/status_line_setup.rs
    # (StatusLineItem enum), 2026-07-11.
    richer_fields = {
        "pull-request-number",
        "total-input-tokens",
        "total-output-tokens",
        "permissions",
        "approval-mode",
        "fast-mode",
        "project-name",
        "context-window-size",
    }
    if not richer_fields.issubset(CODEX_STATUS_LINE_ITEMS):
        failures.append("preset: richer native telemetry fields are missing")
    # Deliberately excluded: thread-id is the full session UUID (too wide for
    # a footer); context-remaining duplicates context-used; reasoning
    # duplicates the suffix already carried by model-with-reasoning;
    # codex-version is static per install, not session telemetry;
    # raw-output is a rarely-toggled boolean; workspace-headline only
    # populates for Enterprise workspaces.
    excluded_fields = {
        "thread-id",
        "context-remaining",
        "reasoning",
        "codex-version",
        "raw-output",
        "workspace-headline",
    }
    present = excluded_fields & set(CODEX_STATUS_LINE_ITEMS)
    if present:
        failures.append(f"preset: deliberately-excluded fields present: {present}")


def _check_preset_order(failures):
    order = {item: index for index, item in enumerate(CODEX_STATUS_LINE_ITEMS)}
    # Mirrors this project's own line-2 rendering philosophy (statusline.py
    # _render_line2): model, then directory/git, then context usage, then
    # tokens, then rate limits.
    philosophy = [
        "model-with-reasoning",
        "current-dir",
        "context-used",
        "used-tokens",
        "five-hour-limit",
    ]
    indices = [order[item] for item in philosophy]
    if indices != sorted(indices):
        failures.append(
            "preset: item order does not mirror the model/dir/context/tokens/limits philosophy"
        )


def _check_array_guard(failures):
    try:
        codex_install._array_value_end("[", 0, 1)
    except ValueError as exc:
        if "end of a status_line array" not in str(exc):
            failures.append(f"array guard: wrong error: {exc}")
    else:
        failures.append("array guard: unterminated array should fail")


def _check_empty_config(failures):
    empty = merge_codex_config("")
    empty_data = tomllib.loads(empty)
    if tuple(empty_data["tui"]["status_line"]) != CODEX_STATUS_LINE_ITEMS:
        failures.append("empty config: preset was not appended")
    if not codex_config_current(empty):
        failures.append("empty config: merged result should be current")


def _check_section_merge_preserves_siblings(failures):
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


def _check_dotted_form(failures):
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


def _check_nested_child_table(failures):
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


def _check_no_tui_anywhere(failures):
    """A config with zero mention of tui (no [tui], no tui.* dotted keys, no
    [tui.child] table) must get a fresh [tui] section appended."""
    plain = 'model = "gpt-5.5"\n\n[history]\npersistence = "save-all"\n'
    merged = merge_codex_config(plain)
    data = tomllib.loads(merged)
    if data["model"] != "gpt-5.5" or data["history"]["persistence"] != "save-all":
        failures.append("no-tui config: unrelated settings changed")
    if not codex_config_current(merged):
        failures.append("no-tui config: preset was not appended")


def _check_incomplete_preset_not_current(failures):
    if codex_config_current('[tui]\nstatus_line = ["model"]\n'):
        failures.append("current check: incomplete preset reported current")


def _check_invalid_toml_error(failures):
    _expect_value_error(failures, "invalid", "[tui\n", "invalid TOML")


def _check_inline_table_error(failures):
    _expect_value_error(
        failures,
        "inline table",
        "tui = { animations = false }\n",
        "unsupported inline-table form",
    )


def _check_integrity_guard(failures):
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


def _check_idempotent_merge(failures):
    """Running the merge twice must produce a byte-identical file: the second
    pass strips its own freshly-written preset and rewrites the same bytes."""
    existing = (
        'model = "gpt-5.5"\n'
        "\n"
        "[tui]\n"
        "animations = false\n"
        'status_line = ["model", "current-dir"]\n'
        "status_line_use_colors = false\n"
        "\n"
        "[history]\n"
        'persistence = "none"\n'
    )
    once = merge_codex_config(existing)
    twice = merge_codex_config(once)
    if once != twice:
        failures.append("idempotency: second merge produced different bytes")


def _check_trailing_content_after_tui(failures):
    """[tui] immediately followed by a [tui.child] sub-table (not a sibling
    top-level table) must bound [tui]'s content at the child header, not
    swallow it, and extra keys on both sides of the preset assignments must
    survive."""
    text = (
        "[tui]\n"
        "animations = false\n"
        'status_line = ["model"]\n'
        "status_line_use_colors = false\n"
        "show_tooltips = true\n"
        "\n"
        '[tui.model_availability_nux]\n"gpt-5.5" = 1\n'
        "\n"
        "[history]\n"
        'persistence = "none"\n'
    )
    merged = merge_codex_config(text)
    data = tomllib.loads(merged)
    if data["tui"]["animations"] is not False:
        failures.append("trailing content: leading sibling key changed")
    if data["tui"]["show_tooltips"] is not True:
        failures.append("trailing content: trailing sibling key changed")
    if data["tui"]["model_availability_nux"]["gpt-5.5"] != 1:
        failures.append("trailing content: child table under [tui] changed")
    if data["history"]["persistence"] != "none":
        failures.append("trailing content: table after the child table changed")
    if not codex_config_current(merged):
        failures.append("trailing content: preset was not retained")


def _check_no_trailing_newline(failures):
    """[tui] as the last table in the file, with no trailing newline at EOF
    and an extra key after status_line_use_colors, must still merge cleanly."""
    text = (
        'model = "gpt-5.5"\n'
        "\n"
        "[tui]\n"
        "animations = false\n"
        'status_line = ["model"]\n'
        "status_line_use_colors = false\n"
        "extra_after = true"
    )
    merged = merge_codex_config(text)
    data = tomllib.loads(merged)
    if data["tui"]["animations"] is not False:
        failures.append("no trailing newline: leading sibling key changed")
    if data["tui"]["extra_after"] is not True:
        failures.append("no trailing newline: trailing key (no EOF newline) dropped")
    if not codex_config_current(merged):
        failures.append("no trailing newline: preset was not retained")


def check(failures):
    _check_preset_fields(failures)
    _check_preset_order(failures)
    _check_array_guard(failures)
    _check_empty_config(failures)
    _check_section_merge_preserves_siblings(failures)
    _check_dotted_form(failures)
    _check_nested_child_table(failures)
    _check_no_tui_anywhere(failures)
    _check_incomplete_preset_not_current(failures)
    _check_invalid_toml_error(failures)
    _check_inline_table_error(failures)
    _check_integrity_guard(failures)
    _check_idempotent_merge(failures)
    _check_trailing_content_after_tui(failures)
    _check_no_trailing_newline(failures)


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
