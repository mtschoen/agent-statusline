"""The static half of the render-budget invariant: what the source must say.

Four production incidents shared one disease: a synchronous call inside a
render that can block for many seconds (2026-07-02, SMB per-file stats plus
walker timeout stalls, 20s renders; 2026-07-10, psutil attribute expansion,
11s renders; 2026-07-11, beacons-history over an SMB root, 5s timeout stalls;
2026-07-16, pace and spend transcript walks over an SMB extra root, 5.5s
renders). Three scans make the cure mechanical instead of tribal.

Bounded subprocesses, over the library and the entry points. Every subprocess
call must carry an explicit numeric ``timeout=`` no greater than
``_MAX_SUBPROCESS_TIMEOUT`` seconds, at the call site or defaulted in the
wrapper, and ``subprocess.Popen`` and ``time.sleep`` are banned outright. The
resident server is why this is a scan over source rather than a measurement of
a render: the refreshers still shell out to git and to the walker, but they do
it on a bounded worker pool that the loop answering a client never waits on.

Import-free clients. In the resident-server model the render path is
statusline_client.py plus the statusline_client_support.py it reaches through
a lazy import, and nothing else. Neither may import anything outside the
standard library, with one named exception each, because importing
statusline_lib costs several times everything else a client does.

One datagram. Exactly one socket send and one receive, each inside the one
function allowed to make it, and neither inside a loop. A client that retries,
polls, or fans out is a client that can block.

The live end-to-end benchmark against a real server lives next door in
verify_render_budget.py, which imports and runs everything here as well, so
either file alone is a complete verdict on the invariant. The split is for the
400-line file gate, not a change of scope.

Run from anywhere: this file scans by path and imports nothing from the
package, so it can be imported before a suite has installed its own isolated
HOME without dragging statusline_lib in early.
"""

import ast
import os
import sys

_TEXT_ENCODING = "utf-8"

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The render path: everything importable from a statusline render. install.py
# and friends are excluded, since installers may run long.
_RENDER_PATH_FILES = [
    os.path.join(_REPO, "statusline.py"),
    os.path.join(_REPO, "subagent_statusline.py"),
    os.path.join(_REPO, "qwen_statusline.py"),
    os.path.join(_REPO, "wrap_nudge.py"),
]
_RENDER_PATH_FILES += [
    os.path.join(_REPO, "statusline_lib", f)
    for f in sorted(os.listdir(os.path.join(_REPO, "statusline_lib")))
    if f.endswith(".py")
    and f
    not in (
        "codex_install.py",
        "nudge_install.py",
        # process_safe.py implements the bounded-timeout replacement for
        # subprocess.run/Popen this scan exists to enforce elsewhere (its
        # Popen call is the sanctioned kill-then-abandon-reader pattern, not
        # the unbounded raw usage the ban targets) -- scanning it would flag
        # the fix as the violation.
        "process_safe.py",
        # server_control.py's blocking poll (wait_until_gone) is statusline-ctl
        # `server stop` only, never the render path -- see its module docstring.
        "server_control.py",
    )
]

_MAX_SUBPROCESS_TIMEOUT = 2.0

_CLIENT = os.path.join(_REPO, "statusline_client.py")
_CLIENT_SUPPORT = os.path.join(_REPO, "statusline_client_support.py")

# Per client file: the one import from outside the standard library it is
# allowed, and the function that import has to sit inside. Everything else,
# including the standard library's own subprocess, is a failure.
_ALLOWED_IMPORTS = {
    _CLIENT: ("statusline_client_support", "_support"),
    _CLIENT_SUPPORT: ("statusline_lib.process_safe", "_spawn"),
}

_SEND_ATTRIBUTES = ("send", "sendto")
_RECEIVE_ATTRIBUTES = ("recv", "recvfrom")

# Per client file: the one function allowed a socket send, and the one allowed
# a receive. None means the file may make no such call anywhere. The support
# module's shutdown is fire and forget by design, so a receive appearing there
# would be a blocking wait on the fallback path, which is the disease itself.
_SEND_FUNCTIONS = {_CLIENT: "request_render", _CLIENT_SUPPORT: "_send_shutdown"}
_RECEIVE_FUNCTIONS = {_CLIENT: "request_render", _CLIENT_SUPPORT: None}


def _parse(path):
    with open(path, encoding=_TEXT_ENCODING) as f:
        return ast.parse(f.read(), filename=path)


def _numeric_value(node):
    """Return the numeric value of a Constant/negated-Constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    return None


def _default_timeouts(node):
    """Every defaulted `timeout` parameter of a function definition, positional
    and keyword-only alike, as (parameter name, default node) pairs."""
    arguments = node.args
    positional = arguments.args[len(arguments.args) - len(arguments.defaults) :]
    defaults = dict(zip([a.arg for a in positional], arguments.defaults, strict=True))
    defaults.update(
        {
            a.arg: d
            for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
            if d is not None
        }
    )
    return [(name, default) for name, default in defaults.items() if name == "timeout"]


def _subprocess_timeout_violations(path):
    """Yield (lineno, message) for subprocess calls without a bounded timeout."""
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A wrapper that takes timeout as a parameter must bound its DEFAULT.
            for _, default in _default_timeouts(node):
                value = _numeric_value(default)
                if value is None or value > _MAX_SUBPROCESS_TIMEOUT:
                    yield (
                        node.lineno,
                        f"{node.name}() defaults timeout={ast.dump(default)}"
                        f" (must be numeric <= {_MAX_SUBPROCESS_TIMEOUT})",
                    )
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_subprocess = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
            and function.attr in ("run", "check_output", "check_call", "call", "Popen")
        )
        if is_subprocess and function.attr == "Popen":
            yield (node.lineno, "subprocess.Popen is banned in the render path")
            continue
        timeout_keyword = next((k for k in node.keywords if k.arg == "timeout"), None)
        if is_subprocess:
            if timeout_keyword is None:
                yield (node.lineno, f"subprocess.{function.attr} without timeout=")
                continue
            value = _numeric_value(timeout_keyword.value)
            # A Name (forwarded parameter) is allowed: the wrapper's default
            # is checked above, and explicit call-site overrides are caught
            # by the constant check below when literal.
            if isinstance(timeout_keyword.value, ast.Name):
                continue
            if value is None or value > _MAX_SUBPROCESS_TIMEOUT:
                yield (
                    node.lineno,
                    f"subprocess.{function.attr} timeout must be numeric <="
                    f" {_MAX_SUBPROCESS_TIMEOUT}",
                )
        elif timeout_keyword is not None:
            # Any other call passing a literal timeout (e.g. a walker wrapper)
            # must also stay within the cap.
            value = _numeric_value(timeout_keyword.value)
            if value is not None and value > _MAX_SUBPROCESS_TIMEOUT:
                yield (
                    node.lineno,
                    f"call passes timeout={value} > {_MAX_SUBPROCESS_TIMEOUT}",
                )
        is_sleep = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "time"
            and function.attr == "sleep"
        )
        if is_sleep:
            yield (node.lineno, "time.sleep is banned in the render path")


def check_render_path_sync_calls(failures):
    for path in _RENDER_PATH_FILES:
        relative = os.path.relpath(path, _REPO)
        for lineno, message in _subprocess_timeout_violations(path):
            failures.append(f"{relative}:{lineno}: {message}")


def _enclosing_function_name(tree, node):
    """The name of the innermost function `node` sits inside, or None when it
    sits at module level. Innermost wins, so a call inside a nested helper is
    attributed to the helper rather than to whatever encloses it."""
    innermost = None
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(descendant is node for descendant in ast.walk(candidate)) and (
            innermost is None or candidate.lineno > innermost.lineno
        ):
            innermost = candidate
    return None if innermost is None else innermost.name


def _attribute_calls(tree, attribute_names):
    """Every Call node in `tree` that calls one of the named methods."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in attribute_names
    ]


def _attribute_call_sites(tree, attribute_names):
    """(lineno, enclosing function name) for every call of the named methods."""
    return [
        (node.lineno, _enclosing_function_name(tree, node))
        for node in _attribute_calls(tree, attribute_names)
    ]


def _looping_call_sites(tree, attribute_names):
    """Line numbers of named-method calls that sit inside a loop.

    Counting call sites is not enough on its own: a `while` around the one
    send the design allows is still a retry, and still reads as exactly one
    call to everything above.
    """
    inside = set()
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for node in _attribute_calls(loop, attribute_names):
            inside.add(node.lineno)
    return sorted(inside)


def _import_roots(node):
    """(the root module names an import statement pulls in, what to call it in
    a message). For `from a.b import c` the root is `a`: `c` is a name inside
    the module, not a module of its own."""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return {module.split(".")[0]}, module
    return {alias.name.split(".")[0] for alias in node.names}, ", ".join(
        alias.name for alias in node.names
    )


def check_client_is_import_free(failures):
    """The client must not import statusline_lib on its hot path. Importing
    the package costs several times everything else the client does, which is
    the entire reason this file exists as a separate entry point.

    Two exceptions, asserted rather than merely allowed: the hot path's own
    lazy import of the support module, and the function-local import of
    statusline_lib.process_safe inside the spawn helper. The second runs only
    on the fallback path, after the line has already been printed, and
    process_safe is the repository's only sanctioned subprocess surface.
    """
    for path, (allowed_module, allowed_function) in _ALLOWED_IMPORTS.items():
        tree = _parse(path)
        name = os.path.basename(path)
        allowed_import_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            roots, imported = _import_roots(node)
            if "subprocess" in roots:
                failures.append(
                    f"{name}:{node.lineno}: subprocess is banned in the client;"
                    " process_safe is the only sanctioned subprocess surface"
                )
                continue
            if all(root in sys.stdlib_module_names for root in roots):
                continue
            enclosing = _enclosing_function_name(tree, node)
            if imported == allowed_module and enclosing == allowed_function:
                allowed_import_count += 1
                continue
            failures.append(
                f"{name}:{node.lineno}: {imported} is outside the standard"
                f" library; the only one allowed here is {allowed_module}"
                f" inside {allowed_function}()"
            )
        if allowed_import_count != 1:
            failures.append(
                f"{name}: {allowed_module} inside {allowed_function}() appears"
                f" {allowed_import_count} times, expected exactly 1"
            )


def _check_datagram_calls(failures, name, tree, attributes, expected, label):
    """Exactly one call of `attributes` in the file, inside `expected`, and not
    in a loop. An `expected` of None means the file is allowed no such call at
    all."""
    sites = _attribute_call_sites(tree, attributes)
    for lineno in _looping_call_sites(tree, attributes):
        failures.append(
            f"{name}:{lineno}: socket {label} inside a loop; one datagram means"
            " one, not one per retry"
        )
    if expected is None:
        for lineno, function in sites:
            failures.append(
                f"{name}:{lineno}: no socket {label} belongs in this file,"
                f" found one in {function}()"
            )
        return
    inside = [lineno for lineno, function in sites if function == expected]
    if len(inside) != 1:
        failures.append(
            f"{name}: {expected}() makes {len(inside)} socket {label}s,"
            " expected exactly 1"
        )
    for lineno, function in sites:
        if function != expected:
            failures.append(
                f"{name}:{lineno}: socket {label} outside {expected}(), in {function}()"
            )


def check_client_sends_one_datagram(failures):
    """One send, one receive. A client that retries, polls, or fans out is a
    client that can block, which is the invariant this file guards. Counted
    per function, not per file: the count only means anything if the call is
    also in the one place the design puts it."""
    for path in (_CLIENT, _CLIENT_SUPPORT):
        tree = _parse(path)
        name = os.path.basename(path)
        _check_datagram_calls(
            failures, name, tree, _SEND_ATTRIBUTES, _SEND_FUNCTIONS[path], "send"
        )
        _check_datagram_calls(
            failures,
            name,
            tree,
            _RECEIVE_ATTRIBUTES,
            _RECEIVE_FUNCTIONS[path],
            "receive",
        )
        for lineno, message in _subprocess_timeout_violations(path):
            failures.append(f"{name}:{lineno}: {message}")


def check_static_guards(failures):
    check_render_path_sync_calls(failures)
    check_client_is_import_free(failures)
    check_client_sends_one_datagram(failures)


def main():
    failures = []
    check_static_guards(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: the render path is import-free, one-datagram, and bounded")


if __name__ == "__main__":
    main()
