"""The only place in the runtime package that may start a process.

Every operation that runs an external command will do it through this module, and nowhere else:
the AST lint (``tools/guards/ast_lint.py``) fails CI on a process-spawning call or import in
``src/lucille_host/`` outside this file, in the forms it lists and through the re-exports it
follows, including a name taken from this module that stands for a launcher. In this file it
requires anything that starts a process to be called directly, so that nothing leaves it under
another name; what this module exports is its own functions. The conformance package,
``src/lucille_host/conformance/``, is not part of the runtime and has its own, narrower rule.

The rules this module will follow when it runs a command:

- argv only, never a shell; the executable is an absolute path taken from the release's catalog;
- every value that came from a request goes after ``--``;
- stdin is ``/dev/null``, and the child's environment is built from an allowlist, never copied
  from the host's own;
- each child runs in its own session, and its whole process group is killed on timeout;
- output is read incrementally, by a reader thread per stream (a selector would need
  ``register``, which the Switchboard-surface lint refuses everywhere), and capped.

In this release no operation runs a command, so this module starts nothing: it does not import a
process-spawning module, and :func:`run` only raises.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn


def run(argv: Sequence[str]) -> NoReturn:
    """Run one catalog command. Not implemented: this release starts no process."""
    raise NotImplementedError("this release of lucille-host runs no commands")
