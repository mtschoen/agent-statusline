"""Verify statusline_lib/server_info.py: the server info file (server.json)
round trip and the code-version digest algorithm.

Covers:
  - version_input_files: sorted, forward-slash, repository-relative names,
    recursing under statusline_lib/ and including every root entry point
    regardless of whether that file exists.
  - code_version: stable across repeat calls, changes when a file's mtime
    is bumped by a second, changes when a file grows (mtime held fixed so
    size is isolated as the cause), and changes when a root entry point
    file that was missing appears.
  - server_info_path: state_dir(state_directory)/server.json.
  - write_server_info / read_server_info: atomic round trip, parent
    directory creation, None on a missing file and on malformed or
    non-object JSON.
  - remove_server_info: safe (no raise) on a missing path.

pid_is_alive and _resolve_psutil are covered separately, in
verify_server_info_liveness.py -- kept apart so neither file grows past the
400-line file-size cap.

Run from anywhere; imports from `agent-statusline` by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.server_info as server_info

_FIXED_MTIME = 1_700_000_000
_ENCODING = "utf-8"


def _check_server_info_path(failures):
    with tempfile.TemporaryDirectory() as state:
        path = server_info.server_info_path(state)
        expected = os.path.join(state, server_info.SERVER_INFO_FILENAME)
        if path != expected:
            failures.append(f"server_info_path: expected {expected!r}, got {path!r}")


def _check_version_input_files(failures):
    with tempfile.TemporaryDirectory() as root:
        nested = os.path.join(root, "statusline_lib", "nested")
        os.makedirs(nested)
        open(os.path.join(root, "statusline_lib", "base.py"), "w").close()
        open(os.path.join(nested, "deep.py"), "w").close()
        open(os.path.join(root, "statusline_lib", "notes.txt"), "w").close()

        names = server_info.version_input_files(root)

        if names != sorted(names):
            failures.append("version_input_files is not sorted")
        if "statusline_lib/base.py" not in names:
            failures.append("version_input_files missing statusline_lib/base.py")
        if "statusline_lib/nested/deep.py" not in names:
            failures.append(
                "version_input_files did not recurse into nested directories"
            )
        if any(name.endswith("notes.txt") for name in names):
            failures.append("version_input_files included a non-.py file")
        for entry_point in (
            "statusline.py",
            "subagent_statusline.py",
            "qwen_statusline.py",
            "kimi_statusline.py",
            "statusline_client.py",
            "statusline_server.py",
        ):
            if entry_point not in names:
                failures.append(
                    f"version_input_files missing root entry point {entry_point}"
                )
        if any("\\" in name for name in names):
            failures.append(
                "version_input_files used backslashes instead of forward slashes"
            )


def _write_pinned(path, contents):
    with open(path, "w", encoding=_ENCODING) as f:
        f.write(contents)
    os.utime(path, (_FIXED_MTIME, _FIXED_MTIME))


def _check_code_version_stability(failures):
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "statusline_lib"))
        module_path = os.path.join(root, "statusline_lib", "base.py")
        entry_path = os.path.join(root, "statusline.py")
        _write_pinned(module_path, "x = 1\n")
        _write_pinned(entry_path, "print('hi')\n")

        first = server_info.code_version(root)
        second = server_info.code_version(root)
        if first != second:
            failures.append(
                "code_version is not stable across calls on an unchanged tree"
            )

        os.utime(module_path, (_FIXED_MTIME + 1, _FIXED_MTIME + 1))
        bumped = server_info.code_version(root)
        if bumped == first:
            failures.append("code_version did not change after a one-second mtime bump")

        os.utime(module_path, (_FIXED_MTIME, _FIXED_MTIME))
        restored = server_info.code_version(root)
        if restored != first:
            failures.append(
                "code_version did not return to its original value after the mtime was restored"
            )

        with open(module_path, "a", encoding=_ENCODING) as f:
            f.write("y = 2\n")
        os.utime(module_path, (_FIXED_MTIME, _FIXED_MTIME))
        grown = server_info.code_version(root)
        if grown == restored:
            failures.append(
                "code_version did not change after a file grew with its mtime held fixed"
            )


def _check_code_version_missing_entry_point(failures):
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "statusline_lib"))
        without = server_info.code_version(root)

        kimi_path = os.path.join(root, "kimi_statusline.py")
        _write_pinned(kimi_path, "pass\n")
        with_file = server_info.code_version(root)

        if without == with_file:
            failures.append(
                "code_version did not change when a missing root entry point appeared"
            )


def _check_read_write_round_trip(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "server.json")

        if server_info.read_server_info(path) is not None:
            failures.append("read_server_info on a missing file must return None")

        server_info.write_server_info(
            path,
            pid=1234,
            port=54321,
            version="abc123",
            started_at=1_700_000_000.0,
            platform="claude",
        )
        info = server_info.read_server_info(path)
        expected = {
            "pid": 1234,
            "port": 54321,
            "version": "abc123",
            "started_at": 1_700_000_000.0,
            "platform": "claude",
        }
        if info != expected:
            failures.append(f"read_server_info round trip mismatch: {info!r}")

        with open(path, "w", encoding=_ENCODING) as f:
            f.write("{not json")
        if server_info.read_server_info(path) is not None:
            failures.append("read_server_info on malformed JSON must return None")

        with open(path, "w", encoding=_ENCODING) as f:
            f.write("[1, 2, 3]")
        if server_info.read_server_info(path) is not None:
            failures.append(
                "read_server_info on a non-object JSON value must return None"
            )

        server_info.remove_server_info(path)
        if os.path.exists(path):
            failures.append("remove_server_info did not remove the file")

        # Safe on an already-missing path.
        server_info.remove_server_info(path)
        server_info.remove_server_info(os.path.join(tmp, "never-existed.json"))


def _check_write_server_info_creates_directories(failures):
    with tempfile.TemporaryDirectory() as tmp:
        nested_path = os.path.join(tmp, "nested", "dir", "server.json")
        server_info.write_server_info(
            nested_path,
            pid=1,
            port=2,
            version="v",
            started_at=0.0,
            platform="p",
        )
        if server_info.read_server_info(nested_path) is None:
            failures.append(
                "write_server_info did not create missing parent directories"
            )


def check(failures):
    _check_server_info_path(failures)
    _check_version_input_files(failures)
    _check_code_version_stability(failures)
    _check_code_version_missing_entry_point(failures)
    _check_read_write_round_trip(failures)
    _check_write_server_info_creates_directories(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server info file round trip and code version verified")


if __name__ == "__main__":
    main()
