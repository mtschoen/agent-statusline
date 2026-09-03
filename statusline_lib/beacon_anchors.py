"""The forward pass over a session transcript that finds its beacon anchors.

beacon.py renders the beacon column; this is the part of it that reads a whole
JSONL file, split out because those are two different costs. The render is
formatting over values already in hand and belongs on the resident server's
receive thread. A full transcript scan does not, so it is a refresh job on the
worker pool (transcript_summaries.refresh_beacon_anchors), and this module is
what that job calls.

The anchor state a scan produces is three fields folded from every beacon in
the file, in order:

  begin_ts   -- the most recent kind=begin, or None when a kind=end closed the
                lifecycle
  report_ts  -- the most recent kind=report inside that begin's lifecycle
  begin_eta  -- eta_seconds off that begin, when it carried a positive one

Imports:
  base -- _json_loads
"""

import re

from .base import _json_loads

_BEACON_BLOCK_RE = re.compile(
    r"<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>", re.DOTALL
)


def _iter_beacons_in_text(text):
    """Yield parsed beacon dicts embedded in one assistant text chunk."""
    if "<progress-beacon>" not in text:
        return
    for match in _BEACON_BLOCK_RE.finditer(text):
        try:
            beacon = _json_loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(beacon, dict):
            yield beacon


def _iter_assistant_beacons(entry):
    """Yield (timestamp, beacon_dict) for every progress-beacon in a JSONL
    assistant entry. No-op for non-assistant / malformed entries."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return
    ts = entry.get("timestamp")
    if not ts:
        return
    content = (entry.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return
    for chunk in content:
        if not isinstance(chunk, dict) or chunk.get("type") != "text":
            continue
        for beacon in _iter_beacons_in_text(chunk.get("text") or ""):
            yield ts, beacon


def _apply_beacon(beacon, ts, state):
    """Fold one beacon into the (begin_ts, report_ts, begin_eta) anchor state."""
    kind = beacon.get("kind")
    if kind == "begin":
        state["begin_ts"] = ts
        # New begin resets the step anchor -- any reports before this begin
        # belonged to a closed lifecycle.
        state["report_ts"] = None
        eta = beacon.get("eta_seconds")
        try:
            eta_val = float(eta) if eta is not None else 0.0
        except (TypeError, ValueError):
            eta_val = 0.0
        state["begin_eta"] = eta_val if eta_val > 0 else None
    elif kind == "report":
        # Only track reports within the current begin's lifecycle.
        if state["begin_ts"] is not None:
            state["report_ts"] = ts
    elif kind == "end":
        state["begin_ts"] = None
        state["report_ts"] = None
        state["begin_eta"] = None


def _scan_beacon_anchors(path):
    """One forward pass over the JSONL, folding every beacon into anchor state.

    Reads the file from byte zero, so it runs on the worker pool rather than on
    the receive thread. Raises OSError when the file cannot be opened; the
    caller decides what an unreadable transcript means.
    """
    state = {"begin_ts": None, "report_ts": None, "begin_eta": None}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                evt = _json_loads(line)
            except (ValueError, TypeError):
                continue
            for ts, beacon in _iter_assistant_beacons(evt):
                _apply_beacon(beacon, ts, state)
    return state
