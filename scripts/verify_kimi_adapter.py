"""Verify statusline_lib/kimi.py's payload adapter: render_kimi_statusline's
single-line composition and its honest degradation on missing/null/wrong-typed
fields of Kimi Code CLI's camelCase payload (gitBranch null, contextTokens as
a string, model as an int, planMode as a string, zero maxContextTokens).

Same test shape as scripts/verify_qwen_adapter.py: count_active_sessions /
debounce_session_count are monkeypatched at the statusline_lib.kimi module
level (the names it imported into its own namespace) so this never touches
the live ~/.kimi-code or ~/.claude session-count/debounce state files.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.kimi as kimi_module
from statusline_lib.kimi import render_kimi_statusline


class _PatchedKimiSessions:
    """Swap statusline_lib.kimi's imported count_active_sessions /
    debounce_session_count / git_working_tree_cached names for fakes, so
    render_kimi_statusline's session-badge and working-tree-badge branches
    are exercised without touching real state files or the gitref cache."""

    def __init__(self, n_sessions, git_stats=(0, 0, 0, 0)):
        self._n_sessions = n_sessions
        self._git_stats = git_stats
        self._originals = {}

    def __enter__(self):
        self._originals["count_active_sessions"] = kimi_module.count_active_sessions
        self._originals["debounce_session_count"] = kimi_module.debounce_session_count
        self._originals["git_working_tree_cached"] = kimi_module.git_working_tree_cached
        kimi_module.count_active_sessions = lambda cwd: self._n_sessions
        kimi_module.debounce_session_count = lambda raw_count, cwd: raw_count
        kimi_module.git_working_tree_cached = lambda cwd, state_dir=None: (
            self._git_stats
        )
        return self

    def __exit__(self, *_exc_info):
        kimi_module.count_active_sessions = self._originals["count_active_sessions"]
        kimi_module.debounce_session_count = self._originals["debounce_session_count"]
        kimi_module.git_working_tree_cached = self._originals["git_working_tree_cached"]


def _render(payload, n_sessions=1, git_stats=(0, 0, 0, 0)):
    with _PatchedKimiSessions(n_sessions, git_stats):
        return render_kimi_statusline(payload, "/tmp", "|")


def _check_full_payload(failures):
    """The exact contract sample from the kimi-code source (commit
    67dd03149): every field populated, default permissionMode, no plan mode."""
    payload = {
        "model": "K3",
        "cwd": "C:/path/to/project",
        "gitBranch": "main",
        "permissionMode": "manual",
        "planMode": False,
        "contextUsage": 12,
        "contextTokens": 1024,
        "maxContextTokens": 8192,
        "sessionId": "abc123def456",
        "version": "0.29.2",
    }
    line = _render(payload)
    if "\n" in line:
        failures.append(f"kimi renders exactly one line, got {line!r}")
    if "(main)" not in line:
        failures.append(f"line should show the payload's gitBranch, got {line!r}")
    if "[abc123de]" not in line:
        failures.append(f"line should show the 8-char session-id badge, got {line!r}")
    if "K3" not in line:
        failures.append(f"line should show the model badge, got {line!r}")
    if "1.0K" not in line or "8.2K" not in line:
        failures.append(f"line should show context usage, got {line!r}")
    if "manual" in line:
        failures.append(f"the default permissionMode should be omitted, got {line!r}")
    if "PLAN" in line:
        failures.append(f"planMode false should not light the PLAN badge, got {line!r}")
    if "v0.29.2" not in line:
        failures.append(f"line should show the CLI-version badge, got {line!r}")


def _check_null_git_branch(failures):
    line = _render({"gitBranch": None, "sessionId": "abc"})
    if "(None)" in line or "(null)" in line:
        failures.append(f"null gitBranch must not render a branch, got {line!r}")


def _check_missing_keys(failures):
    """An empty payload still renders the spinner/host/cwd prefix (the TUI's
    non-empty-first-line contract) with no ` | ` field section."""
    line = _render({})
    if "[" not in line:
        failures.append(f"empty payload should still render the prefix, got {line!r}")
    if " | " in line:
        failures.append(f"empty payload should have no field section, got {line!r}")


def _check_wrong_typed_fields(failures):
    """Every field wrong-typed at once: model as an int stringifies into the
    badge, contextTokens "x" degrades to 0, gitBranch as a list drops, and
    planMode as the string "false" must NOT light the badge (only a real
    boolean True counts)."""
    payload = {
        "model": 5,
        "gitBranch": [1, 2],
        "permissionMode": 7,
        "planMode": "false",
        "contextTokens": "x",
        "maxContextTokens": [8192],
        "sessionId": 42,
    }
    line = _render(payload)
    if "5" not in line:
        failures.append(f"int model should still render (stringified), got {line!r}")
    if "PLAN" in line:
        failures.append(f"string planMode must not light the badge, got {line!r}")
    if "[42]" not in line:
        failures.append(f"int sessionId should still badge (stringified), got {line!r}")


def _check_non_default_permission_modes(failures):
    line = _render({"permissionMode": "yolo"})
    if "yolo" not in line:
        failures.append(f"yolo permissionMode should render, got {line!r}")
    line = _render({"permissionMode": "auto"})
    if "auto" not in line:
        failures.append(f"auto permissionMode should render, got {line!r}")


def _check_plan_mode_true(failures):
    line = _render({"planMode": True})
    if "PLAN" not in line:
        failures.append(f"planMode true should light the PLAN badge, got {line!r}")


def _check_zero_max_context_tokens(failures):
    """maxContextTokens 0: the shared badge.format_context's unknown-window
    path renders the honest '???' denominator instead of a wrong percentage."""
    line = _render({"contextTokens": 1024, "maxContextTokens": 0})
    if "???" not in line:
        failures.append(
            f"zero maxContextTokens should degrade to the '???' denominator, got {line!r}"
        )


def _check_sessions_badge(failures):
    line = _render({"sessionId": "abc"}, n_sessions=3)
    if "[3 sessions]" not in line:
        failures.append(f"line should show the multi-session badge, got {line!r}")
    line = _render({"sessionId": "abc"}, n_sessions=1)
    if "sessions]" in line:
        failures.append(f"a single session should not badge, got {line!r}")


def _check_version_badge(failures):
    """Absent/blank/container versions render no badge; a payload-carried
    leading "v" is stripped so we never render "vv..."; numeric scalars
    stringify like the session-id badge does."""
    line = _render({})
    if " v" in line:
        failures.append(f"absent version should not badge, got {line!r}")
    line = _render({"version": "  "})
    if " v" in line:
        failures.append(f"blank version should not badge, got {line!r}")
    line = _render({"version": ["0", "29"]})
    if "['0'" in line or '["0"]' in line:
        failures.append(f"container version should drop, got {line!r}")
    line = _render({"version": "v1.2.3"})
    if "v1.2.3" not in line or "vv1.2.3" in line:
        failures.append(f"leading-v version should render once, got {line!r}")


def _check_working_tree_badge(failures):
    """The `+A -B ↑x ↓y` badge inside the branch parens: both sections,
    sync-only, diff-only, and all-zero (no badge, branch renders bare).
    Counts come from the patched git_working_tree_cached, so this exercises
    kimi.py's composition only -- the cache itself is verify_git_ref_cache's
    contract."""
    line = _render({"gitBranch": "main"}, git_stats=(2, 1, 58, 0))
    if "+2" not in line or "-1" not in line or "↑58" not in line:
        failures.append(f"line should show the full working-tree badge, got {line!r}")
    line = _render({"gitBranch": "main"}, git_stats=(0, 0, 0, 3))
    if "↓3" not in line or "+" in line:
        failures.append(f"sync-only stats should badge only the sync, got {line!r}")
    line = _render({"gitBranch": "main"}, git_stats=(5, 2, 0, 0))
    if "+5" not in line or "-2" not in line or "↑" in line or "↓" in line:
        failures.append(f"diff-only stats should badge only the diff, got {line!r}")
    line = _render({"gitBranch": "main"}, git_stats=(0, 0, 0, 0))
    if "(main)" not in line or "↑" in line or "+" in line:
        failures.append(f"zero stats must render the bare branch, got {line!r}")


def check(failures):
    _check_full_payload(failures)
    _check_null_git_branch(failures)
    _check_missing_keys(failures)
    _check_wrong_typed_fields(failures)
    _check_non_default_permission_modes(failures)
    _check_plan_mode_true(failures)
    _check_zero_max_context_tokens(failures)
    _check_sessions_badge(failures)
    _check_version_badge(failures)
    _check_working_tree_badge(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: statusline_lib/kimi.py's adapter renders Kimi's single-line "
        "statusline and survives missing/null/wrong-typed payload fields"
    )


if __name__ == "__main__":
    main()
