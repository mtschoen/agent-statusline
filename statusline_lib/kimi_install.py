"""Merge logic for Kimi Code CLI's tui.toml: a single `[status_line] command`
key, unlike Codex's native status_line item-list preset (codex_install.py).

Kimi's TUI reads `[status_line] command = "<shell command>"` from
~/.kimi-code/tui.toml and spawns it per render (see statusline_lib/kimi.py
for the full payload/timeout/first-line contract). The sibling `items` key
(the built-in footer slots) and every other setting are left alone -- only
`command` is managed.

Same regex-based text-surgery approach as codex_install.py, whose
_table_bounds/_newline/_parse_config helpers this reuses rather than
duplicating: no AST, but the merged text is re-parsed through tomllib and
the installed command checked before returning, so a scanner miscount
fails loudly with ValueError instead of silently corrupting the user's
config. Pure text-in/text-out helpers -- no file I/O, no printing -- so the
verify suite can exercise the merge directly and install.py stays the sole
place that touches disk or reports progress.
"""

import re

from .codex_install import _newline, _parse_config, _table_bounds

# A bare `command = ...` assignment line (the value runs to end-of-line;
# status-line commands are single-line strings in practice).
_COMMAND_ASSIGNMENT = re.compile(r"(?m)^[ \t]*command[ \t]*=")
# status_line written as a dotted key (status_line.command = ...) or an
# inline table (status_line = {...}) at top level. Both forms conflict with
# the [status_line] section this merge appends (TOML duplicate key), so
# they get a targeted refusal instead of a cryptic re-parse error.
_UNSUPPORTED_STATUS_LINE = re.compile(r"(?m)^[ \t]*['\"]?status_line['\"]?[ \t]*[=.]")


def _toml_basic_string(value):
    """Quote `value` as a TOML basic string. Backslash and double-quote are
    the only escapes this can need: repo paths are forward-slashed by
    install.py, and the emitted Windows command is the UNQUOTED
    `py -3 <path>` form (quoted forms are mangled by kimi-code's Node
    spawn -> cmd /d /s /c chain -- see platform_commands), so the escaping
    here is defensive against hand-supplied commands, not the common case."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _command_line(command, newline):
    return f"command = {_toml_basic_string(command)}{newline}"


def _status_line_table(parsed):
    """The [status_line] table from parsed TOML, or {} when absent or
    wrong-typed (a bare `status_line = "..."` string has no .get)."""
    table = parsed.get("status_line")
    return table if isinstance(table, dict) else {}


def _merge_unchecked(text, command):
    newline = _newline(text)
    line = _command_line(command, newline)
    section = _table_bounds(text, "status_line")
    if section is not None:
        content_start, content_end = section
        assignment = _COMMAND_ASSIGNMENT.search(text, content_start, content_end)
        if assignment is not None:
            # Replace the whole existing command line (preserving any
            # leading indent is deliberately NOT done: the canonical form
            # this writes is unindented, matching a fresh append).
            line_end = text.find("\n", assignment.start(), content_end)
            span_end = content_end if line_end < 0 else line_end + 1
            return text[: assignment.start()] + line + text[span_end:]
        separator = "" if text[content_start - 1] == "\n" else newline
        return text[:content_start] + separator + line + text[content_start:]

    if _UNSUPPORTED_STATUS_LINE.search(text):
        raise ValueError(
            "the existing status_line value uses an unsupported dotted or "
            "inline-table form; convert it to a [status_line] section first"
        )
    separator = "" if not text or text.endswith(("\n", "\r")) else newline
    blank = "" if not text.strip() else newline
    return text + separator + blank + f"[status_line]{newline}" + line


def merge_kimi_config(text, command):
    """Return tui.toml text with `[status_line] command = <command>` installed,
    preserving all unrelated content and comments. Raises ValueError on
    invalid input TOML, on an unsupported dotted/inline status_line form, or
    if the re-parsed merge did not retain the command (integrity guard)."""
    _parse_config(text)
    merged = _merge_unchecked(text, command)
    if _status_line_table(_parse_config(merged)).get("command") != command:
        raise ValueError("merged TOML did not retain the status_line command")
    return merged


def kimi_config_current(text, command):
    """Return whether tui.toml already runs `command` as the status line."""
    return _status_line_table(_parse_config(text)).get("command") == command
