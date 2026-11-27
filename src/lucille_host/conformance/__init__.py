"""The conformance suite: a hostile fake Lucille that checks the host's invariants.

Installed with ``lucille-host[conformance]``, ``python -m lucille_host.conformance`` starts a real
Switchboard hub on 127.0.0.1 itself (``switchboard serve``), pairs the installed ``lucille-host``
with a fake Lucille through it, drives the host as Lucille would, and runs every registered
scenario (``_scenarios.py``). It prints one line per scenario and a summary, and exits 0 only when
at least one scenario ran and every one passed: a run with no scenarios exits 1, so nothing can
read it as a pass. It needs no network beyond 127.0.0.1, and refuses to run as root, as the host
does.

The fake Lucille (``_lucille.py``) signs with a throwaway key made for the run, never Lucille's
own. The host takes it only on a loopback hub its local configuration names (``pair begin --hub
--test-key``), so the suite can pair nothing with Lucille's hub.

This package is not part of the runtime. The runtime lints exempt it by name, and it has a lint of
its own: it may start only the local hub (``switchboard serve``) and the installed
``lucille-host``, from a single module, ``_spawn.py``. The lint lets that module name each program
by a constant bound to its path beside the interpreter running the suite
(``Path(sys.executable).parent / "lucille-host"``), so that the suite starts the copy installed with
it rather than one found first on ``PATH``. ``tools/guards/ast_lint.py`` lists the forms it accepts
and the ones it refuses; this package is not held to the runtime's import allowlist, so a
standard-library module that starts a process inside (``platform.architecture`` runs ``file``) is
not refused here, and review covers it. The runtime may not import this package.

Scenarios live in modules named ``scenarios_*.py`` in this package. :func:`load` imports each with
a plain ``import`` statement (never by name), and the order of those statements is the run order.
"""

from __future__ import annotations

import sys

__all__ = ["load", "main"]


def load() -> None:
    """Import every scenario module, so that each registers its scenarios."""
    from lucille_host.conformance import scenarios_plumbing  # noqa: F401


def main() -> int:
    """Run every registered scenario; 0 only when at least one ran and all passed."""
    from lucille_host.conformance._scenarios import run_all

    load()
    return run_all(sys.stdout, sys.stderr)
