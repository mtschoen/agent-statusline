#!/usr/bin/env bash
# PostToolUse hook: nudge the agent if no beacon has been emitted recently,
# OR if no beacon has been emitted at all in a session that's been alive
# past a grace period.
#
# Two accuracy constraints discovered in real sessions (2026-06-10 debugging,
# see skills-progress-beacon README "Nudge paths and rate limiting"):
#   1. Claude Code persists mid-turn assistant messages to the session JSONL
#      with multi-minute lag (sometimes never, for text-only mid-turn
#      messages). A beacon the agent just emitted can be invisible to
#      agent-walker for minutes. Thresholds must ride that out.
#   2. PostToolUse fires once per tool call, including per call in a parallel
#      batch. Without rate limiting one stale window produces a nudge burst
#      (observed: 10+ identical nudges in recorded incident sessions).
# Hence: a per-session cooldown gates BOTH nudge paths, and the staleness /
# grace thresholds are set well above the observed flush lag.
set -euo pipefail

# Seconds between nudges for one session (both paths share the stamp).
NUDGE_COOLDOWN=300
# Stale path: a live (non-end) beacon older than this triggers a nudge.
# Keep comfortably above typical JSONL flush lag so just-emitted beacons
# aren't nagged about.
STALE_AFTER=600
# Missing path: no beacon at all and the session is older than this.
GRACE=300

input="$(cat)"
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty')"
if [[ -z "$session_id" || ! "$session_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo '{}'
    exit 0
fi

# Cooldown check up front: if we nudged this session recently (either path),
# stay silent regardless of beacon state. A per-session directory lock ensures
# parallel tool-call hook invocations check and update the cooldown atomically.
stamp_dir="${TMPDIR:-/tmp}/progress-beacon-nudge"
if ! mkdir -p "$stamp_dir" 2>/dev/null || ! [[ -w "$stamp_dir" ]]; then
    stamp_dir="/tmp/progress-beacon-nudge"
    if ! mkdir -p "$stamp_dir" 2>/dev/null || ! [[ -w "$stamp_dir" ]]; then
        echo '{}'
        exit 0
    fi
fi
stamp="$stamp_dir/$session_id"
lock_file="$stamp_dir/$session_id.lock"
dlock="$stamp_dir/$session_id.dlock"

get_mtime() {
    local target="$1"
    local t=""
    if t="$(stat -c %Y "$target" 2>/dev/null)" && [[ "$t" =~ ^[0-9]+$ ]]; then
        echo "$t"
        return 0
    fi
    if t="$(stat -f %m "$target" 2>/dev/null)" && [[ "$t" =~ ^[0-9]+$ ]]; then
        echo "$t"
        return 0
    fi
    if t="$(date -r "$target" +%s 2>/dev/null)" && [[ "$t" =~ ^[0-9]+$ ]]; then
        echo "$t"
        return 0
    fi
    echo 0
}

acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        if ! exec 9>"$lock_file" 2>/dev/null; then
            return 1
        fi
        flock -x -w 5 9
    else
        local retries=100
        local empty_retries=0
        while ! mkdir "$dlock" 2>/dev/null; do
            retries=$((retries - 1))
            if (( retries <= 0 )); then
                return 1
            fi

            local is_stale=0
            local now
            now="$(date +%s)"

            if [[ -f "$dlock/owner" ]]; then
                local lock_pid="" lock_ts=""
                read -r lock_pid lock_ts < "$dlock/owner" 2>/dev/null || true
                if [[ "$lock_pid" =~ ^[0-9]+$ && "$lock_ts" =~ ^[0-9]+$ ]]; then
                    if ! kill -0 "$lock_pid" 2>/dev/null; then
                        is_stale=1
                    fi
                else
                    empty_retries=$((empty_retries + 1))
                    local dir_ts
                    dir_ts="$(get_mtime "$dlock")"
                    if (( empty_retries >= 20 || (dir_ts > 0 && now - dir_ts >= 2) )); then
                        is_stale=1
                    fi
                fi
            else
                empty_retries=$((empty_retries + 1))
                local dir_ts
                dir_ts="$(get_mtime "$dlock")"
                if (( empty_retries >= 20 || (dir_ts > 0 && now - dir_ts >= 2) )); then
                    is_stale=1
                fi
            fi

            if (( is_stale == 1 )); then
                local stale_target="$dlock.stale.$$"
                if mv "$dlock" "$stale_target" 2>/dev/null; then
                    rm -rf "$stale_target" 2>/dev/null || true
                    continue
                fi
            fi

            sleep 0.05
        done

        echo "$$ $(date +%s)" > "$dlock/owner" 2>/dev/null || true
        return 0
    fi
}

release_lock() {
    if command -v flock >/dev/null 2>&1; then
        flock -u 9 2>/dev/null || true
        exec 9>&- 2>/dev/null || true
    else
        rm -rf "$dlock" 2>/dev/null || true
    fi
}

if ! acquire_lock; then
    echo '{}'
    exit 0
fi
trap release_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

now_epoch="$(date +%s)"
if [[ -f "$stamp" ]]; then
    last_nudge="$(cat "$stamp" 2>/dev/null || echo 0)"
    if [[ "$last_nudge" =~ ^[0-9]+$ ]] && (( now_epoch - last_nudge < NUDGE_COOLDOWN )); then
        echo '{}'
        exit 0
    fi
fi

walker_out="$(agent-walker beacons-latest --session-id "$session_id" 2>/dev/null || true)"
if [[ -z "$walker_out" ]]; then
    echo '{}'
    exit 0
fi

beacon="$(printf '%s' "$walker_out" | jq -c '.beacon // null')"
age="$(printf '%s' "$walker_out" | jq -r '.age_seconds // empty')"

needs_nudge=0
nudge_msg=""

if [[ "$beacon" != "null" && -n "$age" && "$age" != "null" ]]; then
    # Stale-beacon path: a beacon exists but hasn't been refreshed.
    kind="$(printf '%s' "$beacon" | jq -r '.kind')"
    # Only nudge live beacons; an end-beacon means the agent has already
    # closed out and a stale-end is just inactivity, not under-trigger.
    if [[ "$kind" != "end" ]]; then
        if awk -v a="$age" -v t="$STALE_AFTER" 'BEGIN{exit !(a > t)}'; then
            minutes=$(awk -v a="$age" 'BEGIN{printf "%d", a/60}')
            needs_nudge=1
            nudge_msg="No progress beacon visible in ${minutes}+ minutes (note: beacons can take a few minutes to land in the transcript). If this turn is non-trivial and you haven't emitted one recently, emit a <progress-beacon> 'report' now."
        fi
    fi
else
    # Missing-beacon path: walker reports no beacon at all. Nudge if the
    # session has been alive past the grace period so trivial one-shot
    # turns don't get spammed.
    jsonl=""
    for f in "$HOME"/.claude/projects/*/"$session_id".jsonl; do
        if [[ -f "$f" ]]; then
            jsonl="$f"
            break
        fi
    done
    if [[ -n "$jsonl" ]]; then
        first_ts="$(jq -r 'select(.timestamp) | .timestamp' "$jsonl" 2>/dev/null | head -1 || true)"
        if [[ -n "$first_ts" ]]; then
            first_epoch="$(date -d "$first_ts" +%s 2>/dev/null || echo 0)"
            session_age=$((now_epoch - first_epoch))
            if [[ "$first_epoch" -gt 0 && "$session_age" -gt "$GRACE" ]]; then
                minutes=$((session_age / 60))
                needs_nudge=1
                nudge_msg="This session is ${minutes}+ minutes old with no progress-beacon visible yet. If the current turn is non-trivial (multi-file edits, multi-step research, dispatching subagents, or >2 minutes wall-clock) and you haven't emitted a beacon, include a <progress-beacon> 'begin' block in your next assistant message. If the turn is trivial (one-off Q&A, single-file lookup), ignore this."
            fi
        fi
    fi
fi

if [[ "$needs_nudge" -eq 1 ]]; then
    mkdir -p "$stamp_dir" 2>/dev/null || true
    printf '%s' "$now_epoch" > "$stamp" 2>/dev/null || true
    jq -n --arg msg "$nudge_msg" '{
        hookSpecificOutput: {
            hookEventName: "PostToolUse",
            additionalContext: $msg
        }
    }'
else
    echo '{}'
fi
