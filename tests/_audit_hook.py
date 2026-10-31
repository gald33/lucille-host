"""Record, with Python's audit hooks, every process started and every socket bound or connected.

An audit hook cannot be removed once added, so one is added the first time a test asks, for the
rest of the run, and it records only while a :func:`recording` block is open. It sees the events of
every thread.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Iterator
from typing import Any

SPAWN_EVENTS = frozenset(
    {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty", "os.spawn"}
    | {"os.system", "os.startfile"}
)
SOCKET_EVENTS = frozenset({"socket.bind", "socket.connect"})

_installed = False
_install_lock = threading.Lock()
_sinks: list[list[tuple[str, Any]]] = []


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if not _sinks:
        return
    if event in SPAWN_EVENTS:
        seen = (event, args[1] if event == "subprocess.Popen" else args[0])
    elif event in SOCKET_EVENTS:
        seen = (event, args[1])
    else:
        return
    for sink in list(_sinks):
        sink.append(seen)


@contextlib.contextmanager
def recording() -> Iterator[list[tuple[str, Any]]]:
    """Yield a list that collects ``(event, argv or address)`` until the block ends."""
    global _installed
    with _install_lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True
    sink: list[tuple[str, Any]] = []
    _sinks.append(sink)
    try:
        yield sink
    finally:
        _sinks.remove(sink)


def spawns(events: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [event for event in events if event[0] in SPAWN_EVENTS]
