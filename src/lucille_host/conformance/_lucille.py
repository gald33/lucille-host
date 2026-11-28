"""Lucille's side, played by the suite against the local hub, with the Switchboard library.

It does what Lucille's backend does: it answers the host's Connect request by delivering a pairing
code it signs with a throwaway key (never Lucille's own: the host takes it because the suite passed
its public half to ``pair begin --test-key``), collects the host's answer, mints a work room and
offers it over the pair, posts requests there and reads the host's replies.

It keeps its pairs in a folder under the suite's temporary folder, never under ``~/.switchboard``.
Its HTTP client ignores proxy settings in the environment, so that it reaches only the hub on
127.0.0.1. Its agent id is ``lucille``, never the host's ``lucille-host``.

The steps of the Connect answer use the host's own ``connect`` and ``delivery`` modules for the
code's arithmetic (decisions/0002 §11.4): what the suite tests is how the host treats what it is
sent, not that arithmetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from switchboard import pairing, sharing
from switchboard.client import Client
from switchboard.config import ClientConfig

from lucille_host import connect, delivery

AGENT_ID = "lucille"
HOST_ID = "lucille-host"
EXEC = "lucille.exec"
# The most messages a read of the work room returns: the most the hub gives in one read.
READ_MOST = 500
# The epochs whose drop rooms Lucille reads for a Connect code, as offsets from its own clock's
# epoch E: E, E-1, E+1 and E-2 (decisions/0002 §11.4, step 6). The host stretched the code with the
# epoch of its own clock when it drew it, and an epoch can end before Lucille stretches it too.
EPOCH_OFFSETS = (0, -1, 1, -2)


class NoRequest(Exception):
    """No drop room, or more than one, of the epochs Lucille reads holds the host's hello."""


class LoopbackClient(Client):
    """Switchboard's client, with an HTTP client that reads no proxy setting from the
    environment."""

    def __init__(self, config: ClientConfig, *, agent_id: str | None = None) -> None:
        http = httpx.Client(base_url=config.url, timeout=40.0, trust_env=False)
        super().__init__(config, agent_id=agent_id, http=http)
        # The client closes what it did not build only when told it owns it.
        self._owns_http = True


def connect_client(config: ClientConfig, *, agent_id: str | None = None) -> Client:
    return LoopbackClient(config, agent_id=agent_id)


class Signer:
    """A throwaway Ed25519 key, in the place of Lucille's signing key."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.kid = hashlib.sha256(self.raw).digest()[:8].hex()

    @property
    def test_key(self) -> str:
        """The public key as ``pair begin --test-key`` takes it: unpadded base64url."""
        return base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("=")

    def delivery(self, code: str, nonce: bytes, *, sender: str, handle: str) -> str:
        signed = delivery.signed_bytes(self.kid, nonce, code, sender, handle)
        sig = base64.urlsafe_b64encode(self.private.sign(signed)).decode("ascii").rstrip("=")
        return json.dumps(
            {
                "t": delivery.TYPE,
                "code": code,
                "kid": self.kid,
                "from": sender,
                "handle": handle,
                "sig": sig,
            }
        )


class Lucille:
    """Lucille, on the hub at ``url``, keeping its pairs and rooms in ``folder``."""

    def __init__(self, url: str, folder: Any) -> None:
        self.url = url
        self.base = ClientConfig(
            url=url,
            url_source="explicit",
            token=None,
            workspace="lucille",
            workspace_source="explicit",
            agent_id=AGENT_ID,
            peer_log="",
            stash_db="",
        )
        self.pairs = pairing.PairStore(folder / "lucille-pairs", env={})
        self.rooms = sharing.ShareStore(folder / "lucille-pairs", env={})
        self.signer = Signer()
        self.name: str | None = None
        # The clock whose epoch Lucille stretches a Connect code with.
        self.clock: Callable[[], float] = time.time

    def _with(self, **options: Any) -> dict[str, Any]:
        return {"connect": connect_client, "now": time.time(), **options}

    def deliver(self, shown: str, *, sender: str, handle: str) -> int:
        """Answer the Connect code the host showed: start a pairing whose note names the
        request's nonce, and post its code, signed, into the request's drop room.

        Like Lucille, it reads the drop rooms of every epoch in :data:`EPOCH_OFFSETS`, and
        delivers into the one that holds the host's hello. The offset of that room's epoch from
        this clock's is returned; :class:`NoRequest` if no room, or more than one, holds it."""
        code = connect.parse_code(shown)
        if code is None:
            raise ValueError(f"not a connect code: {shown!r}")
        here = connect.epoch(self.clock())
        found = [
            (offset, key)
            for offset in EPOCH_OFFSETS
            for key in [connect.stretch(code, here + offset)]
            if self._holds_hello(key)
        ]
        if len(found) != 1:
            offsets = ", ".join(f"{offset:+d}" for offset, _ in found) or "none"
            raise NoRequest(
                f"the host's hello is in the drop rooms of epochs {offsets} of Lucille's clock "
                f"({len(EPOCH_OFFSETS)} read), not in exactly one"
            )
        offset, key = found[0]
        nonce = connect.nonce(key)
        started = pairing.start(
            self.base,
            **self._with(store=self.pairs, note=connect.note_for(nonce), with_token=False, ttl=900),
        )
        self.name = started.name
        body = self.signer.delivery(started.code, nonce, sender=sender, handle=handle)
        with connect_client(self._drop(key), agent_id=pairing.throwaway_agent("lucille")) as poster:
            poster.post(connect.CHANNEL, body, ttl=connect.POST_TTL)
        return offset

    def _drop(self, key: bytes) -> ClientConfig:
        """The drop room of the stretched key ``key``."""
        room = pairing.handshake_room(key)
        return ClientConfig(
            url=self.url,
            workspace=room.workspace,
            key=room.key,
            write_key=room.write_key,
            peer_log="",
            stash_db="",
        )

    def _holds_hello(self, key: bytes) -> bool:
        """Whether the drop room of ``key`` holds the host's hello."""
        with connect_client(self._drop(key), agent_id=pairing.throwaway_agent("lucille")) as reader:
            rows = reader.history(connect.CHANNEL, limit=connect.READ_LIMIT)
        return any(isinstance(row, dict) and row.get("body") == connect.HELLO for row in rows)

    def collect(self) -> str:
        """Collect the host's answer; the pairing's state."""
        assert self.name is not None
        return str(pairing.collect(self.name, self.base, **self._with(store=self.pairs)).state)

    def sas(self) -> str | None:
        """The SAS Lucille's page shows, once it has collected the answer."""
        assert self.name is not None
        record = self.pairs.get(self.name)
        return record.sas if record is not None and record.sas else None

    def offer(self, room: str) -> None:
        """Mint a work room and offer it over the pair."""
        assert self.name is not None
        sharing.share_new(
            room,
            self.base,
            pairs=[self.name],
            **self._with(store=self.rooms, pair_store=self.pairs),
        )

    def _room(self, room: str) -> Client:
        config = sharing.resolve_room(room, self.base, store=self.rooms, now=time.time()).config
        return connect_client(config, agent_id=AGENT_ID)

    def post(self, room: str, body: Any, *, ttl: float = 600) -> int:
        """Post ``body`` on ``lucille.exec`` in ``room``; its seq."""
        with self._room(room) as poster:
            return int(poster.post(EXEC, body, ttl=ttl)["seq"])

    def replies(self, room: str) -> list[dict[str, Any]]:
        """The bodies the host posted on ``lucille.exec`` in ``room`` that are replies, oldest
        first."""
        with self._room(room) as reader:
            rows = reader.history(EXEC, limit=READ_MOST)
            host = reader.peer_id(HOST_ID)
        found = [
            row
            for row in rows
            if row.get("from") == host
            and isinstance(row.get("body"), dict)
            and row["body"].get("dir") == "reply"
        ]
        return [row["body"] for row in sorted(found, key=lambda row: row["seq"])]
