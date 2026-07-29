#!/usr/bin/env python3
"""Post the aislop score as a commit status from CI (stdlib only).

Reads the JSON report written by ``aislop ci . --json`` and POSTs a Gitea
commit status on $GITHUB_SHA using the auto $GITHUB_TOKEN, so the score shows
directly in the PR's list of checks (mirrors post-coverage-status.py, whose
``pr-crew/coverage`` status reads e.g. "85.6% line coverage"). Matches the
schoen-lab and git-wizard fleet convention for the ``aislop/score`` context.

The gate itself (exit non-zero below ci.failBelow) stays in the workflow's
aislop step; this script only reports. The workflow passes the gate outcome
via --state so the status colors match the job result.

On an unreadable report it posts state=error (visible as unreadable, not
silently missing) and still exits 0 so an ``if: always()`` step does not
double-fail the job. A POST/network failure DOES raise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def _post(context: str, state: str, description: str) -> None:
    server = os.environ["GITHUB_SERVER_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    body = json.dumps(
        {
            "context": context,
            "state": state,
            "description": description,
            "target_url": f"{server}/{repository}/actions/runs/{run_id}",
        }
    ).encode()
    request = urllib.request.Request(
        f"{server}/api/v1/repos/{repository}/statuses/{sha}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    # Gitea serves a publicly-trusted Let's Encrypt cert, so urllib's default
    # verifying context validates it with no custom CA handling - verification
    # stays ON so a cert problem fails loudly rather than leaking the token.
    urllib.request.urlopen(request).read()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="aislop/score")
    parser.add_argument("--report", required=True, help="aislop --json report path")
    parser.add_argument(
        "--state",
        required=True,
        choices=["success", "failure"],
        help="gate outcome from the workflow step (the gate exits there)",
    )
    arguments = parser.parse_args(argv[1:])
    try:
        with open(arguments.report) as handle:
            report = json.load(handle)
        score = report["score"]
        files = report["summary"]["files"]
    except Exception as error:  # report unreadable -> post error, exit 0
        print(f"aislop report unreadable: {error}", file=sys.stderr)
        _post(arguments.context, "error", "aislop report unreadable")
        return 0
    description = f"score {score}/100 over {files} files"
    _post(arguments.context, arguments.state, description)
    print(f"posted {arguments.context} {arguments.state}: {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
