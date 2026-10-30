"""Types and helpers shared by the runtime tests (the fixtures themselves are in conftest.py)."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from lucille_host.jobs import Context

# make_context(enabled=..., roots=..., log_results=..., audit=...) -> Context
MakeContext = Callable[..., Context]


_HOLD = (
    "import os, sys\n"
    "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "os.lockf(descriptor, os.F_LOCK, 0)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextlib.contextmanager
def lock_held(lock: Path) -> Iterator[None]:
    """Another process holds ``lock`` (as a CLI verb would) until the block ends."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdin is not None and holder.stdout is not None
    try:
        assert holder.stdout.readline() == "held\n"
        yield
    finally:
        holder.stdin.close()
        holder.wait(10)
