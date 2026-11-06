"""Minimum Python version for the statusline hooks, in a module that imports
nothing from ``statusline_lib``.

This file, and every guard block that runs before ``statusline_lib`` is
imported, must itself parse and run on very old Python 3 (3.4+: no f-strings,
no annotations, no walrus), so a too-old interpreter prints the message below
instead of a SyntaxError traceback. ``verify_wrap_nudge_python_floor.py``
enforces that. Shared by ``wrap_nudge.py`` (runtime guard) and ``install.py``
(install-time check), so they can never disagree about the floor.
"""

MIN_PYTHON = (3, 9)


def is_too_old(version_info):
    """True when ``version_info`` is below the supported floor."""
    return tuple(version_info[:2]) < MIN_PYTHON


def too_old_message(version_info, executable, hook_name):
    """One-line diagnosis for a hook that cannot run on this interpreter."""
    have = ".".join(str(part) for part in version_info[:3])
    want = ".".join(str(part) for part in MIN_PYTHON)
    return (
        hook_name
        + ": "
        + executable
        + " is Python "
        + have
        + ", but agent-statusline needs "
        + want
        + "+. Install Python "
        + want
        + "+ and make python3 resolve to it."
    )
