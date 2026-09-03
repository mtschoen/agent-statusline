"""Verify that the client's terminal width reaches the render that needs it.

compact.py sheds line-2 fields only when the rendered width exceeds
os.environ["COLUMNS"], and the harness sets that variable on the process it
spawns. Under the resident-server model that process is the thin client, while
the render happens in a long-lived server that never saw it, so the width has
to travel in the request and be applied around the render that reads it. This
file pins both halves: the server applies and then restores it, and the real
client actually puts it on the wire.

The in-process checks drive Server.handle_request directly, against the
synthetic home and recording pool scripts/verify_server_requests.py installs.
The one end-to-end check runs the real client as a subprocess against a live
server whose handle_request records what arrived. Nothing here asserts on
elapsed time.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys

# The scripts directory, so the server suites next door are importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_server_protocol import (
    _CLIENT,
    _client_environment,
    _run_with_payload,
    _running_server,
)
from verify_server_requests import _claude_payload, _server

# Only now the repository root, and only now statusline_lib: importing the
# suites above is what installed the temporary HOME and CLAUDE_STATE_DIR, and
# several statusline_lib modules resolve app_dir()-based paths at import time.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.compact import visible_width
from statusline_lib.server_socket import parse_request

# This process must not carry a terminal width of its own: every check below
# that renders without a width in the request would otherwise pick one up from
# whatever shell ran the suite, and the shedding comparison would be moot.
os.environ.pop("COLUMNS", None)

# Wide enough that the fixture's full line 2 (49 columns) overflows it, and
# narrow enough that the shed line (23 columns) fits, so the gate is provably
# the thing being measured rather than a coincidence of either number.
_NARROW_COLUMNS = 30

# What the end-to-end check puts in the client subprocess's environment.
_CLIENT_COLUMNS = "20"

_ENCODING = "utf-8"

# Distinguishes "the request carried no columns key at all" from "it carried
# null", which are different wire shapes that must render identically.
_ABSENT = object()


def _request(columns=_ABSENT):
    """A claude request, carrying `columns` unless it is left absent."""
    request = {"kind": "claude", "payload": _claude_payload()}
    if columns is not _ABSENT:
        request["columns"] = columns
    return request


def _line2_width(columns=_ABSENT):
    """The visible width of line 2 of the render one request produces."""
    reply = _server().handle_request(_request(columns))
    return visible_width(reply.split("\n")[1])


def check_a_narrow_width_sheds_line2_fields(failures):
    """The whole point: a width in the request drives compact.py's auto-shrink
    exactly as $COLUMNS did when the render ran in the client's own process."""
    full = _line2_width()
    narrow = _line2_width(_NARROW_COLUMNS)
    if full <= _NARROW_COLUMNS:
        failures.append(
            f"full line 2 is {full} columns, not wider than the"
            f" {_NARROW_COLUMNS}-column request; the width gate cannot engage"
        )
    if narrow >= full:
        failures.append(
            f"a columns={_NARROW_COLUMNS} request rendered line 2 at {narrow}"
            f" columns, no narrower than the {full} columns it renders without"
        )
    if narrow > _NARROW_COLUMNS:
        failures.append(
            f"a columns={_NARROW_COLUMNS} request rendered line 2 at {narrow}"
            " columns, which still overflows the width it was given"
        )


def check_an_unusable_width_renders_full(failures):
    """Null, zero, negative and non-numeric all mean "no width to report", so
    each renders the full line rather than raising or shedding at random."""
    full = _line2_width()
    for value in (None, 0, -5, "wide", ""):
        width = _line2_width(value)
        if width != full:
            failures.append(
                f"columns={value!r} rendered line 2 at {width} columns,"
                f" expected the full {full} it renders with no width at all"
            )


def check_an_infinite_width_renders_full(failures):
    """A datagram carrying `"columns": 1e400` parses as float("inf"), and
    int() on that raises OverflowError, which is neither the TypeError nor
    the ValueError every other unusable width raises. Uncaught it costs that
    client its whole render, so it has to collapse to "no width" like the
    rest. Sent through parse_request, since only the wire produces inf."""
    wire = {"kind": "claude", "payload": _claude_payload(), "columns": 1e400}
    request = parse_request(json.dumps(wire).encode(_ENCODING))
    reply = _server().handle_request(request)
    if reply is None:
        failures.append("an infinite width lost the render entirely")
    elif visible_width(reply.split("\n")[1]) != _line2_width():
        failures.append("an infinite width must render the full line 2")


def check_the_environment_is_restored_when_it_was_unset(failures):
    """A render must leave no COLUMNS behind in the server process: the next
    request's width is the next client's, and a leftover one is a wrong
    render for whichever client sends no width at all."""
    _server().handle_request(_request(_NARROW_COLUMNS))
    if "COLUMNS" in os.environ:
        failures.append(
            "COLUMNS was unset before the request and is"
            f" {os.environ['COLUMNS']!r} after it"
        )


def check_the_environment_is_restored_when_it_was_set(failures):
    """The mirror case: a COLUMNS the server process legitimately carries must
    survive a request that overrides it, and a request that carries none."""
    previous = "500"
    os.environ["COLUMNS"] = previous
    try:
        for columns in (_NARROW_COLUMNS, _ABSENT, None):
            _server().handle_request(_request(columns))
            if os.environ.get("COLUMNS") != previous:
                failures.append(
                    f"a columns={columns!r} request left COLUMNS as"
                    f" {os.environ.get('COLUMNS')!r}, expected {previous!r}"
                )
    finally:
        os.environ.pop("COLUMNS", None)


def check_a_request_without_the_key_still_renders(failures):
    """The key is optional on the wire, so a client that predates it (or the
    status and shutdown kinds, which never send one) still gets an answer."""
    reply = _server().handle_request(_request())
    if not reply or not reply.strip():
        failures.append(f"a request with no columns key rendered {reply!r}")


def check_parse_request_accepts_the_new_key(failures):
    """parse_request accepts a request with or without the columns key, and
    drops neither: the key survives the wire round trip intact."""
    for columns in (_NARROW_COLUMNS, None):
        wire = {"kind": "claude", "payload": {}, "version": "v1", "columns": columns}
        parsed = parse_request(json.dumps(wire).encode(_ENCODING))
        if parsed != wire:
            failures.append(f"parse_request({wire}) returned {parsed!r}")
    legacy = {"kind": "claude", "payload": {}, "version": "v1"}
    if parse_request(json.dumps(legacy).encode(_ENCODING)) != legacy:
        failures.append("parse_request stopped accepting a request with no columns")


def _recorded_client_columns(failures, environment):
    """The `columns` value the real client put on the wire when run under
    `environment`, or the string describing why nothing was recorded."""
    recorded = []
    with _running_server(failures, reply="fixed") as context:
        original = context.server.handle_request

        def handle_request(request):
            recorded.append(request)
            return original(request)

        context.server.handle_request = handle_request
        result = _run_with_payload(
            [sys.executable, _CLIENT, "--kind", "claude"],
            _claude_payload(),
            environment,
        )
    if result.returncode != 0:
        return f"client exited {result.returncode}: {result.stderr!r}"
    if not recorded:
        return f"the server recorded no request; client said {result.stdout!r}"
    return recorded[0].get("columns")


def check_the_client_sends_its_terminal_width(failures):
    """The end-to-end half: the real client subprocess reads COLUMNS from its
    own environment, the one place the harness sets it, and forwards it."""
    columns = _recorded_client_columns(
        failures, _client_environment(COLUMNS=_CLIENT_COLUMNS)
    )
    if columns != int(_CLIENT_COLUMNS):
        failures.append(
            f"a client run with COLUMNS={_CLIENT_COLUMNS} sent columns={columns!r}"
        )


def check_the_client_sends_none_without_a_usable_width(failures):
    """No COLUMNS, or one the shell set to something unusable, is null on the
    wire rather than a guess, so the server renders full instead of narrow."""
    for value in (None, "not-a-number", "0"):
        columns = _recorded_client_columns(failures, _client_environment(COLUMNS=value))
        if columns is not None:
            failures.append(
                f"a client run with COLUMNS={value!r} sent columns={columns!r},"
                " expected None"
            )


def main():
    failures = []
    for check in (
        check_a_narrow_width_sheds_line2_fields,
        check_an_unusable_width_renders_full,
        check_an_infinite_width_renders_full,
        check_the_environment_is_restored_when_it_was_unset,
        check_the_environment_is_restored_when_it_was_set,
        check_a_request_without_the_key_still_renders,
        check_parse_request_accepts_the_new_key,
        check_the_client_sends_its_terminal_width,
        check_the_client_sends_none_without_a_usable_width,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: the client's terminal width reaches the render and is restored")


if __name__ == "__main__":
    main()
