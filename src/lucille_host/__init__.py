"""lucille-host: runs a closed set of named operations for a paired Lucille account.

This development version pairs with Lucille's code (``pair``), keeps the pair and answers Lucille's
requests in the work rooms Lucille offers over it (``run``), within the catalog, the local policy
and the local controls, and shows and ends the pair (``status``, ``log``, ``forget``). No release
exists.
"""

from __future__ import annotations

__all__ = ["__version__"]

# The single source of the package version (pyproject.toml reads it from here).
__version__ = "0.1.0.dev0"
