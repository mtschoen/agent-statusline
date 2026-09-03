"""Test telemetry and process fixtures for verify_server_concurrency.py."""

import json
import os
import subprocess
import sys
import threading

# The repository root, so statusline_lib is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.server_jobs import WorkerPool


class RecordingWorkerPool(WorkerPool):
    """A WorkerPool subclass that records every submit() attempt and its
    verdict without changing any production pool behavior."""

    def __init__(self, size, runner, error_logger):
        super().__init__(size=size, runner=runner, error_logger=error_logger)
        self._telemetry_lock = threading.Lock()
        self._submissions = []

    def submit(self, kind, argument):
        accepted = super().submit(kind, argument)
        with self._telemetry_lock:
            self._submissions.append((kind, str(argument), accepted))
        return accepted

    def submissions(self):
        """A shallow snapshot of (kind, argument_str, accepted) in call order."""
        with self._telemetry_lock:
            return list(self._submissions)


_WRAPPER_SOURCE = """
import json
import os
import subprocess
import sys

command = json.loads(sys.argv[1])
payload_path = sys.argv[2]
pid_path = sys.argv[3]

with open(payload_path, "rb") as stdin_file:
    child = subprocess.Popen(
        command,
        stdin=stdin_file,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

temp_pid_path = f"{pid_path}.tmp"
with open(temp_pid_path, "w", encoding="utf-8") as f:
    f.write(str(child.pid))
os.replace(temp_pid_path, pid_path)

child.wait()
"""


def start_client_under_parent(command, payload_path, environment, pid_path):
    """Launch a wrapper parent process that starts a client with stdin fed from
    payload_path, writes the client child's PID atomically to pid_path, and
    waits for it."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRAPPER_SOURCE,
            json.dumps(command),
            payload_path,
            pid_path,
        ],
        env=environment,
    )
