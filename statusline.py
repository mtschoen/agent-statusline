"""Main statusline entry point: the thin client wrapper.

Reads Claude Code's JSON payload from stdin and forwards it to the resident
server through statusline_client.main("claude"), which prints whatever the
server replies with (or a fallback line when the server cannot answer in
time). Antigravity CLI reuses this same entry point and render kind, since
its payload is close enough to Claude Code's for the shared adapter in
statusline_lib/render_claude.py.

All rendering logic lives in statusline_lib (statusline_lib/render_claude.py,
reached through statusline_lib/server.py) so the resident server can render
without ever importing this file. Do not rename this file -- every deployed
machine's settings embed the literal path.

See README.md for layout, color thresholds, and install instructions.
"""

import sys

import statusline_client

# Force UTF-8 stdout regardless of the Windows console code page. Without
# this, characters like `⏱` (U+23F1, used in the beacon column) crash with
# UnicodeEncodeError on cp1252 stdout. errors="replace" is belt-and-braces
# so a future non-encodable glyph degrades to "?" instead of crashing the
# whole statusline. statusline_client already reconfigures stdout the same
# way on import; this is kept here too, matching the deployed contract.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if __name__ == "__main__":
    sys.exit(statusline_client.main("claude"))
