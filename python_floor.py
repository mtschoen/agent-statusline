"""Minimum Python version for the statusline hooks, in a module that imports
nothing from ``statusline_lib``.

``statusline_lib`` uses 3.11+ syntax and stdlib names (``datetime.UTC``), so on
an older interpreter even ``import statusline_lib`` raises. A hook that wants to
fail with a readable message must therefore check the version BEFORE touching
the package, which is why this lives at the repo root instead of inside it.
Shared by ``wrap_nudge.py`` (runtime guard) and ``statusline_lib.nudge_install``
(install-time check), so the two can never disagree about the floor.
"""

MIN_PYTHON = (3, 11)


def is_too_old(version_info):
    """True when ``version_info`` is below the supported floor."""
    return tuple(version_info[:2]) < MIN_PYTHON


def too_old_message(version_info, executable, hook_name):
    """One-line diagnosis for a hook that cannot run on this interpreter."""
    have = ".".join(str(part) for part in version_info[:3])
    want = ".".join(str(part) for part in MIN_PYTHON)
    return (
        f"{hook_name}: {executable} is Python {have}, but agent-statusline needs "
        f"{want}+. Re-run install.py with a Python {want}+ interpreter to "
        f"rewrite this hook with an absolute interpreter path."
    )
