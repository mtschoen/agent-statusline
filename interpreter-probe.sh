#!/usr/bin/env bash
# Shared interpreter probe, sourced (not executed) by the statusline wrapper
# scripts that live alongside it. Sets $PY as a side effect; callers do
# `exec $PY "$DIR/some_script.py" "$@"`.
#
# Interpreter choice matters a LOT here: on Windows, bare `python`/`python3`
# resolve to the Microsoft Store app-execution-alias shim, whose ~750ms
# per-invocation launch overhead dominated the whole render (statusline went
# from ~1000ms -> ~280ms just by switching off it). The `py` launcher points at
# the python.org build (~50ms startup) and is where orjson/psutil are installed.
# On Linux `py` doesn't exist, so we fall back to python3/python (already fast).
#
# To avoid paying the execution probe startup cost (~15-50ms) on every render,
# the resolved interpreter is cached under the app directory with a 1-hour TTL,
# keyed on candidate paths from `command -v`.
# shellcheck disable=SC2034  # PY is consumed by the script that sources this file

_PY_PATH="$(command -v py 2>/dev/null || true)"
_PY3_PATH="$(command -v python3 2>/dev/null || true)"
_PYTHON_PATH="$(command -v python 2>/dev/null || true)"
_FP="${_PY_PATH}|${_PY3_PATH}|${_PYTHON_PATH}"

_PLATFORM="${STATUSLINE_PLATFORM:-}"
if [ -z "$_PLATFORM" ]; then
  _NEXT_IS_PLATFORM=0
  for _arg in "$@"; do
    if [ "$_NEXT_IS_PLATFORM" = "1" ]; then
      _PLATFORM="$_arg"
      break
    elif [ "$_arg" = "--statusline-platform" ]; then
      _NEXT_IS_PLATFORM=1
    else
      case "$_arg" in
        --statusline-platform=*)
          _PLATFORM="${_arg#*=}"
          break
          ;;
      esac
    fi
  done
fi

_HOME="${HOME:-}"
if [ "$_PLATFORM" = "antigravity" ]; then
  _APP_DIR="$_HOME/.gemini/antigravity-cli"
elif [ "$_PLATFORM" = "qwen" ]; then
  _APP_DIR="$_HOME/.qwen"
elif [ "$_PLATFORM" = "kimi" ]; then
  _APP_DIR="$_HOME/.kimi-code"
elif [ "$_PLATFORM" = "claude" ]; then
  _APP_DIR="$_HOME/.claude"
elif [ "${ANTIGRAVITY_AGENT:-}" = "1" ] || [ -n "${ANTIGRAVITY_CONVERSATION_ID:-}" ]; then
  if [ ! -d "$_HOME/.gemini/antigravity-cli" ] && [ -d "$_HOME/.claude" ]; then
    _APP_DIR="$_HOME/.claude"
  else
    _APP_DIR="$_HOME/.gemini/antigravity-cli"
  fi
else
  _APP_DIR="$_HOME/.claude"
fi

if [ -n "${CLAUDE_STATE_DIR:-}" ]; then
  _CACHE_DIR="$CLAUDE_STATE_DIR"
elif [ -n "${ANTIGRAVITY_STATE_DIR:-}" ]; then
  _CACHE_DIR="$ANTIGRAVITY_STATE_DIR"
else
  _CACHE_DIR="$_APP_DIR"
fi
_CACHE_FILE="$_CACHE_DIR/.statusline-interpreter-cache"

_NOW=0
if ! printf -v _NOW "%(%s)T" -1 2>/dev/null || [ -z "$_NOW" ] || [ "$_NOW" -le 0 ] 2>/dev/null; then
  _NOW="$(date +%s 2>/dev/null || echo 0)"
fi

PY=""
if [ -f "$_CACHE_FILE" ]; then
  _CACHED_TIME=""
  _CACHED_FP=""
  _CACHED_PY=""
  {
    read -r _CACHED_TIME
    read -r _CACHED_FP
    read -r _CACHED_PY
  } < "$_CACHE_FILE" 2>/dev/null || true

  if [ -n "$_CACHED_TIME" ] && [ -n "$_CACHED_FP" ] && [ -n "$_CACHED_PY" ]; then
    if [ "$_CACHED_FP" = "$_FP" ] && [ "$_NOW" -ge "$_CACHED_TIME" ] 2>/dev/null; then
      if [ $(( _NOW - _CACHED_TIME )) -lt 3600 ] 2>/dev/null; then
        PY="$_CACHED_PY"
      fi
    fi
  fi
fi

if [ -z "$PY" ]; then
  if [ -n "$_PY_PATH" ] && py -3 -c "" >/dev/null 2>&1; then
    PY="py -3"
  elif [ -n "$_PY3_PATH" ] && python3 -c "" >/dev/null 2>&1; then
    PY=python3
  elif [ -n "$_PYTHON_PATH" ] && python -c "" >/dev/null 2>&1; then
    PY=python
  elif [ -n "$_PYTHON_PATH" ]; then
    PY=python
  else
    PY=python3
  fi

  mkdir -p "$_CACHE_DIR" 2>/dev/null || true
  _TMP_CACHE="$_CACHE_FILE.$$.tmp"
  if printf "%s\n%s\n%s\n" "$_NOW" "$_FP" "$PY" > "$_TMP_CACHE" 2>/dev/null; then
    mv -f "$_TMP_CACHE" "$_CACHE_FILE" 2>/dev/null || rm -f "$_TMP_CACHE" 2>/dev/null
  fi
fi

unset _PY_PATH _PY3_PATH _PYTHON_PATH _FP _PLATFORM _NEXT_IS_PLATFORM _arg
unset _HOME _APP_DIR _CACHE_DIR _CACHE_FILE _NOW _CACHED_TIME _CACHED_FP _CACHED_PY _TMP_CACHE


