"""The closed set of operations this release of the host can run.

:data:`OPERATIONS` is the complete list. Lucille can ask the host to run an operation only if it is
named here; nothing Lucille sends can add an entry, and the host never runs a command line or code
it was sent. Each entry will be a reviewed catalog definition (the executable, an argv template per
OS, a typed parameter schema, and limits), so a new operation means a new public release. The local
policy file can only enable entries, map folder aliases to paths, and tighten limits.

In this release the set is empty: the host can run no operation at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["OPERATIONS"]

# Read-only: MappingProxyType refuses assignment and deletion.
OPERATIONS: Final[Mapping[str, object]] = MappingProxyType({})
