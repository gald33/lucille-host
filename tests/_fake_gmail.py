"""A stand-in for Gmail's IMAP server, for the Gmail child's tests.

It is a socket-like object: the child writes commands with ``sendall`` and reads replies through
``makefile("rb")``. It answers the commands the child may send, records every command line and
literal it gets, and answers ``BAD`` to anything else, so a test can hold the child to its list.
Nothing touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ADDRESS = "reader@example.org"
PASSWORD = "abcdefghijklmnop"


@dataclass
class Message:
    uid: int
    msgid: int
    thrid: int
    raw: bytes
    flags: str = ""
    internaldate: str = "17-Nov-2026 10:00:00 +0000"


@dataclass
class FakeGmail:
    mailboxes: list[tuple[str, str]] = field(
        default_factory=lambda: [
            ("\\HasNoChildren", "INBOX"),
            ("\\HasChildren \\Noselect", "[Gmail]"),
            ("\\All \\HasNoChildren", "[Gmail]/All Mail"),
            ("\\HasNoChildren \\Sent", "[Gmail]/Sent Mail"),
            ("\\HasNoChildren \\Trash", "[Gmail]/Trash"),
            ("\\HasNoChildren \\Junk", "[Gmail]/Spam"),
        ]
    )
    messages: dict[str, list[Message]] = field(default_factory=dict)
    capabilities: str = "IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1"
    password: str = PASSWORD
    address: str = ADDRESS
    # uids SEARCH returns for a criterion: a function of (words, literal) or None for all.
    search: Any = None
    # Extra bytes added to every untagged reply (to drive the read cap).
    padding: bytes = b""
    fail_on: str | None = None  # a verb answered BAD
    timeout_on: str | None = None  # a verb after which reading times out

    def __post_init__(self) -> None:
        self.commands: list[str] = []
        self.literals: list[bytes] = []
        self._in = b""
        self._out = bytearray(b"* OK Gimap ready for requests\r\n")
        self._pending: tuple[str, int] | None = None
        self._selected: str | None = None
        self.closed = False
        self._timeout = False

    # --- the socket ---------------------------------------------------------------------------

    def makefile(self, mode: str) -> FakeGmail:
        assert mode == "rb"
        return self

    def sendall(self, data: bytes) -> None:
        self._in += data
        while True:
            if self._pending is not None:
                head, size = self._pending
                end = self._in.find(b"\r\n", size)
                if len(self._in) < size or end < 0:
                    return
                literal, self._in = self._in[:size], self._in[end + 2 :]
                self._pending = None
                self._dispatch(head, literal)
                continue
            end = self._in.find(b"\r\n")
            if end < 0:
                return
            line, self._in = self._in[:end].decode("ascii"), self._in[end + 2 :]
            match = re.search(r"\{(\d+)\}$", line)
            if match:
                self._pending = (line[: match.start()].rstrip(), int(match.group(1)))
                self._out += b"+ go ahead\r\n"
                continue
            self._dispatch(line, None)

    def readline(self, limit: int = -1) -> bytes:
        if self._timeout:
            raise TimeoutError("timed out")
        end = self._out.find(b"\n")
        end = len(self._out) if end < 0 else end + 1
        if limit is not None and limit >= 0:
            end = min(end, limit)
        line = bytes(self._out[:end])
        del self._out[:end]
        return line

    def read(self, size: int = -1) -> bytes:
        size = len(self._out) if size is None or size < 0 else size
        data = bytes(self._out[:size])
        del self._out[:size]
        return data

    def close(self) -> None:
        self.closed = True

    def shutdown(self, how: int) -> None:
        self.closed = True

    # --- the server ---------------------------------------------------------------------------

    def verbs(self) -> list[str]:
        return [
            " ".join(c.split()[:2]) if c.startswith("UID ") else c.split()[0] for c in self.commands
        ]

    def _say(self, text: str | bytes) -> None:
        data = text.encode("utf-8") if isinstance(text, str) else text
        self._out += data + self.padding + b"\r\n"

    def _dispatch(self, line: str, literal: bytes | None) -> None:
        tag, _, rest = line.partition(" ")
        self.commands.append(rest)
        if literal is not None:
            self.literals.append(literal)
        verb = rest.split()[0].upper()
        words = _words(rest)
        key = " ".join(words[:2]).upper() if verb == "UID" else verb
        if self.fail_on is not None and key == self.fail_on:
            self._say(f"{tag} BAD Could not parse command")
            return
        if verb == "CAPABILITY":
            self._say(f"* CAPABILITY {self.capabilities}")
            self._say(f"{tag} OK Thats all she wrote!")
        elif verb == "LOGIN":
            if words[1:] == [self.address, self.password]:
                self._say(f"{tag} OK {self.address} authenticated (Success)")
            else:
                self._say(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        elif verb == "LIST":
            for flags, name in self.mailboxes:
                self._say(f'* LIST ({flags}) "/" "{_escape(_mutf7(name))}"')
            self._say(f"{tag} OK Success")
        elif verb == "EXAMINE":
            name = _unmutf7(words[1])
            if name in [n for _, n in self.mailboxes]:
                self._selected = name
                count = len(self.messages.get(name, []))
                self._say(f"* {count} EXISTS")
                self._say(f"{tag} OK [READ-ONLY] {name} selected. (Success)")
            else:
                self._say(f"{tag} NO [NONEXISTENT] Unknown Mailbox: {name} (Failure)")
        elif key == "UID SEARCH":
            box = self.messages.get(self._selected or "", [])
            if "X-GM-MSGID" in words:
                wanted = int(words[words.index("X-GM-MSGID") + 1])
                uids = [m.uid for m in box if m.msgid == wanted]
            elif self.search is not None:
                uids = list(self.search(words, literal))
            else:
                uids = [m.uid for m in box]
            self._say("* SEARCH" + "".join(f" {u}" for u in uids))
            self._say(f"{tag} OK SEARCH completed (Success)")
        elif key == "UID FETCH":
            box = {m.uid: m for m in self.messages.get(self._selected or "", [])}
            wanted = [int(u) for u in words[2].split(",")]
            items = rest[rest.index("(") :]
            for index, uid in enumerate(wanted, start=1):
                message = box.get(uid)
                if message is None:
                    continue
                self._fetch(index, message, items)
            self._say(f"{tag} OK Success")
        elif verb == "LOGOUT":
            self._say("* BYE LOGOUT Requested")
            self._say(f"{tag} OK 73 GOOD BYE")
        else:
            self._say(f"{tag} BAD Unknown command")
        if self.timeout_on is not None and key == self.timeout_on:
            self._timeout = True

    def _fetch(self, index: int, message: Message, items: str) -> None:
        head = (
            f"* {index} FETCH (UID {message.uid} X-GM-MSGID {message.msgid} "
            f"X-GM-THRID {message.thrid} FLAGS ({message.flags}) "
            f'INTERNALDATE "{message.internaldate}" RFC822.SIZE {len(message.raw)} '
        )
        if "BODY.PEEK[]<0." in items:
            size = int(re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", items).group(1))  # type: ignore[union-attr]
            body = message.raw[:size]
            key = "BODY[]<0>"
        else:
            end = message.raw.find(b"\r\n\r\n")
            body = message.raw[: end + 4] if end >= 0 else message.raw
            key = "BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO)]"
        self._out += (head + f"{key} {{{len(body)}}}\r\n").encode("ascii") + body + b")\r\n"


def _words(text: str) -> list[str]:
    """IMAP words: atoms, and quoted strings unquoted."""
    found = []
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\S+)', text):
        if match.group(1) is not None:
            found.append(re.sub(r"\\(.)", r"\1", match.group(1)))
        else:
            found.append(match.group(2))
    return found


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _mutf7(text: str) -> str:
    from lucille_host import gmail

    return gmail.mutf7_encode(text)


def _unmutf7(text: str) -> str:
    from lucille_host import gmail

    return gmail.mutf7_decode(text)
