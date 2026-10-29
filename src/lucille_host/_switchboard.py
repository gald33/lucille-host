"""The only runtime module that imports Switchboard.

The host talks to Lucille through agent-switchboard (MIT, pinned ``==2.6.*``, base install with no
extras). Keeping every import of it in this one module lets a reader see the whole surface the
host uses in one place, and the Switchboard-surface lint (``tools/guards/switchboard_lint.py``)
fails CI on a ``switchboard`` import or use anywhere else in the runtime package, including one
that reaches it through this module.

The surface is the plain synchronous library. The calls the design names are:

- ``switchboard.pairing``: ``PairingCode.decode``, ``accept``, ``resolve``, ``forget`` and
  ``status``, with ``PairStore`` for ``--store-file``;
- ``switchboard.client.Client``: ``post``, ``inbox`` with a ``since`` cursor, and
  ``read_channels``, on clients built from the ``config`` of a pairing ``Resolution`` or a sharing
  ``SharedRoom``, each given a fresh ``switchboard.signing.SigningIdentity.generate()``;
- ``switchboard.sharing``: ``touch``, ``accept``, ``resolve_room`` and ``leave``, the calls that
  read and take a room Lucille offers over the pair, with the host's own ``ShareStore``. Never
  ``share_new``: the host offers no room of its own.

Never ``register()``, an invite, a pairing the host starts (``pairing.start``), a heal, an export
of the pair secret, the Switchboard CLI, its MCP bridge, or a hosted bridge.

The lint enforces this at the level of modules and their members: it allows only the submodules
and members listed in its ``SURFACE``, which also covers the types and errors these calls take and
raise. A method called on a client object is not checked by name, except ``register``, anything
named for an invite, and private members; review covers the rest.

Another runtime module may take from here only what this module shows to be its own: a function
or class it defines, or a constant it assigns, that uses no part of Switchboard in its
decorators, bases or value. The lint presumes every other name here is Switchboard, so
``CLIENT_CLASS`` and the ``*_CALLS`` tuples below stay inside this module.

In this release the module has no behaviour. It only names the library entry points the host will
call, which also makes importing it fail if the pinned library no longer has them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from switchboard import client, pairing, sharing

# Nothing here is for ``from lucille_host._switchboard import *``, which the lints refuse anyway.
__all__: list[str] = []

# The hub the host talks to. A pairing code naming any other hub is refused (a loopback hub is
# accepted only when the local configuration names it, for the conformance suite).
LUCILLE_HUB: Final = "https://switchboard.lucille-ai.com"

# The library entry points the host may call. Nothing calls them yet.
PAIRING_CALLS: Final[tuple[Callable[..., Any], ...]] = (
    pairing.PairingCode.decode,
    pairing.accept,
    pairing.resolve,
    pairing.forget,
    pairing.status,
)
CLIENT_CLASS: Final[type] = client.Client
SHARING_CALLS: Final[tuple[Callable[..., Any], ...]] = (
    sharing.touch,
    sharing.accept,
    sharing.resolve_room,
    sharing.leave,
)
