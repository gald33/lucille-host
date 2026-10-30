"""What another process of the same user does beside a test: ``python -m tests._elsewhere``.

The tests run ``lucille-host`` in-process, against Switchboard's in-process test hub, which no other
process can reach. Some claims are about another process, though: a lock that keeps out only the
other threads of its own process passes every test that runs both sides as threads. So these run in
a process of their own, started by :func:`start` from the repository's root, with the test's
environment (its ``$HOME`` among it):

- ``forget [SECONDS]``: ``lucille-host forget`` for the default profile, with the hub out of reach.
  Every request fails to connect before any socket is opened, so Lucille is not told; with
  ``SECONDS``, each fails only after that long, as a hub that does not answer holds a request until
  the client's timeout. The profile must keep its pair in files (``--store-file``): this process
  cannot see the test's keyring in memory.
- ``journal PATH JOB_ID...``: once a line arrives on stdin, write a journal entry at ``PATH`` for
  each job id, then print the ids this process took, as JSON.

The host refuses to run as root, and the tests may run as root: here, as in the tests, the host sees
the user id ``TEST_UID``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from switchboard import client

from lucille_host import _switchboard, cli
from lucille_host.journal import Journal

REPO = Path(__file__).resolve().parents[1]
TEST_UID = 1000


def start(*argv: str) -> subprocess.Popen[str]:
    """Start ``python -m tests._elsewhere ARGV...``, with pipes for stdin, stdout and stderr."""
    return subprocess.Popen(
        [sys.executable, "-m", "tests._elsewhere", *argv],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("the hub is out of reach", request=request)


class Unreachable(client.Client):
    """A Switchboard client whose every request fails to connect, before any socket is opened,
    once :attr:`delay` seconds have passed."""

    delay = 0.0

    def __init__(self, config: Any, **options: Any) -> None:
        options.pop("http", None)
        refused = httpx.Client(base_url=config.url, transport=httpx.MockTransport(self._unanswered))
        super().__init__(config, http=refused, **options)

    def _unanswered(self, request: httpx.Request) -> httpx.Response:
        time.sleep(self.delay)
        return _refuse(request)


def _forget(patch: pytest.MonkeyPatch, delay: float) -> int:
    Unreachable.delay = delay
    real = _switchboard.open_link

    def open_link(dirs: Any, config: Any, **_: Any) -> _switchboard.Link:
        return real(dirs, config, client_class=Unreachable)

    patch.setattr("lucille_host._switchboard.open_link", open_link)
    return cli.main(["forget"])


def _journal(path: str, job_ids: list[str]) -> int:
    journal = Journal(Path(path), clock=time.time)
    sys.stdin.readline()
    taken = [
        job_id
        for job_id in job_ids
        if journal.received(
            job_id, room="w", seq=1, received_at=time.time(), not_after=None, body={}
        )
        is not None
    ]
    print(json.dumps(taken), flush=True)
    return 0


def main(argv: list[str]) -> int:
    patch = pytest.MonkeyPatch()
    patch.setattr("os.geteuid", lambda: TEST_UID)
    if argv[:1] == ["forget"]:
        return _forget(patch, float(argv[1]) if len(argv) > 1 else 0.0)
    if argv[:1] == ["journal"]:
        return _journal(argv[1], argv[2:])
    print(f"no such step: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
