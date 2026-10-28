"""The conformance suite. In this release it is a stub: it has no scenarios.

Running it (``python -m lucille_host.conformance``) prints that there are no scenarios and exits
1, so nothing can read it as a pass.

What it will be: a hostile fake Lucille that checks the host's invariants. Installed with
``lucille-host[conformance]``, it will start a real Switchboard hub on 127.0.0.1 itself, drive the
installed ``lucille-host`` against it, and try to widen what the host permits.

This package is not part of the runtime. The runtime lints exempt it by name, and it has a lint of
its own: it may start only the local hub (``switchboard serve``) and the installed
``lucille-host``, from a single module, ``_spawn.py``, which does not exist yet. The lint lets that
module name each program by a constant bound to its path beside the interpreter running the suite
(``Path(sys.executable).parent / "lucille-host"``), so that the suite will start the copy
installed with it rather than one found first on ``PATH``. ``tools/guards/ast_lint.py`` lists the
forms it accepts and the ones it refuses; this package is not held to the runtime's import
allowlist, so a standard-library module that starts a process inside (``platform.architecture``
runs ``file``) is not refused here, and review covers it. The runtime may not import this package.
"""

from __future__ import annotations

import sys

__all__ = ["main"]

NO_SCENARIOS = "lucille-host conformance: this release has no conformance scenarios yet."


def main() -> int:
    """Run the suite. With no scenarios, report that and return 1, so nothing reads it as a pass."""
    print(NO_SCENARIOS, file=sys.stderr)
    return 1
