"""Regression checks for the render-budget static scanner itself.

Run from anywhere; imports from the sibling scanner by path and builds every
source fixture in a temporary directory.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_render_budget_static as scanner


def _write(path, text):
    with open(path, "w", encoding=scanner._TEXT_ENCODING) as source_file:
        source_file.write(text)


def _check_missing_allowed_import(failures):
    client_path = os.path.join(scanner._REPO, "statusline_client.py")
    with open(client_path, encoding=scanner._TEXT_ENCODING) as source_file:
        source = source_file.read()
    import_line = "    import statusline_client_support\n"
    if source.count(import_line) != 1:
        failures.append(
            "statusline_client.py must contain one lazy support import fixture"
        )
        return
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_path = os.path.join(temporary_directory, "statusline_client.py")
        _write(fixture_path, source.replace(import_line, ""))
        saved_allowed_imports = scanner._ALLOWED_IMPORTS
        scanner._ALLOWED_IMPORTS = {
            fixture_path: ("statusline_client_support", "_support")
        }
        try:
            scanner_failures = []
            scanner.check_client_is_import_free(scanner_failures)
        finally:
            scanner._ALLOWED_IMPORTS = saved_allowed_imports
    expected = (
        "statusline_client.py: statusline_client_support inside _support()"
        " appears 0 times, expected exactly 1"
    )
    if expected not in scanner_failures:
        failures.append(
            f"missing lazy support import was not rejected: {scanner_failures!r}"
        )


def _check_async_timeout_default(failures):
    source = """async def run_command(timeout=3.0):
    return timeout
"""
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_path = os.path.join(temporary_directory, "async_wrapper.py")
        _write(fixture_path, source)
        violations = list(scanner._subprocess_timeout_violations(fixture_path))
    expected = (
        1,
        "run_command() defaults timeout=Constant(value=3.0) (must be numeric <= 2.0)",
    )
    if expected not in violations:
        failures.append(f"async timeout default was not rejected: {violations!r}")


def main():
    failures = []
    _check_missing_allowed_import(failures)
    _check_async_timeout_default(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        raise SystemExit(1)
    print("OK: render-budget static scanner regressions")


if __name__ == "__main__":
    main()
