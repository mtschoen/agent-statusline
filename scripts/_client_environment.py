"""Minimal environments for resident-client verification subprocesses."""

import os

_SYSTEM_ENVIRONMENT_VARIABLES = (
    "PATH",
    "Path",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "SystemDrive",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "COMSPEC",
    "ComSpec",
    "WINDIR",
    "windir",
)


def isolated_client_environment(
    home,
    *,
    state_directory=None,
    encoding="utf-8",
    **overrides,
):
    """A child environment containing fixture state, system variables, and explicit values."""
    environment = {
        "HOME": home,
        "USERPROFILE": home,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": encoding,
    }
    for name in _SYSTEM_ENVIRONMENT_VARIABLES:
        if name in os.environ:
            environment[name] = os.environ[name]
    if state_directory is not None:
        environment["CLAUDE_STATE_DIR"] = state_directory
    for name, value in overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment
