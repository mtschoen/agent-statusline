#!/usr/bin/env bash
# UserPromptSubmit hook: inject a brief reminder of progress-beacon trigger
# criteria into the agent's context. Fires once per user prompt, before the
# agent starts any tool calls - the strongest moment to influence first-action
# behavior.
#
# Conditional: skip injection if the session already has a non-end beacon
# (skill is firing - no need to remind). Always inject if no beacon yet, or
# if the latest beacon is kind=end (turn boundary; next prompt may be a
# fresh non-trivial turn).
set -euo pipefail

input="$(cat)"
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty')"
if [[ -z "$session_id" || ! "$session_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo '{}'
    exit 0
fi

walker_out="$(agent-walker beacons-latest --session-id "$session_id" 2>/dev/null || true)"
beacon_kind=""
if [[ -n "$walker_out" ]]; then
    beacon_kind="$(printf '%s' "$walker_out" | jq -r '.beacon.kind // empty')"
fi

# If a live (non-end) beacon already exists, the agent is already pacing -
# don't add noise.
if [[ "$beacon_kind" == "begin" || "$beacon_kind" == "report" ]]; then
    echo '{}'
    exit 0
fi

reminder="progress-beacon reminder: if this turn is non-trivial (multi-file edits, multi-step research, dispatching subagents, or >2 minutes wall-clock), your FIRST substantive action MUST be emitting a <progress-beacon> {\"kind\": \"begin\", \"eta_seconds\": N, \"summary\": \"...\"} block in your assistant message text. Periodic 'report' beacons during work; 'end' beacon when finished. Trivial turns (one-line answers, single-file lookups, simple Q&A) skip the beacon entirely."

jq -n --arg msg "$reminder" '{
    hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: $msg
    }
}'
