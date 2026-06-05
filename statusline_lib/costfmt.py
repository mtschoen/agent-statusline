"""Cost/cache segment rendering for line 2.

Split out of cost.py (which keeps the transcript-walking + accumulation) to
stay under the aislop 400-line file gate. cost.py re-exports every public name
here, so `statusline_lib.cost.format_cache` (and the rest) still resolve - this
module is the implementation home, cost.py the back-compat surface.

Imports: base only (color constants, fmt, color_high_good) -- a leaf module, no
dependency back on cost.py's walk.
"""

from .base import (
    CACHE_READ,
    CACHE_WRITE,
    GREEN,
    INPUT_TOK,
    ORANGE,
    OUTPUT_TOK,
    RED,
    RESET,
    YELLOW,
    color_high_good,
    fmt,
)

# U+FE0E text-presentation selector keeps the warning glyph monochrome so the
# ANSI red wins cross-platform (Windows Terminal would otherwise color-font it),
# matching the on-target yin-yang's treatment.
TTL_WARN_GLYPH = "⚠︎"


def format_cache(
    read,
    write,
    input_t,
    read_cost=None,
    write_cost=None,
    show_costs=True,
    show_hit=True,
    output_t=0,
    input_cost=None,
    output_cost=None,
    show_input=False,
    show_output=False,
):
    """`[input (cost) /] reads (cost) / writes (cost) [/ output (cost)] / hit %`.

    The cache pair (read/write tokens + hit%) is the essential core and never
    sheds its token counts. Around it sit the two optional full-breakdown
    figures - fresh `input` (steel blue) and `output` (violet) - each rendered
    `tokens (cost)` so all four $ components are visible and sum to the token
    cost. They are off by default and gated on their own cost arg, so any caller
    that omits them (subagent render, compact mode) is byte-identical to before.
    The full-breakdown figures drop FIRST under width pressure (see
    compact.DROP_ORDER), leaving read/write/hit% as the last cache fields to go.

    The read/write per-figure $ parens render only when both cost args are
    supplied AND `show_costs`; the $ reuses each figure's identity color (teal
    read, orange write). `show_hit=False` drops the trailing hit%.
    """
    total_in = read + write + input_t
    if total_in <= 0:
        return ""
    with_costs = show_costs and read_cost is not None and write_cost is not None
    read_part = f"{CACHE_READ}{fmt(read)}{RESET}"
    write_part = f"{CACHE_WRITE}{fmt(write)}{RESET}"
    if with_costs:
        read_part += f" {CACHE_READ}(${read_cost:.2f}){RESET}"
        write_part += f" {CACHE_WRITE}(${write_cost:.2f}){RESET}"
    parts = []
    if show_input and input_cost is not None:
        parts.append(f"{INPUT_TOK}{fmt(input_t)} (${input_cost:.2f}){RESET}")
    parts.append(read_part)
    parts.append(write_part)
    if show_output and output_cost is not None:
        parts.append(f"{OUTPUT_TOK}{fmt(output_t)} (${output_cost:.2f}){RESET}")
    segment = " / ".join(parts)
    if show_hit:
        hit_pct = read * 100.0 / total_in
        segment += f" / {color_high_good(hit_pct, 90, 75)} hit"
    return segment


def format_ttl(evictions, wasted, show_wasted=True):
    """Loud red cache-eviction counter, or "" when there were no evictions.

    `⚠ TTL:N` plus ` (~$X.XX)` of estimated wasted spend when `show_wasted`.
    Whole segment is red - it is a problem signal, not a routine metric.
    """
    if not evictions or evictions <= 0:
        return ""
    segment = f"{RED}{TTL_WARN_GLYPH} TTL:{evictions}{RESET}"
    if show_wasted and wasted and wasted > 0:
        segment += f" {RED}(~${wasted:.2f}){RESET}"
    return segment


def _cost_threshold_color(cost):
    """Magnitude band shared by the parent figure and the subagent addend:
    green < $25, yellow < $50, red >= $50."""
    return RED if cost >= 50 else YELLOW if cost >= 25 else GREEN


_SUM_COST_THRESHOLD_YELLOW = 35  # combined parent+subagent total >= this -> yellow
_SUM_COST_THRESHOLD_RED = 70  # combined parent+subagent total >= this -> red


def _sum_threshold_color(cost):
    """Magnitude band for the combined parent+subagent total (the `= $total`
    segment). Breakpoints sit higher than the per-figure bands
    (_cost_threshold_color, 25/50): a total runs bigger than its parts, so
    reusing 25/50 would peg the sum red on routine sessions. 35/70 splits the
    difference -- it flags a combined burn that two individually-modest figures
    can hide, without crying wolf."""
    return (
        RED
        if cost >= _SUM_COST_THRESHOLD_RED
        else YELLOW
        if cost >= _SUM_COST_THRESHOLD_YELLOW
        else GREEN
    )


def format_cost(cost):
    if cost is None or cost <= 0:
        return ""
    return f"{_cost_threshold_color(cost)}${cost:.2f}{RESET}"


# The subagent addend carries the same magnitude bands as the parent figure
# (green/yellow/red via _cost_threshold_color). Its trailing "~" is the estimate
# marker, and its COLOR is the drift signal -- grey when our formula tracks the
# harness, else tinted by the DIRECTION and SEVERITY of the divergence:
#   under-estimate (our_parent < authoritative -- shown cost low, you may pay MORE):
#     * moderate (> 4%):  caution-orange
#     * way off  (> 25%): deep red -- a structural miss (a rate hike or a new
#       billed dimension we don't model yet); you may be paying
#       WAY more than shown.
#   over-estimate (our_parent > authoritative -- shown cost high, you pay LESS):
#     * moderate (> 4%):  cyan -- reassuring
#     * way off  (> 25%): bright pink -- our estimate is wildly high (e.g. a rate
#       CUT we haven't caught -- Anthropic dropped Opus 3x at 4.5); you're paying
#       WAY less than shown.
# Thresholds are tight because our formula matches the harness's parent
# total_cost_usd to the penny in practice, so even a few percent is a real signal.
_SUBAGENT_COST_COLOR = "\x1b[38;5;245m"  # grey -- "~" tracks the harness
_COST_DRIFT_UNDER_COLOR = ORANGE  # under, moderate: you may pay MORE
_COST_DRIFT_UNDER_MAJOR_COLOR = "\x1b[38;5;124m"  # under, way off: deep red
_COST_DRIFT_OVER_COLOR = "\x1b[38;5;51m"  # over, moderate: cyan, you pay LESS
_COST_DRIFT_OVER_MAJOR_COLOR = "\x1b[38;5;198m"  # over, way off: bright pink
_COST_DRIFT_THRESHOLD = 0.04  # flag at all
_COST_DRIFT_MAJOR_THRESHOLD = 0.25  # "way off"


def format_cost_with_subagents(authoritative_parent, our_parent, subagent_cost):
    """Render `($parent + $sub~) = $total`: authoritative parent + estimated subagent
    spend, then their sum.

    The parent and subagent figures carry the same per-figure magnitude bands
    (green/yellow/red via _cost_threshold_color). The trailing `= $total` is their
    sum and carries its OWN, higher bands (_sum_threshold_color: green < $35,
    yellow < $70, red >= $70) -- two individually-modest figures can add up to a
    combined burn worth flagging that neither part shows alone. The parent is
    the harness's authoritative `total_cost_usd` (ground truth, but PARENT-ONLY
    -- subagents are invisible to it). The subagent figure is our formula's
    estimate, marked with a trailing "~".

    The "~" closes the loop on drift. Drift is a PARENT-side measurement:
    `our_parent` is our formula over the same parent turns as the authoritative
    figure, so a gap means our cost formula has diverged from the harness. We
    tint the estimate marker by the DIRECTION and SEVERITY of that gap: grey when
    it tracks the harness; over-estimate (shown high, you pay less) is cyan,
    escalating to bright pink past 25%; under-estimate (shown low, you may pay
    more) is caution-orange, escalating to deep red past 25%. The same formula
    produces the subagent figure, so the direction carries over; it is NOT a
    per-subagent measurement (no ground truth exists for subagents).

    With no subagent cost the result is just the authoritative figure -- byte
    identical to the pre-subagent behavior.
    """
    parent_part = format_cost(authoritative_parent)
    if not subagent_cost or subagent_cost <= 0:
        return parent_part
    drift = 0.0
    if authoritative_parent and authoritative_parent > 0:
        drift = (our_parent - authoritative_parent) / authoritative_parent
    magnitude = abs(drift)
    major = magnitude > _COST_DRIFT_MAJOR_THRESHOLD
    if magnitude <= _COST_DRIFT_THRESHOLD:
        tilde_color = _SUBAGENT_COST_COLOR  # tracks the harness
    elif drift > 0:  # over-estimate -> you pay LESS
        tilde_color = _COST_DRIFT_OVER_MAJOR_COLOR if major else _COST_DRIFT_OVER_COLOR
    else:  # under-estimate -> you may pay MORE
        tilde_color = (
            _COST_DRIFT_UNDER_MAJOR_COLOR if major else _COST_DRIFT_UNDER_COLOR
        )
    addend = (
        f"{_cost_threshold_color(subagent_cost)}+ ${subagent_cost:.2f}{RESET}"
        f"{tilde_color}~{RESET}"
    )
    body = f"{parent_part} {addend}" if parent_part else addend
    # The sum is meaningful only when there's a parent figure to add to. With no
    # authoritative parent (rare -- payload hasn't delivered a cost yet), `= $total`
    # would just echo the subagent addend, so drop it but still surface the spend.
    if not parent_part:
        return body
    total = authoritative_parent + subagent_cost
    return f"({body}) {_sum_threshold_color(total)}= ${total:.2f}{RESET}"
