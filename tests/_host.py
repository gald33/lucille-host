"""Types and helpers shared by the runtime tests (the fixtures themselves are in conftest.py)."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from lucille_host.jobs import Context
from lucille_host.paths import Dirs
from tests._keyring import MemoryKeyring

# make_context(enabled=..., roots=..., log_results=..., audit=...) -> Context
MakeContext = Callable[..., Context]


@dataclass(frozen=True)
class HostEnv:
    """What the host_env fixture gives a test: the private home, the default profile's
    directories in it, and the keyring in memory the host uses."""

    home: Path
    dirs: Dirs
    keyring: MemoryKeyring


_HOLD = (
    "import os, sys\n"
    "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "os.lockf(descriptor, os.F_LOCK, 0)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


_TRY = (
    "import os, sys\n"
    "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "try:\n"
    "    os.lockf(descriptor, os.F_TLOCK, 0)\n"
    "except OSError:\n"
    "    print('held')\n"
    "else:\n"
    "    print('free')\n"
)


def lock_free(lock: Path) -> bool:
    """Whether another process could take ``lock`` right now."""
    result = subprocess.run(
        [sys.executable, "-c", _TRY, str(lock)], capture_output=True, text=True, check=True
    )
    return result.stdout == "free\n"


@contextlib.contextmanager
def lock_held(lock: Path) -> Iterator[Callable[[], None]]:
    """Another process holds ``lock`` (as a CLI verb would) until the block ends, or until the
    function it gives is called, from any thread."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdin is not None and holder.stdout is not None
    stdin = holder.stdin
    once = threading.Lock()

    def release() -> None:
        with once:
            if not stdin.closed:
                stdin.close()
        holder.wait(10)

    try:
        assert holder.stdout.readline() == "held\n"
        yield release
    finally:
        release()
