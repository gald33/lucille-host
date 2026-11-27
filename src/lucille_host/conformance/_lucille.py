"""Lucille's side of a pairing, played against the local hub the suite starts.

It does what Lucille's backend does with the Switchboard library: it answers the host's Connect
request with a pairing code signed by a key, collects the host's answer, mints and offers work
rooms over the pair, posts requests in them and reads the host's replies. Only this package and
the tests may mint and offer rooms (``sharing.share_new``); the Switchboard-surface lint exempts
them for that.

Its signing key is a throwaway Ed25519 key made for the run (:class:`Signer`), never Lucille's own.
The host takes answers signed with it only on a loopback hub its local configuration names
(``pair begin --hub <url> --test-key <key>``), so nothing here can pair a host to Lucille's hub.

It keeps its pairs and rooms in a folder of its own under the run's temporary folder, never under
``~/.switchboard``; its client keeps no peer log and no stash. Its agent id is ``lucille``, never
the host's ``lucille-host``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from switchboard import pairing, sharing
from switchboard.client import Client
from switchboard.config import ClientConfig

from lucille_host import connect, delivery

__all__ = ["AGENT_ID", "EXEC", "HOST_ID", "FakeLucille", "Signer"]

AGENT_ID = "lucille"
HOST_ID = "lucille-host"
EXEC = "lucille.exec"
# How many messages one read of a work room asks for.
READ_LIMIT = 1000


class Signer:
    """A throwaway Ed25519 key, standing in for Lucille's signing key: never Lucille's own."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.kid = hashlib.sha256(self.raw).digest()[:8].hex()

    @property
    def test_key(self) -> str:
        """The public key as ``pair begin --test-key`` takes it: unpadded base64url."""
        return base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("=")

    def delivery(self, code: str, nonce: bytes, *, sender: str, handle: str) -> str:
        """The body of a delivery of ``code`` for the request whose nonce is ``nonce``."""
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


def _config(url: str, *, workspace: str, agent_id: str = AGENT_ID) -> ClientConfig:
    return ClientConfig(
        url=url,
        url_source="explicit",
        token=None,
        workspace=workspace,
        workspace_source="explicit",
        agent_id=agent_id,
        peer_log="",
        stash_db="",
    )


@dataclass
class FakeLucille:
    """Lucille, on the hub at ``url``, keeping its pairs and rooms in ``folder``."""

    url: str
    folder: Path
    name: str | None = None

    def __post_init__(self) -> None:
        self.base = _config(self.url, workspace="lucille")
        self.pairs = pairing.PairStore(self.folder / "lucille-pairs", env={})
        self.rooms = sharing.ShareStore(self.folder / "lucille-pairs", env={})

    def _with(self, **options: Any) -> dict[str, Any]:
        return {"connect": Client, "now": time.time(), **options}

    # --- pairing ------------------------------------------------------------------------------

    def deliver(
        self,
        shown: str,
        signer: Signer,
        *,
        sender: str = "telegram:123456789",
        handle: str = "@conformance",
    ) -> str:
        """Answer the connect code ``shown`` as Lucille does (decisions/0002 §11.4, step 6):
        stretch it for this epoch, start a pairing whose note names the request's nonce, and post
        its code into the request's drop room, signed by ``signer``. Returns the code delivered."""
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
        body = signer.delivery(started.code, nonce, sender=sender, handle=handle)
        with Client(drop, agent_id=pairing.throwaway_agent("lucille")) as poster:
            poster.post(connect.CHANNEL, body, ttl=connect.POST_TTL)
        return str(started.code)

    def collect(self) -> Any:
        if self.name is None:
            raise RuntimeError("no pairing started")
        return pairing.collect(self.name, self.base, **self._with(store=self.pairs))

    def sas(self) -> str:
        """The SAS Lucille's page shows, once it has collected the answer."""
        record = None if self.name is None else self.pairs.get(self.name)
        if record is None or not record.sas:
            raise RuntimeError("Lucille has not collected the answer")
        return str(record.sas)

    # --- work rooms ---------------------------------------------------------------------------

    def offer(self, room: str) -> Any:
        """Mint a work room and offer it over the pair."""
        if self.name is None:
            raise RuntimeError("no pair to offer a room over")
        return sharing.share_new(
            room,
            self.base,
            pairs=[self.name],
            **self._with(store=self.rooms, pair_store=self.pairs),
        )

    def _client(self, room: str) -> Client:
        config = sharing.resolve_room(room, self.base, store=self.rooms, now=time.time()).config
        return Client(config, agent_id=AGENT_ID)

    def post(self, room: str, body: Any, *, ttl: float = 600) -> int:
        """Post ``body`` on ``lucille.exec`` in ``room``, as Lucille posts a request; its seq."""
        with self._client(room) as poster:
            return int(poster.post(EXEC, body, ttl=ttl)["seq"])

    def replies(self, room: str) -> list[dict[str, Any]]:
        """The bodies the host posted on ``lucille.exec`` in ``room``, oldest first."""
        with self._client(room) as reader:
            rows = reader.read_channels([reader.cipher.blind_channel(EXEC)], limit=READ_LIMIT)
            host = reader.peer_id(HOST_ID)
        found = sorted(
            (row for row in rows if row.get("from") == host and not row.get("unreadable")),
            key=lambda row: row["seq"],
        )
        return [row["body"] for row in found if isinstance(row.get("body"), dict)]

    def reply(self, room: str, job_id: str) -> dict[str, Any] | None:
        """The host's first reply for ``job_id`` in ``room``."""
        for sent in self.replies(room):
            if sent.get("dir") == "reply" and sent.get("job_id") == job_id:
                return sent
        return None
