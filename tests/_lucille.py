"""Lucille's side of a pairing, played on Switchboard's in-process test hub.

It does what Lucille's backend does with the library: it starts a pairing and shows the code,
collects the host's answer, and mints and offers work rooms over the pair. It keeps its pairs in a
folder of its own under the test's temporary folder, never under ``~/.switchboard``. Its agent id
is ``lucille``, never the host's ``lucille-host``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from switchboard import pairing, sharing

AGENT_ID = "lucille"


class FakeLucille:
    def __init__(self, h: Any, folder: Path) -> None:
        self.h = h
        self.connect = h.client_class()
        self.base = h.client_config(workspace="lucille", agent_id=AGENT_ID)
        self.pairs = pairing.PairStore(folder / "lucille-pairs", env={})
        self.rooms = sharing.ShareStore(folder / "lucille-pairs", env={})
        self.name: str | None = None

    def _with(self, **options: Any) -> dict[str, Any]:
        return {"connect": self.connect, "now": self.h.now, **options}

    def start(self, **options: Any) -> str:
        """Start a pairing; returns the code Lucille's page shows."""
        started = pairing.start(self.base, **self._with(store=self.pairs, **options))
        self.name = started.name
        return started.code

    def collect(self) -> Any:
        assert self.name is not None
        return pairing.collect(self.name, self.base, **self._with(store=self.pairs))

    def offer(self, room: str, **options: Any) -> Any:
        """Mint a work room and offer it over the pair."""
        assert self.name is not None
        return sharing.share_new(
            room,
            self.base,
            pairs=[self.name],
            **self._with(store=self.rooms, pair_store=self.pairs, **options),
        )

    def touch(self) -> Any:
        assert self.name is not None
        return sharing.touch(
            self.base, pairs=[self.name], **self._with(store=self.rooms, pair_store=self.pairs)
        )
