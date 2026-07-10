"""Codex CLI status-line preset and config.toml merge helpers.

Codex owns its TUI footer and accepts an ordered list of built-in item IDs; it
does not invoke an external command with a JSON payload.  These helpers install
the closest native equivalent while preserving unrelated config text.
"""

import re
import tomllib

CODEX_STATUS_LINE_ITEMS = (
    "run-state",
    "current-dir",
    "git-branch",
    "pull-request-number",
    "model-with-reasoning",
    "context-used",
    "five-hour-limit",
    "weekly-limit",
    "used-tokens",
    "total-input-tokens",
    "total-output-tokens",
    "branch-changes",
    "permissions",
    "approval-mode",
    "fast-mode",
    "thread-title",
    "task-progress",
)

_TABLE_HEADER = re.compile(
    r"(?m)^[ \t]*\[(?!\[)([^\]\r\n]+)\][ \t]*(?:#.*)?(?:\r?\n|$)"
)


def _parse_config(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML: {exc}") from exc


def _newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _settings_text(newline, dotted=False):
    prefix = "tui." if dotted else ""
    items = "".join(f'  "{item}",{newline}' for item in CODEX_STATUS_LINE_ITEMS)
    return (
        f"{prefix}status_line = [{newline}"
        f"{items}]"
        f"{newline}{prefix}status_line_use_colors = true{newline}"
    )


def _table_bounds(text, name):
    headers = list(_TABLE_HEADER.finditer(text))
    for index, match in enumerate(headers):
        table_name = match.group(1).strip().strip("'\"")
        if table_name == name:
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            return match.end(), end
    return None


def _array_value_end(text, start, limit):
    depth = 0
    quote = None
    escaped = False
    comment = False
    for index in range(start, limit):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            comment = True
        elif char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("could not find the end of a status_line array")


def _assignment_span(text, key, start, end):
    assignment = re.compile(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=").search(
        text, start, end
    )
    if assignment is None:
        return None
    value_start = assignment.end()
    while value_start < end and text[value_start] in " \t\r\n":
        value_start += 1
    value_end = (
        _array_value_end(text, value_start, end)
        if value_start < end and text[value_start] == "["
        else value_start
    )
    line_end = text.find("\n", value_end, end)
    return assignment.start(), end if line_end < 0 else line_end + 1


def _without_assignments(text, keys, start, end):
    spans = [
        span
        for key in keys
        if (span := _assignment_span(text, key, start, end)) is not None
    ]
    for span_start, span_end in sorted(spans, reverse=True):
        text = text[:span_start] + text[span_end:]
    return text, spans


def _merge_unchecked(text):
    newline = _newline(text)
    keys = ("status_line", "status_line_use_colors")
    section = _table_bounds(text, "tui")
    if section is not None:
        content_start, content_end = section
        text, _spans = _without_assignments(text, keys, content_start, content_end)
        separator = (
            "" if content_start == 0 or text[content_start - 1] == "\n" else newline
        )
        return (
            text[:content_start]
            + separator
            + _settings_text(newline)
            + text[content_start:]
        )

    dotted_keys = tuple(f"tui.{key}" for key in keys)
    text_without, spans = _without_assignments(text, dotted_keys, 0, len(text))
    if spans:
        insertion = min(span[0] for span in spans)
        return (
            text_without[:insertion]
            + _settings_text(newline, dotted=True)
            + text_without[insertion:]
        )

    inline_tui_keys = ("tui", '"tui"', "'tui'")
    if any(
        _assignment_span(text, key, 0, len(text)) is not None for key in inline_tui_keys
    ):
        raise ValueError(
            "the existing tui value uses an unsupported inline-table form; "
            "convert it to a [tui] section first"
        )
    separator = "" if not text or text.endswith(("\n", "\r")) else newline
    blank = "" if not text.strip() else newline
    return text + separator + blank + f"[tui]{newline}" + _settings_text(newline)


def merge_codex_config(text):
    """Return config TOML with the native Codex status-line preset installed."""
    _parse_config(text)
    merged = _merge_unchecked(text)
    parsed = _parse_config(merged)
    tui = parsed.get("tui") or {}
    if tuple(tui.get("status_line") or ()) != CODEX_STATUS_LINE_ITEMS:
        raise ValueError("merged TOML did not retain the Codex status-line preset")
    return merged


def codex_config_current(text):
    """Return whether config TOML already has the complete native preset."""
    parsed = _parse_config(text)
    tui = parsed.get("tui") or {}
    return (
        tuple(tui.get("status_line") or ()) == CODEX_STATUS_LINE_ITEMS
        and tui.get("status_line_use_colors") is True
    )
