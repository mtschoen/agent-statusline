"""The transcript-walk accumulator seam, split out of cost.py to stay under
the aislop 400-line file gate. cost.walk_transcript composes these three
steps (new_walk_accumulator, fold_transcript_lines, summarize_walk) so the
resident server can later fold in only the transcript bytes appended since
the last render, instead of rewalking the whole file.

Imports:
  base -- _json_loads (the walk's only module-level dependency)
  cost -- _accumulate_assistant_turn, _accumulate_user_prompt: imported
          inside fold_transcript_lines rather than at module level, because
          cost.py imports the names defined here. A top-level import would
          cycle; the deferred import runs only when a fold is first
          performed, well after both modules have finished loading.
"""

from .base import _json_loads


def new_walk_accumulator():
    """The accumulator dict a transcript walk folds lines into.

    `track_evictions` and `track_user_prompts` default off: those are
    parent-only concerns and the caller opts in explicitly.
    """
    return {
        "read": 0,
        "write": 0,
        "input": 0,
        "output": 0,
        "cost": 0.0,
        "read_cost": 0.0,
        "write_cost": 0.0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "ttl_evictions": 0,
        "ttl_wasted": 0.0,
        "assistant_turns": 0,
        "user_prompts": 0,
        "track_evictions": False,
        "track_user_prompts": False,
        "last_model": "",
        "last_input": 0,
        "last_cache_create": 0,
        "last_cache_read": 0,
        "last_turn_ts": None,
        "last_turn_ttl_seconds": None,
    }


def fold_transcript_lines(lines, accumulator, seen_ids):
    """Fold each raw JSONL line in `lines` into `accumulator`. A line that
    does not parse is skipped, not fatal: a torn write at the tail of a live
    transcript is normal, not an error."""
    from .cost import _accumulate_assistant_turn, _accumulate_user_prompt

    for line in lines:
        try:
            entry = _json_loads(line)
        except Exception:
            continue
        _accumulate_assistant_turn(entry, accumulator, seen_ids)
        _accumulate_user_prompt(entry, accumulator)


def _walk_one_transcript(path, acc, seen_ids):
    """Stream one JSONL transcript, folding each line into `acc`."""
    try:
        with open(path, encoding="utf-8") as f:
            fold_transcript_lines(f, acc, seen_ids)
    except OSError:
        # Transcript became unreadable mid-walk; use the totals gathered so far
        # rather than failing the whole render.
        pass


def summarize_walk(acc, parent_cost):
    """The dict a transcript walk returns. See cost.walk_transcript for the
    field-by-field description.
    """
    return {
        "read": acc["read"],
        "write": acc["write"],
        "input": acc["input"],
        "output": acc["output"],
        "cost": acc["cost"],
        "read_cost": acc["read_cost"],
        "write_cost": acc["write_cost"],
        "input_cost": acc["input_cost"],
        "output_cost": acc["output_cost"],
        "ttl_evictions": acc["ttl_evictions"],
        "ttl_wasted": acc["ttl_wasted"],
        "parent_cost": parent_cost,
        "subagent_cost": acc["cost"] - parent_cost,
        "last_model_id": acc["last_model"],
        "last_input": acc["last_input"],
        "last_cache_create": acc["last_cache_create"],
        "last_cache_read": acc["last_cache_read"],
        "user_prompts": acc["user_prompts"],
        "assistant_turns": acc["assistant_turns"],
    }
