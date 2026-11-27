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

    def _with(self, **options: Any) -> dict[str, Any]:
        return {"connect": connect_client, "now": time.time(), **options}

    def deliver(self, shown: str, *, sender: str, handle: str) -> None:
        """Answer the Connect code the host showed: start a pairing whose note names the
        request's nonce, and post its code, signed, into the request's drop room."""
        code = connect.parse_code(shown)
        if code is None:
            raise ValueError(f"not a connect code: {shown!r}")
        key = connect.stretch(code, connect.epoch(time.time()))
        nonce = connect.nonce(key)
        started = pairing.start(
            self.base,
            **self._with(store=self.pairs, note=connect.note_for(nonce), with_token=False, ttl=900),
        )
        self.name = started.name
        room = pairing.handshake_room(key)
        drop = ClientConfig(
            url=self.url,
            workspace=room.workspace,
            key=room.key,
            write_key=room.write_key,
            peer_log="",
            stash_db="",
        )
        body = self.signer.delivery(started.code, nonce, sender=sender, handle=handle)
        with connect_client(drop, agent_id=pairing.throwaway_agent("lucille")) as poster:
            poster.post(connect.CHANNEL, body, ttl=connect.POST_TTL)

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
