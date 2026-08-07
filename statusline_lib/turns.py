"""Line-1 turn counter: how many turns the session has done.

Sourced from the transcript walk (statusline_lib.cost): the typed user-prompt
count is the primary signal (one typed prompt = one turn, matching the
beacon's turn definition), the deduped assistant-turn count is the fallback
for transcripts that carry no user entries at all (piped/headless sessions,
older harness shapes).

Leaf module: base RESET only.
"""

from .base import RESET

# Muted grey, same family as the session title: the count is a secondary
# label, not a headline.
_TURN_COUNT_COLOR = "\x1b[38;5;245m"


def _plural(count, singular):
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def format_turn_count(user_prompts, assistant_turns):
    """`N turns` from the typed-prompt count, `N steps` from the assistant-turn
    count when no user entries exist, "" when neither does (no walk data)."""
    prompts = int(user_prompts or 0)
    if prompts > 0:
        return f"{_TURN_COUNT_COLOR}{_plural(prompts, 'turn')}{RESET}"
    steps = int(assistant_turns or 0)
    if steps > 0:
        return f"{_TURN_COUNT_COLOR}{_plural(steps, 'step')}{RESET}"
    return ""
