"""Lucille's side of a pairing, played on Switchboard's in-process test hub.

It does what Lucille's backend does with the library: it starts a pairing and shows the code,
collects the host's answer, mints and offers work rooms over the pair, posts requests in them and
reads the host's replies. It keeps its pairs in a folder of its own under the test's temporary
folder, never under ``~/.switchboard``. Its agent id is ``lucille``, never the host's
``lucille-host``.

Its codes and rooms name the hub at ``url``: the test hub's own address unless a test gives
another, such as Lucille's (the in-process transport reaches the test hub whatever the address).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from switchboard import crypto, pairing, sharing
from switchboard.config import ClientConfig

from lucille_host import _switchboard, connect, delivery

AGENT_ID = "lucille"
HOST_ID = "lucille-host"
EXEC = "lucille.exec"
# A body that :func:`unparsable_bodies` seals as bytes that are not JSON.
UNPARSABLE = "this body is sealed as bytes that are not JSON"


@contextlib.contextmanager
def unparsable_bodies() -> Iterator[None]:
    """While held, a message posted with the body :data:`UNPARSABLE` is sealed with its room's
    key as bytes that open but are not JSON: what any holder of a room's write key can post."""
    real = crypto.json

    class Shim:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def dumps(self, value: Any, **options: Any) -> str:
            if isinstance(value, dict) and value.get("b") == UNPARSABLE:
                return "{not json"
            return real.dumps(value, **options)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(crypto, "json", Shim())
        yield


class Signer:
    """A throwaway Ed25519 key, standing in for Lucille's signing key: never Lucille's own. A
    test pins it in Lucille's place by its ``keys``."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).digest()[:8].hex()
        self.keys = {self.kid: raw}

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


def drop_config(key: bytes, url: str = _switchboard.LUCILLE_HUB) -> Any:
    """The drop room for ``K``, opened as "The wire" says Lucille opens it."""
    room = pairing.handshake_room(key)
    return ClientConfig(
        url=url,
        workspace=room.workspace,
        key=room.key,
        write_key=room.write_key,
        peer_log="",
        stash_db="",
    )


class FakeLucille:
    def __init__(
        self, h: Any, folder: Path, *, url: str | None = None, connect: Any = None
    ) -> None:
        self.h = h
        self.connect = h.client_class() if connect is None else connect
        self.base = h.client_config(workspace="lucille", agent_id=AGENT_ID)
        if url is not None:
            self.base = replace(self.base, url=url)
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

    def drop(self, key: bytes, body: Any) -> None:
        """Post ``body`` into the drop room of ``K``, as anyone who holds the code can."""
        config = drop_config(key, self.base.url)
        with self.connect(config, agent_id=pairing.throwaway_agent("lucille")) as poster:
            poster.post(connect.CHANNEL, body, ttl=connect.POST_TTL)

    def read_drop(self, key: bytes) -> list[Any]:
        """What the drop room of ``K`` holds, as Lucille reads it."""
        config = drop_config(key, self.base.url)
        with self.connect(config, agent_id=pairing.throwaway_agent("lucille")) as reader:
            return list(reader.history(connect.CHANNEL, limit=connect.READ_LIMIT))

    def deliver(
        self,
        shown: str,
        signer: Signer,
        *,
        at_epoch: int | None = None,
        sender: str = "telegram:123456789",
        handle: str = "@ada",
        note: str | None = None,
        **options: Any,
    ) -> str:
        """Answer ``connect <shown>`` as Lucille does (decisions/0002 §11.4, step 6): stretch the
        code for the epoch (this hub clock's, unless ``at_epoch`` names one), start a pairing
        whose note names the request's nonce, and post its code into the drop room, signed by
        ``signer``. Returns the pairing code delivered."""
        code = connect.parse_code(shown)
        assert code is not None, shown
        at = connect.epoch(self.h.now) if at_epoch is None else at_epoch
        key = connect.stretch(code, at)
        nonce = connect.nonce(key)
        options.setdefault("with_token", False)
        options.setdefault("ttl", 900)
        started = pairing.start(
            self.base,
            **self._with(
                store=self.pairs,
                note=f"lucille-host/2 n={nonce.hex()}" if note is None else note,
                **options,
            ),
        )
        self.name = started.name
        self.drop(key, signer.delivery(started.code, nonce, sender=sender, handle=handle))
        return started.code

    def collect(self) -> Any:
        assert self.name is not None
        return pairing.collect(self.name, self.base, **self._with(store=self.pairs))

    def resolve(self) -> Any:
        assert self.name is not None
        return pairing.resolve(self.name, self.base, **self._with(store=self.pairs))

    def forget(self) -> Any:
        """End the pair, as removing the computer in Lucille does."""
        assert self.name is not None
        return pairing.forget(self.name, self.base, **self._with(store=self.pairs))

    def sas(self) -> str:
        """The SAS Lucille's page shows, once it has collected the answer."""
        assert self.name is not None
        record = self.pairs.get(self.name)
        assert record is not None and record.sas, "Lucille has not collected the answer"
        return record.sas

    def offer(
        self, room: str, *, url: str | None = None, token: str | None = None, **options: Any
    ) -> Any:
        """Mint a work room and offer it over the pair: on Lucille's own hub URL unless ``url``
        names another, and carrying the hub token ``token`` when one is given."""
        assert self.name is not None
        base = self.base if url is None else replace(self.base, url=url)
        if token is not None:
            base = replace(base, token=token)
            options["with_token"] = True
        return sharing.share_new(
            room,
            base,
            pairs=[self.name],
            **self._with(store=self.rooms, pair_store=self.pairs, **options),
        )

    def offer_again(self, room: str) -> Any:
        """Offer a room Lucille already holds over the current pair too, as no Lucille should:
        what a host must refuse when the room is one it left."""
        assert self.name is not None
        record = self.pairs.get(self.name)
        assert record is not None
        held = self.rooms.load()
        held["rooms"][room]["list"].append({"pair": self.name, "th": record.transcript})
        self.rooms.save(held)
        return self.touch()

    def leave(self, room: str) -> Any:
        """Leave a room Lucille holds, as it does once it no longer uses it."""
        return sharing.leave(
            f"shared:{room}", self.base, **self._with(store=self.rooms, pair_store=self.pairs)
        )

    def heal(self) -> Any:
        """Move pair:<name> to a new generation."""
        assert self.name is not None
        return pairing.heal(self.name, self.base, **self._with(store=self.pairs))

    def touch(self) -> Any:
        assert self.name is not None
        return sharing.touch(
            self.base, pairs=[self.name], **self._with(store=self.rooms, pair_store=self.pairs)
        )

    # --- requests and replies in a work room --------------------------------------------------

    def room_config(self, room: str) -> Any:
        """The configuration of a room Lucille holds, as Lucille names it."""
        return sharing.resolve_room(room, self.base, store=self.rooms, now=self.h.now).config

    def client(self, room: str, *, agent_id: str = AGENT_ID, **changes: Any) -> Any:
        """A client of a room Lucille holds; ``changes`` alter its configuration (a test can
        drop the room's key, say, to post a body nobody can open)."""
        config = self.room_config(room)
        if changes:
            config = replace(config, **changes)
        return self.connect(config, agent_id=agent_id)

    def request(
        self,
        room: str,
        body: Any,
        *,
        ttl: float = 600,
        kind: str = "note",
        thread: str | None = None,
        channel: str = EXEC,
        agent_id: str = AGENT_ID,
    ) -> int:
        """Post ``body`` in ``room``, as Lucille would post a request; returns its seq."""
        with self.client(room, agent_id=agent_id) as poster:
            return int(poster.post(channel, body, type=kind, thread=thread, ttl=ttl)["seq"])

    def post_unparsable(self, config: Any, channel: str) -> int:
        """Post on ``channel``, in the room ``config`` names, a body that opens with the room's
        key but is not JSON. Returns its seq, read by a client with no key, which is told no
        more than that."""
        with unparsable_bodies(), self.connect(config, agent_id="writer") as poster:
            hub_channel = poster.cipher.blind_channel(channel)
            # The poster's own read of what the hub stored cannot parse it either.
            with pytest.raises(ValueError):
                poster.post(channel, UNPARSABLE)
        with self.connect(replace(config, key=""), agent_id="stranger") as plain:
            return max(int(row["seq"]) for row in plain.read_channels([hub_channel], limit=1000))

    def replies(self, room: str) -> list[dict[str, Any]]:
        """Every message the host posted on ``lucille.exec`` in ``room``, oldest first. Read
        the way that tolerates a message nobody can open or parse, which a test may have
        planted: as the host's own readers read."""
        with self.client(room) as reader:
            reader.cipher = _switchboard._Opener(reader.cipher)
            rows = reader.read_channels([reader.cipher.blind_channel(EXEC)], limit=1000)
            host = reader.peer_id(HOST_ID)
        return sorted(
            (row for row in rows if row["from"] == host and not row.get("unreadable")),
            key=lambda row: row["seq"],
        )

    def reply(self, room: str, job_id: str) -> dict[str, Any] | None:
        """The host's first reply for ``job_id`` in ``room``, as the message that carried it."""
        for row in self.replies(room):
            sent = row["body"]
            if (
                isinstance(sent, dict)
                and sent.get("dir") == "reply"
                and sent.get("job_id") == job_id
            ):
                return row
        return None
