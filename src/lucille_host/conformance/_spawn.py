"""The one module of the conformance suite that starts processes.

It starts two programs, and only these, each named by a constant bound to its path beside the
interpreter running the suite, so that the suite starts the copies installed with it rather than
ones found first on ``PATH``:

- the local hub, ``switchboard serve``, on 127.0.0.1, on a port the hub picks (``--port 0``);
- the installed host, ``lucille-host``, with the arguments the suite gives.

``tools/guards/ast_lint.py`` (rules ``conformance-spawn`` and ``conformance-argv``) holds this
module to that. Every other module of the package calls the functions here, which hand back the
process; none of them names a launcher.

It also counts what it starts (:data:`STARTED`), so that a scenario can tell how many processes
the suite itself started, apart from what the host started for an operation, which the host's
audit log records.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

__all__ = [
    "LUCILLE_HOST_EXE",
    "STARTED",
    "SWITCHBOARD_EXE",
    "host",
    "host_process",
    "hub_process",
]

# The programs installed beside the interpreter running the suite.
SWITCHBOARD_EXE = Path(sys.executable).parent / "switchboard"
LUCILLE_HOST_EXE = Path(sys.executable).parent / "lucille-host"

# How many processes this module has started, by program.
STARTED: dict[str, int] = {"switchboard": 0, "lucille-host": 0}


def hub_process(db: Path, *, env: Mapping[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    """Start the local hub on 127.0.0.1, on a port it picks, keeping its data in ``db``. Its
    standard error says the port once it listens (``Uvicorn running on http://127.0.0.1:<port>``).
    """
    process = subprocess.Popen(
        [
            SWITCHBOARD_EXE,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--db",
            str(db),
            "--log-level",
            "info",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    STARTED["switchboard"] += 1
    return process


def host_process(
    args: Sequence[str], *, env: Mapping[str, str], cwd: Path, piped: bool
) -> subprocess.Popen[bytes]:
    """Start the installed ``lucille-host`` with ``args``; its standard input is a pipe this
    process holds the write end of when ``piped``, and nothing otherwise."""
    process = subprocess.Popen(
        [LUCILLE_HOST_EXE, *args],
        stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    STARTED["lucille-host"] += 1
    return process


def host(
    args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run the installed ``lucille-host`` with ``args`` to its end, with no standard input."""
    STARTED["lucille-host"] += 1
    return subprocess.run(
        [LUCILLE_HOST_EXE, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
