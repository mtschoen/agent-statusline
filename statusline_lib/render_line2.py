"""Line 2 formatter: `model | ctx | cache | ttl | quota | fable | day | burn |
cost | +/-lines`, with fields dropped by the compact-mode flag resolver and
every dollar figure suppressed by STATUSLINE_HIDE_COST. Extracted from
statusline.py so the resident server can render line 2 without importing the
entry script.
"""

from typing import NamedTuple

from .agy import format_agy_cache, format_agy_quota
from .badge import format_context
from .burnrate import format_burn_rate
from .costfmt import format_cache, format_ttl
from .fable_quota import format_fable_quota
from .pace import format_quota
from .prefs import pref_bool


def hide_cost():
    """STATUSLINE_HIDE_COST truthy -> suppress every dollar figure on line 2.

    Accepts 1/true/on/yes (any case). Anything else, including unset, shows
    money as before. A deliberate calm switch: quota %/time-to-limit (the
    non-dollar runway signal) stays, so you keep the useful budgeting info
    without a session-cost figure attached to a run you might have to discard.
    """
    return pref_bool("STATUSLINE_HIDE_COST", default=False)


class Line2Inputs(NamedTuple):
    """Pre-computed inputs to line 2's compact re-render. Context is carried raw
    (not pre-rendered) so the compact resolver can drop its denominator and
    percentage; the cheap format_context call re-runs per flag set."""

    model_summary: str
    context_used: int
    window_size: int
    model_id: str
    walk: dict
    rate_limits: dict | None
    day_budget_summary: str
    cost_summary: str
    # STATUSLINE_HIDE_COST: when True, every dollar-denominated figure is
    # suppressed (session cost, $/min burn + target, day budget, the cache $
    # parens, the TTL wasted-$ estimate). Token counts, hit%, the TTL eviction
    # COUNT, context, and quota %/time-to-limit all stay - none of those carry a $.
    hide_cost: bool
    # Pre-rendered `+A/-B` session diffstat. Not money, so it is NOT gated by
    # hide_cost - only by its own `lines` compact-drop flag.
    lines_summary: str
    # Antigravity CLI's `quota` payload block -- the fallback quota source
    # when there is no `rate_limits` (agy has no such field at all). None on
    # every other harness. Defaulted (trailing fields) so existing
    # keyword-arg callers that predate agy support don't need updating.
    agy_quota: dict | None = None
    # Antigravity CLI's `context_window.current_usage` -- the per-turn cache
    # fallback used when the transcript walk found no cache activity (agy's
    # brain transcripts carry no usage data at all, so this is always the
    # case there).
    current_usage: dict | None = None
    # True only when the payload self-identifies as Antigravity CLI
    # (`product == "antigravity"`). Gates the per-turn cache fallback below --
    # see render_line2 for why this must be an explicit identity check, not
    # "the transcript walk found nothing".
    is_agy: bool = False


def render_line2(flags, inputs):
    """Format line 2 at the verbosity given by `flags` (the compact resolver
    flips entries off to fit $COLUMNS). `inputs` carries the already-computed,
    flag-independent summaries plus the raw walk/rate_limits; only the cheap
    formatting re-runs per flag set."""
    walk = inputs.walk
    # The money master switch ANDs into every dollar-bearing flag below, so it
    # overrides regardless of width: hidden money never reappears just because
    # the terminal is wide enough to show it.
    money = not inputs.hide_cost
    context_summary = format_context(
        inputs.context_used,
        inputs.window_size,
        inputs.model_id,
        show_denom=flags["context_denom"],
        show_pct=flags["context_pct"],
    )
    cache_summary = format_cache(
        walk["read"],
        walk["write"],
        walk["input"],
        walk["read_cost"],
        walk["write_cost"],
        show_costs=flags["cache_costs"] and money,
        show_hit=flags["cache_hit"],
        output_t=walk["output"],
        input_cost=walk["input_cost"],
        output_cost=walk["output_cost"],
        show_input=flags["cache_input"] and money,
        show_output=flags["cache_output"] and money,
    )
    if not cache_summary and inputs.is_agy:
        # The per-turn fallback is gated on an explicit identity check
        # (`product == "antigravity"`, threaded in as inputs.is_agy), NOT on
        # "the transcript walk found nothing". An earlier version used the
        # latter and was a truthfulness bug: a Claude Code (or any other)
        # payload whose walk fails for an unrelated reason (missing/renamed
        # transcript, a start-of-session race, an OSError) would silently
        # render this turn-only snapshot through the exact same
        # read/write/hit% layout and colors the session-cumulative field
        # uses -- indistinguishable in form from the real thing, worse than
        # the pre-fallback "" (an honest "no data" signal). Scoping to agy
        # payloads specifically means a broken walk on any other harness goes
        # back to rendering nothing, which is the honest degrade. `product`
        # was chosen over "the quota block is present" as the gate signal
        # because it's agy's explicit self-identification, not a payload-shape
        # proxy that could coincidentally match some other harness's fields.
        # format_agy_cache also prefixes its own muted "turn" marker as a
        # second, independent safeguard -- even here, the two meanings can
        # never be visually confused.
        cache_summary = format_agy_cache(
            inputs.current_usage, show_hit=flags["cache_hit"]
        )
    ttl_summary = format_ttl(
        walk["ttl_evictions"],
        walk["ttl_wasted"],
        show_wasted=flags["ttl_wasted"] and money,
    )
    quota_summary = (
        format_quota(inputs.rate_limits, show_pace=flags["quota_pace"])
        if inputs.rate_limits
        else format_agy_quota(inputs.agy_quota, show_pace=flags["quota_pace"])
    )
    # Subscription-scoped pool: gate it on the session actually having
    # subscription rate limits, exactly as quota_summary does above. Enterprise
    # and API-billed sessions get no `rate_limits` from Claude Code, so the
    # dashboard's `fable` pool describes a different account than the one this
    # session bills to -- it renders a permanent `fable: 0%` that is not merely
    # uninformative but wrong.
    fable_summary = (
        format_fable_quota(inputs.rate_limits, show_pace=flags["quota_pace"])
        if inputs.rate_limits
        else ""
    )
    burnrate_summary = (
        format_burn_rate(inputs.rate_limits, show_target=flags["burn_target"])
        if flags["burn_rate"] and money
        else ""
    )
    parts = [
        s
        for s in (
            inputs.model_summary,
            context_summary,
            cache_summary,
            ttl_summary,
            quota_summary,
            fable_summary,
            inputs.day_budget_summary if money else "",
            burnrate_summary,
            inputs.cost_summary if money else "",
            inputs.lines_summary if flags["lines"] else "",
        )
        if s
    ]
    return " | ".join(parts)
