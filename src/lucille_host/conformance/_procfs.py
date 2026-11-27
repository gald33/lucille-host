"""What Linux's ``/proc`` shows about a process the host started: its environment and open files.

The conformance suite reads these to check that a child of the host gets the built environment
and nothing more: no variable the host's own environment holds beyond the allowlist, and no file
descriptor but its three standard streams. It only reads ``/proc``; it starts nothing.

``/proc/<pid>/environ`` is the environment the process was started with, which is what the
host passed; a process that changes its own environment later does not change it. Reading it
needs the same user as the process, which the suite is. On macOS there is no ``/proc``, and
:func:`available` is False: the scenarios that need it report that they could not check, and
never pass there.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Process", "available", "descendants", "parse_environ", "parse_stat_ppid", "read"]

PROC = Path("/proc")


def available() -> bool:
    """Whether this computer has a ``/proc`` that shows this process's own environment."""
    return sys.platform.startswith("linux") and (PROC / "self" / "environ").exists()


def parse_stat_ppid(stat: str) -> int:
    """The parent pid from the text of ``/proc/<pid>/stat``. The command name, in parentheses,
    may hold spaces and parentheses itself, so the fields are read after its last ``)``."""
    fields = stat.rsplit(")", 1)[1].split()
    return int(fields[1])


def parse_environ(raw: bytes) -> dict[str, str]:
    """The environment from the bytes of ``/proc/<pid>/environ``: ``NAME=value`` items, each
    ended by a NUL. An item with no ``=`` is kept with an empty value, so that it is still seen."""
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        name, _, value = item.partition(b"=")
        env[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


@dataclass(frozen=True)
class Process:
    pid: int
    argv: tuple[str, ...]
    env: dict[str, str]
    fds: tuple[int, ...]


def _ppid(pid: int) -> int | None:
    try:
        return parse_stat_ppid((PROC / str(pid) / "stat").read_text("utf-8", "replace"))
    except (OSError, ValueError, IndexError):
        return None


def descendants(root: int) -> Iterator[int]:
    """Every live process below ``root``, at the moment each is looked at."""
    parents: dict[int, int] = {}
    for entry in os.listdir(PROC):
        if entry.isdigit():
            ppid = _ppid(int(entry))
            if ppid is not None:
                parents[int(entry)] = ppid
    below = {root}
    grew = True
    while grew:
        grew = False
        for pid, ppid in parents.items():
            if ppid in below and pid not in below:
                below.add(pid)
                grew = True
    below.discard(root)
    yield from sorted(below)


def read(pid: int) -> Process | None:
    """The process's argv, starting environment and open descriptors, or None once it is gone."""
    base = PROC / str(pid)
    try:
        argv = tuple(
            part.decode("utf-8", "replace")
            for part in (base / "cmdline").read_bytes().split(b"\0")
            if part
        )
        env = parse_environ((base / "environ").read_bytes())
        fds = tuple(sorted(int(fd) for fd in os.listdir(base / "fd")))
    except (OSError, ValueError):
        return None
    return Process(pid, argv, env, fds)
