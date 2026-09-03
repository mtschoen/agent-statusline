"""subagentStatusLine entry point: the thin client wrapper.

Reads Claude Code's per-tick payload from stdin (base hook fields +
`columns` + `tasks[]`) and forwards it to the resident server through
statusline_client.main("subagent"), which prints one NDJSON line per
visible row. The subagent kind is the one kind with no fallback line
(`statusline_client_support.KINDS_WITHOUT_A_FALLBACK_LINE`): the panel takes
JSON rows, not a statusline, so a server that cannot answer in time leaves
the panel showing whatever it last had rather than a line that does not
belong in it.

The row-building logic lives in statusline_lib/render_subagent.py
(`render_subagent_rows`), reached through statusline_lib/server.py so the
resident server can render without ever importing this file. Do not rename
this file -- every deployed machine's settings embed the literal path.
"""

import sys

import statusline_client

if __name__ == "__main__":
    sys.exit(statusline_client.main("subagent"))
