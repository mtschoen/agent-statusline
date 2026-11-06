"""Python 3.9-safe stand-ins for newer stdlib helpers used by the verify
scripts and tools: itertools.pairwise and zip(strict=True) (3.10), and
sys.stdlib_module_names (3.10)."""

import importlib.util
import sys
import sysconfig


def pairwise(items):
    """Overlapping adjacent pairs of a sequence: (s0, s1), (s1, s2), ..."""
    items = list(items)
    return list(zip(items, items[1:]))


def zip_strict(*sequences):
    """zip() that raises ValueError when the inputs differ in length."""
    sequences = [list(sequence) for sequence in sequences]
    lengths = {len(sequence) for sequence in sequences}
    if len(lengths) > 1:
        raise ValueError(
            "zip_strict arguments differ in length: "
            + ", ".join(str(len(sequence)) for sequence in sequences)
        )
    return list(zip(*sequences))


def is_stdlib_module(root):
    """True when the top-level module name `root` ships with the interpreter."""
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return root in names
    if root in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(root)
    if spec is None:
        return False
    origin = spec.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    stdlib = sysconfig.get_paths()["stdlib"]
    return origin.startswith(stdlib) and "site-packages" not in origin
