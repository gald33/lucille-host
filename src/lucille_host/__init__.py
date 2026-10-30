"""lucille-host: will run a closed set of named operations for a paired Lucille account.

This version has the catalog, the executor, the local policy, the audit log and the local
controls, with the CLI verbs that change them. It cannot pair with Lucille or read a request yet,
so nothing can ask it to run anything.
"""

from __future__ import annotations

__all__ = ["__version__"]

# The single source of the package version (pyproject.toml reads it from here).
__version__ = "0.1.0.dev0"
