"""The Gmail operations' child: read Gmail over IMAP with an app password kept on this computer.

The catalog runs it as ``<the host's interpreter> -I -B -W ignore -X utf8 -m lucille_host.gmail
--state <state folder> --profile <profile> -- <operation> <args as JSON>`` (decisions/0003,
"Second use"), never inside ``run``. What it does, in order, each refusal writing one fixed line
on standard error, holding no value and nothing Gmail wrote:

0. It reads its arguments by position and checks the args again against the catalog's schema and
   rule ``gmail-args``; anything malformed, or any exception it does not raise on purpose, is
   :data:`INTERNAL_LINE` and exit 6.
1. It reads ``gmail/<credential>`` and refuses (exit 2) when there is none, when its bound hosts do
   not include ``imap.gmail.com``, or when its value is not an address and an app password
   (:data:`VALUE`, matched whole).
2. It counts sign-ins with that credential in the state folder: past 60 in 10 minutes it exits 5;
   within 15 minutes of a sign-in Gmail refused, and with the credential not saved again since,
   it exits 7 without connecting.
3. It resolves ``imap.gmail.com`` and refuses (exit 4) unless every address is allowed by
   ``proxy.address_allowed``; it connects to a vetted address, never to the name, with TLS from
   the first byte on port 993, checked for ``imap.gmail.com`` against certifi's bundle
   (``proxy.tls_connect``). ``imaplib`` never opens a connection of its own: :class:`_Imap`
   replaces its ``open``, ``read`` and ``readline`` with that socket's. Its debug transcript is
   never on (``debug`` 0) and nothing of it is written.
4. It sends only ``CAPABILITY``, ``LOGIN``, ``LIST``, ``EXAMINE``, ``UID SEARCH``, ``UID FETCH`` of
   :data:`SEARCH_ITEMS` or :data:`READ_ITEMS`, and ``LOGOUT``. No command changes anything, and no
   fetch item marks a message read.
5. It reads at most :data:`READ_CAP` from Gmail, never asking for more than one byte past what is
   left (exit 5 past it, nothing on standard output).
6. ``LOGIN`` answered ``NO`` is exit 7; any other ``NO`` is exit 3; a ``BAD`` to anything is exit 6.
7. It writes one JSON document on standard output on success. Every string in it has the app
   password replaced (in any case, with or without spaces or hyphens between its letters, and in
   base64 at each alignment), and the host's own redaction applied to it alone, so that no
   private-key block runs past its string; a block that took the end of a read's text sets
   ``text_truncated``, and one begun in another field takes the text up to the first end of a
   block, or whole (:func:`_close_blocks`). All of that comes before anything is cut; then each
   field is cut to its limit, and the document is made to fit :data:`DOCUMENT_CAP` as rule
   ``stream-size`` measures it (:func:`_fit`). Descriptors 1 and 2 point at the null device from
   its start, and it ends with ``os._exit``.
"""

from __future__ import annotations

import binascii
import datetime
import email.parser
import email.policy
import imaplib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, NoReturn

from lucille_host import nesting, proxy
from lucille_host._output import PRIVATE_KEY_MARK, TOKEN_MARK, key_block_end, redact_to_end

__all__ = [
    "HOST",
    "INTERNAL_LINE",
    "READ_ITEMS",
    "SEARCH_ITEMS",
    "VALUE",
    "Reply",
    "html_text",
    "main",
    "mutf7_decode",
    "mutf7_encode",
    "run",
]

HOST: Final = "imap.gmail.com"
PORT: Final = 993
SERVICE: Final = "gmail"
OPERATIONS: Final = ("gmail.check", "gmail.mailboxes", "gmail.search", "gmail.read")

READ_CAP: Final = 8 * 1024 * 1024
MESSAGE_FETCH: Final = 4 * 1024 * 1024
TEXT_CAP: Final = 32 * 1024
HEADER_CAP: Final = 1024
RECIPIENTS_CAP: Final = 4 * 1024
FILENAME_CAP: Final = 255
# The most of rule stream-size's 64 KiB a document may take (spec/PROTOCOL.md section 13).
DOCUMENT_CAP: Final = 60 * 1024
# What a read's attachment list keeps of it before its text is cut to fit.
ATTACHMENTS_SHARE: Final = 8 * 1024
MAILBOXES_MAX: Final = 500
# The longest line read from Gmail, as imaplib's own limit.
LINE_MAX: Final = 1_000_000
LIMIT_DEFAULT: Final = 20
SIGNIN_WINDOW_S: Final = 600
SIGNINS_MAX: Final = 60
REFUSED_WAIT_S: Final = 900

# The credential's value: the account's address, one space, the app password's 16 letters.
VALUE: Final = re.compile(
    r"^[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}"
    r"[A-Za-z]{2,63} [a-z]{16}$",
    re.ASCII,
)
ADDRESS_MAX: Final = 254

SEARCH_ITEMS: Final = (
    "(UID FLAGS INTERNALDATE RFC822.SIZE X-GM-MSGID X-GM-THRID "
    "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO)])"
)
READ_ITEMS: Final = (
    f"(UID FLAGS INTERNALDATE RFC822.SIZE X-GM-MSGID X-GM-THRID BODY.PEEK[]<0.{MESSAGE_FETCH}>)"
)
SPECIAL_USE: Final = (
    "\\All",
    "\\Archive",
    "\\Drafts",
    "\\Flagged",
    "\\Important",
    "\\Junk",
    "\\Sent",
    "\\Trash",
)

OK: Final = 0
CREDENTIAL: Final = 2
REFUSED: Final = 3
ADDRESS: Final = 4
CAP: Final = 5
INTERNAL: Final = 6
SIGN_IN: Final = 7

_LINE: Final = "lucille-host gmail: "
INTERNAL_LINE: Final = _LINE + "internal error"
_LINES: Final = {
    "none": "no such credential",
    "unbound": f"the credential is not bound to {HOST}",
    "form": "the credential is not an address and an app password",
    "keyring": "the keyring did not give the credential",
    "limit": "more than 60 sign-ins with this credential in 10 minutes; not tried",
    "cooldown": (
        "Gmail refused this app password less than 15 minutes ago; save a new one to try again"
    ),
    "resolve": f"{HOST} did not resolve",
    "address": f"{HOST} resolved to an address that is not allowed",
    "connect": f"could not connect to {HOST}",
    "broken": "the connection to Gmail broke",
    "sign_in": (
        "Gmail refused the app password: make a new one at Google's app-password page and save it"
    ),
    "too_much": "more than 8 MiB read from Gmail in this request; nothing returned",
    "no_mailbox": "Gmail has no such mailbox",
    "no_message": "no such message in All Mail (Gmail keeps Spam and Trash out of it)",
    "no_all": "Gmail's All Mail is hidden from IMAP in this account's settings",
    "no_extensions": "Gmail did not offer its IMAP extensions (X-GM-EXT-1)",
    "refused": "Gmail refused the request",
}


class _Stop(Exception):
    """A refusal or a failure the child reports with one fixed line and an exit code."""

    def __init__(self, code: int, key: str) -> None:
        super().__init__(key)
        self.code = code
        self.line = INTERNAL_LINE if key == "internal" else _LINE + _LINES[key]


class _TooMuch(Exception):
    pass


class _Refused(Exception):
    """Gmail answered NO to a command other than LOGIN."""


class _SignInRefused(Exception):
    """Gmail answered NO to LOGIN."""


@dataclass(frozen=True)
class Reply:
    code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class Credential:
    address: str
    password: str
    saved_at: float


# --- arguments --------------------------------------------------------------------------------


def _arguments(argv: Sequence[str]) -> tuple[str, str, str, dict[str, Any]]:
    """(state folder, profile, operation, args), by position. Anything else is internal."""
    if len(argv) != 7 or argv[0] != "--state" or argv[2] != "--profile" or argv[4] != "--":
        raise _Stop(INTERNAL, "internal")
    state, profile, operation, text = argv[1], argv[3], argv[5], argv[6]
    from lucille_host import ops, paths

    if not os.path.isabs(state) or not paths.valid_profile(profile) or operation not in OPERATIONS:
        raise _Stop(INTERNAL, "internal")
    if nesting.json_deeper(text):
        raise _Stop(INTERNAL, "internal")
    try:
        args = json.loads(text)
    except (ValueError, RecursionError):
        raise _Stop(INTERNAL, "internal") from None
    if not ops.args_valid(ops.OPERATIONS[operation], args):
        raise _Stop(INTERNAL, "internal")
    return state, profile, operation, args


# --- the credential ---------------------------------------------------------------------------

# (state folder, profile, credential name) -> (bound hosts, value, saved_at), or None.
CredentialReader = Callable[[str, str, str], "tuple[tuple[str, ...], str, float] | None"]


def _read_credential(
    state: str, profile: str, name: str
) -> tuple[tuple[str, ...], str, float] | None:
    from lucille_host import credentials

    found = credentials.CredentialStore(Path(state), profile).for_use(SERVICE, name)
    if found is None:
        return None
    saved, value = found
    return saved.hosts, value, saved.saved_at


def _credential(read: CredentialReader, state: str, profile: str, name: str) -> Credential:
    try:
        found = read(state, profile, name)
    except Exception:  # noqa: BLE001 - a keyring's error may hold any credential's value
        raise _Stop(CREDENTIAL, "keyring") from None
    if found is None:
        raise _Stop(CREDENTIAL, "none")
    hosts, value, saved_at = found
    if HOST not in hosts:
        raise _Stop(CREDENTIAL, "unbound")
    if not isinstance(value, str) or VALUE.fullmatch(value) is None:
        raise _Stop(CREDENTIAL, "form")
    address, password = value.split(" ")
    if len(address) > ADDRESS_MAX:
        raise _Stop(CREDENTIAL, "form")
    return Credential(address, password, float(saved_at))


# --- sign-ins in a row ------------------------------------------------------------------------

SIGNINS_FILE: Final = "gmail-signins.json"
_SIGNINS_LOCK: Final = ".gmail-signins.lock"


def _signins_load(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        doc = None if nesting.json_deeper(text) else json.loads(text)
    except (OSError, ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _signins_save(path: Path, doc: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class Signins:
    """The record of sign-ins per credential, in the state folder, under a lock of its own."""

    def __init__(self, state: str, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(state) / SIGNINS_FILE
        self.lock = Path(state) / _SIGNINS_LOCK
        self.clock = clock

    def admit(self, name: str, saved_at: float) -> None:
        """Count one sign-in, or refuse: the cooldown after a refusal (exit 7) or the limit."""
        from lucille_host import store

        now = self.clock()
        with store.file_lock(self.lock):
            doc = _signins_load(self.path)
            entry = doc.get(name)
            if not isinstance(entry, dict):
                entry = {}
            times = [
                t
                for t in entry.get("signins", [])
                if isinstance(t, (int, float)) and 0 <= now - t < SIGNIN_WINDOW_S
            ]
            refused = entry.get("refused_at")
            if (
                isinstance(refused, (int, float))
                and entry.get("saved_at") == saved_at
                and 0 <= now - refused < REFUSED_WAIT_S
            ):
                raise _Stop(SIGN_IN, "cooldown")
            if len(times) >= SIGNINS_MAX:
                raise _Stop(CAP, "limit")
            times.append(now)
            doc[name] = {**entry, "signins": times}
            _signins_save(self.path, doc)

    def refused(self, name: str, saved_at: float) -> None:
        from lucille_host import store

        with store.file_lock(self.lock):
            doc = _signins_load(self.path)
            entry = doc.get(name) if isinstance(doc.get(name), dict) else {}
            doc[name] = {**entry, "refused_at": self.clock(), "saved_at": saved_at}
            _signins_save(self.path, doc)


# --- the connection ---------------------------------------------------------------------------


def _vetted(resolve: Callable[[str, int], list[str]]) -> list[str]:
    try:
        addresses = resolve(HOST, PORT)
    except (OSError, UnicodeError):
        raise _Stop(ADDRESS, "resolve") from None
    if not addresses:
        raise _Stop(ADDRESS, "resolve")
    if not all(proxy.address_allowed(address) for address in addresses):
        raise _Stop(ADDRESS, "address")
    return addresses


def _connect(address: str) -> Any:
    return proxy.tls_connect(address, HOST, PORT)


def _connected(connect: Callable[[str], Any], addresses: list[str]) -> Any:
    for address in addresses:
        try:
            return connect(address)
        except OSError:
            continue
    raise _Stop(ADDRESS, "connect")


class _Counted:
    """The connection's file, counting every byte read from Gmail against :data:`READ_CAP`."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.count = 0

    def _add(self, data: bytes) -> bytes:
        self.count += len(data)
        if self.count > READ_CAP:
            raise _TooMuch
        return data

    def readline(self, limit: int) -> bytes:
        # Never more than one byte past what is left: at the cap it stops reading.
        return self._add(self._raw.readline(min(limit, READ_CAP - self.count + 1)))

    def read(self, size: int) -> bytes:
        # A literal Gmail announces past what is left is not read at all.
        if size < 0 or size > READ_CAP - self.count:
            raise _TooMuch
        return self._add(self._raw.read(size))

    def close(self) -> None:
        self._raw.close()


class _Imap(imaplib.IMAP4):
    """``imaplib``'s client on a socket the child connected itself, to a vetted address."""

    def __init__(self, sock: Any) -> None:
        self._given = sock
        super().__init__(HOST, PORT, timeout=proxy.NET_TIMEOUT_S)

    def open(self, host: str = "", port: int = PORT, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.sock = self._given
        self._counted = _Counted(self.sock.makefile("rb"))
        # Every read goes through read and readline below. imaplib closes its file in shutdown:
        # ``file`` up to Python 3.13, ``_file`` from 3.14, where ``file`` is a read-only property.
        self._file = self._counted
        if not isinstance(getattr(type(self), "file", None), property):
            self.file = self._counted
        # imaplib's transcript of what it sends and reads, which would hold the app password,
        # is never on, whatever imaplib.Debug says.
        self.debug = 0

    def read(self, size: int) -> bytes:
        return self._counted.read(size)

    def readline(self) -> bytes:
        """One line from Gmail; one longer than :data:`LINE_MAX` is a cap reached (exit 5)."""
        line = self._counted.readline(LINE_MAX + 1)
        if not line:
            raise self.abort("socket error: EOF")
        if len(line) > LINE_MAX:
            raise _TooMuch
        return line

    def command(self, name: str, *args: str) -> list[Any]:
        """One command; its untagged data. ``NO`` raises :class:`_Refused`; ``BAD`` is imaplib's
        own error, which the caller reads as the child's fault."""
        typ, data = self._simple_command(name, *args)
        if typ != "OK":
            raise _Refused
        return list(self._untagged_response(typ, data, name)[1])

    def sign_in(self, address: str, password: str) -> None:
        typ, _ = self._simple_command("LOGIN", _quoted(address), _quoted(password))
        if typ != "OK":
            raise _SignInRefused
        self.state = "AUTH"

    def examine(self, mailbox: str) -> None:
        # As imaplib's own select(readonly=True): it then takes Gmail's READ-ONLY answer as asked.
        self.is_readonly = True
        typ, _ = self._simple_command("EXAMINE", _quoted(mutf7_encode(mailbox)))
        if typ != "OK":
            raise _Refused
        self.state = "SELECTED"

    def uid_command(self, command: str, *args: str, literal: bytes | None = None) -> list[Any]:
        self.literal = literal
        typ, data = self._simple_command("UID", command, *args)
        if typ != "OK":
            raise _Refused
        name = "FETCH" if command == "FETCH" else command
        return list(self._untagged_response(typ, data, name)[1])


def _quoted(text: str) -> str:
    """An IMAP quoted string: each ``\\`` and ``"`` escaped with a ``\\`` (RFC 3501 4.3)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- modified UTF-7 (RFC 3501 5.1.3) ------------------------------------------------------------


def mutf7_encode(text: str) -> str:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            raw = "".join(run).encode("utf-16-be")
            out.append("&" + _b64(raw) + "-")
            run.clear()

    for char in text:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


def _b64(raw: bytes) -> str:
    return binascii.b2a_base64(raw, newline=False).decode("ascii").rstrip("=").replace("/", ",")


def mutf7_decode(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = text.find("-", i + 1)
        if end < 0:
            out.append(text[i:])
            break
        chunk = text[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
            try:
                out.append(binascii.a2b_base64(padded).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(text[i : end + 1])
        i = end + 1
    return "".join(out)


# --- reading what Gmail returned --------------------------------------------------------------

_LIST: Final = re.compile(
    rb"^\((?P<flags>[^)]*)\) (?P<delim>NIL|\"(?:[^\"\\]|\\.)*\") (?P<name>.*)$"
)


def _unquote(raw: bytes) -> str:
    text = raw.decode("ascii", "replace")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def _mailboxes(data: Iterable[Any]) -> list[dict[str, Any]]:
    found = []
    for item in data:
        if isinstance(item, tuple):
            head, literal = item[0], item[1]
            match = _LIST.match(head.rsplit(b" ", 1)[0] + b" x")
            name = literal.decode("ascii", "replace") if match else None
        elif isinstance(item, bytes):
            match = _LIST.match(item)
            name = _unquote(match.group("name")) if match else None
        else:
            continue
        if match is None or name is None:
            continue
        flags = match.group("flags").decode("ascii", "replace").split()
        special = next((f for f in flags if f in SPECIAL_USE), None)
        found.append({"name": mutf7_decode(name), "special_use": special})
    return found


_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_INTERNAL: Final = re.compile(
    r"^ ?(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$"
)


def _rfc3339(internal: str) -> str | None:
    match = _INTERNAL.match(internal)
    if match is None:
        return None
    day, month, year, hh, mm, ss, sign, zh, zm = match.groups()
    if month.title() not in _MONTHS:
        return None
    offset = datetime.timedelta(hours=int(zh), minutes=int(zm))
    zone = datetime.timezone(offset if sign == "+" else -offset)
    try:
        when = datetime.datetime(
            int(year),
            _MONTHS.index(month.title()) + 1,
            int(day),
            int(hh),
            int(mm),
            int(ss),
            tzinfo=zone,
        )
    except ValueError:
        return None
    return when.isoformat()


def _imap_date(date: str) -> str:
    """``YYYY-MM-DD`` as IMAP's date, ``18-Nov-2026``, whatever the locale."""
    day = datetime.date.fromisoformat(date)
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


_META: Final = {
    "uid": re.compile(rb"\bUID (\d+)"),
    "msgid": re.compile(rb"\bX-GM-MSGID (\d+)"),
    "thrid": re.compile(rb"\bX-GM-THRID (\d+)"),
    "size": re.compile(rb"\bRFC822\.SIZE (\d+)"),
    "date": re.compile(rb'\bINTERNALDATE "([^"]*)"'),
    "flags": re.compile(rb"\bFLAGS \(([^)]*)\)"),
}


def _fetched(data: Iterable[Any]) -> list[tuple[dict[str, Any], bytes]]:
    """Each fetched message's items and its literal, from ``imaplib``'s FETCH data."""
    items = list(data)
    found: list[tuple[dict[str, Any], bytes]] = []
    for index, item in enumerate(items):
        if not isinstance(item, tuple):
            continue
        meta = item[0]
        following = items[index + 1] if index + 1 < len(items) else b""
        if isinstance(following, bytes):
            meta = meta + b" " + following
        values: dict[str, Any] = {}
        for key, pattern in _META.items():
            match = pattern.search(meta)
            values[key] = match.group(1).decode("ascii", "replace") if match else None
        found.append((values, item[1]))
    return found


def _cut(text: str, most: int) -> tuple[str, bool]:
    """``text`` cut to at most ``most`` bytes of UTF-8, on a character's boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= most:
        return text, False
    return raw[:most].decode("utf-8", "ignore"), True


def _header(message: Any, name: str) -> str | None:
    """A header's value, whole: it is cut only after the scrub (:func:`_bounded`)."""
    try:
        value = message.get(name)
    except Exception:  # noqa: BLE001 - a malformed header reads as none
        return None
    if value is None:
        return None
    return _CONTROLS.sub(" ", str(value))


_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# --- HTML to text ------------------------------------------------------------------------------

_VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK: Final = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_UNREAD: Final = frozenset({"head", "noscript", "script", "style", "template", "title"})
_HIDING_STYLE: Final = re.compile(
    r"(display:none|visibility:hidden|font-size:0(?![.0-9]*[1-9])|opacity:0(?![.0-9]*[1-9]))"
)


def _hides(attrs: list[tuple[str, str | None]]) -> bool:
    for key, value in attrs:
        key = key.lower()
        if key == "hidden":
            return True
        if key == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
        if key == "style" and _HIDING_STYLE.search(re.sub(r"\s+", "", (value or "").lower())):
            return True
    return False


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[tuple[str, bool]] = []

    def _hidden(self) -> bool:
        return any(hidden for _, hidden in self.open)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag in _BLOCK and not self._hidden():
                self.out.append("\n")
            return
        self.open.append((tag, tag in _UNREAD or _hides(attrs)))
        if tag in _BLOCK and not self._hidden():
            self.out.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK and not self._hidden():
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                del self.open[index:]
                break
        if tag in _BLOCK and not self._hidden():
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden():
            self.out.append(data)


def html_text(markup: str) -> str:
    """The text of ``markup`` as a reader sees it, where the markup hides text plainly: no
    script, style or head, and nothing under an element ``hidden``, ``aria-hidden="true"``, or
    styled ``display:none``, ``visibility:hidden``, ``font-size:0`` or ``opacity:0``. No link is
    followed and no image fetched."""
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup gives what was read of it
        pass
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.out).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return _CONTROLS.sub(" ", text)


# --- the message ------------------------------------------------------------------------------


def _part_text(part: Any) -> str:
    try:
        text = str(part.get_content())
    except (LookupError, ValueError, UnicodeError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _message(raw: bytes) -> dict[str, Any]:
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    text: str | None = None
    text_from: str | None = None
    if isinstance(message, EmailMessage):
        plain = message.get_body(preferencelist=("plain",))
        if plain is not None:
            text, text_from = _CONTROLS.sub(" ", _part_text(plain)), "plain"
        else:
            markup = message.get_body(preferencelist=("html",))
            if markup is not None:
                text, text_from = html_text(_part_text(markup)), "html"
    attachments = []
    if isinstance(message, EmailMessage):
        try:
            parts = list(message.iter_attachments())
        except Exception:  # noqa: BLE001 - a message cut at 4 MiB may end mid-part
            parts = []
        for part in parts:
            try:
                payload = part.get_payload(decode=True) or b""
                name = part.get_filename()
            except Exception:  # noqa: BLE001
                continue
            attachments.append(
                {
                    "filename": None if name is None else _CONTROLS.sub(" ", name),
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                }
            )
    return {
        "from": _header(message, "From"),
        "to": _header(message, "To"),
        "cc": _header(message, "Cc"),
        "subject": _header(message, "Subject"),
        "message_id": _header(message, "Message-ID"),
        "in_reply_to": _header(message, "In-Reply-To"),
        "text": text,
        "text_from": text_from,
        "text_truncated": False,
        "attachments": attachments,
        "attachments_truncated": False,
    }


# --- the scrub, the cuts and the fit ----------------------------------------------------------

# Each field's limit, in bytes of UTF-8, wherever it occurs in a document.
_CUTS: Final = {
    "from": HEADER_CAP,
    "to": RECIPIENTS_CAP,
    "cc": RECIPIENTS_CAP,
    "subject": HEADER_CAP,
    "message_id": HEADER_CAP,
    "in_reply_to": HEADER_CAP,
    "content_type": HEADER_CAP,
    "filename": FILENAME_CAP,
    "name": HEADER_CAP,
    "text": TEXT_CAP,
}
_SURROGATE: Final = re.compile("[\ud800-\udfff]")


class _Scrub:
    """Every form of the app password this child replaces in what it writes."""

    def __init__(self, password: str) -> None:
        # Its 16 letters in any case, with up to three spaces or hyphens between any two.
        self.pattern = re.compile(
            "[\\s-]{0,3}".join(re.escape(letter) for letter in password), re.IGNORECASE
        )
        self.forms = [form.decode("ascii") for form in _secrets(password)]
        # The fields where a private-key block began and did not end.
        self.opened: set[str | None] = set()

    def text(self, value: str, key: str | None = None) -> str:
        # The host's own redaction, string by string: a private-key block that does not end in
        # this string is taken to its end, never past it, so the document stays JSON and the host's
        # pass over the whole stream finds no block left to run to its end.
        value, to_end = redact_to_end(value)
        if to_end:
            self.opened.add(key)
        value = self.pattern.sub(TOKEN_MARK, value)
        for form in self.forms:
            value = value.replace(form, TOKEN_MARK)
        # A lone surrogate (an undecodable byte) cannot be written as UTF-8.
        return _SURROGATE.sub("\ufffd", value)

    def walk(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            return self.text(value, key)
        if isinstance(value, list):
            return [self.walk(item, key) for item in value]
        if isinstance(value, dict):
            return {name: self.walk(item, name) for name, item in value.items()}
        return value


def _close_blocks(document: dict[str, Any], opened: set[str | None]) -> None:
    """A read's text, where a private-key block took part of it: a block that took the end of
    the text sets ``text_truncated``; and one that began in another field (a subject, say) and did
    not end there may go on in the text, so the text is taken up to the first end of a block, or
    wholly when it has none, and ``text_truncated`` is set then too."""
    text = document.get("text")
    if not isinstance(text, str) or "text_truncated" not in document:
        return
    if "text" in opened:
        document["text_truncated"] = True
    if opened - {"text"}:
        end = key_block_end(text)
        if end is None:
            document["text"] = PRIVATE_KEY_MARK
            document["text_truncated"] = True
        else:
            document["text"] = PRIVATE_KEY_MARK + text[end:]


def _cut_fields(value: Any) -> None:
    """Each field of :data:`_CUTS` cut to its limit, in place; ``text`` sets ``text_truncated``."""
    if isinstance(value, list):
        for item in value:
            _cut_fields(item)
    elif isinstance(value, dict):
        for key in list(value):
            item = value[key]
            if isinstance(item, str) and key in _CUTS:
                value[key], cut = _cut(item, _CUTS[key])
                if key == "text" and cut:
                    value["text_truncated"] = True
            else:
                _cut_fields(item)


def _bounded(document: dict[str, Any], password: str) -> bytes:
    """What the child writes for ``document``: scrubbed first, so no cut can leave a part of the
    password, then cut field by field, then fit to the stream, then scrubbed once more as
    bytes."""
    scrubber = _Scrub(password)
    cleaned = scrubber.walk(document)
    _close_blocks(cleaned, scrubber.opened)
    _cut_fields(cleaned)
    return scrub(_fit(cleaned).encode("utf-8"), password)


def _stream_size(stdout: str) -> int:
    """Rule ``stream-size`` of a standard output the host reads as ``stdout``: its JSON string,
    quotes included, every non-ASCII character escaped (``envelope.size``)."""
    return len(json.dumps(stdout, ensure_ascii=True, separators=(",", ":")))


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _piece_cost(value: Any) -> int:
    """What ``value``, written inside the document, adds to its stream size."""
    return _stream_size(_encoded(value)) - 2


def _fit(document: dict[str, Any]) -> str:
    """The document as JSON whose stream size is at most :data:`DOCUMENT_CAP`.

    Past it, what is cut, in order: attachments, from the last, while the list takes more than
    :data:`ATTACHMENTS_SHARE` (``attachments_truncated``); the end of ``text``
    (``text_truncated``); the other attachments; search results, from the oldest, and mailboxes,
    from the last (``truncated``). A quote, the costliest character, takes 4 of the stream for its
    byte, so a read's headers at their limits (12 KiB) take at most 48 KiB, and with the share
    the attachments keep it fits without the third step, which is there so that the loop always
    ends. One search result at its limits takes at most 24 KiB, one mailbox's name 4 KiB.
    """
    out = _encoded(document)
    while (excess := _stream_size(out) - DOCUMENT_CAP) > 0:
        _shrink(document, excess)
        out = _encoded(document)
    return out


def _shrink(document: dict[str, Any], excess: int) -> None:
    attachments = document.get("attachments")
    if attachments and (over := _piece_cost(attachments) - ATTACHMENTS_SHARE) > 0:
        removed = 0
        while attachments and removed < min(over, excess):
            removed += _piece_cost(attachments.pop()) + 1
        document["attachments_truncated"] = True
        return
    text = document.get("text")
    if text:
        end, removed = len(text), 0
        while end > 0 and removed < excess:
            end -= 1
            # One character's own cost: its piece less the two quotes, each 2 in the stream.
            removed += _piece_cost(text[end]) - 4
        document["text"] = text[:end]
        document["text_truncated"] = True
        return
    for key, flag in (
        ("attachments", "attachments_truncated"),
        ("messages", "truncated"),
        ("mailboxes", "truncated"),
    ):
        items = document.get(key)
        if items:
            removed = 0
            while items and removed < excess:
                removed += _piece_cost(items.pop()) + 1
            document[flag] = True
            return
    raise _Stop(INTERNAL, "internal")


# --- the operations ---------------------------------------------------------------------------


def _capable(imap: _Imap) -> None:
    data = imap.command("CAPABILITY")
    words = b" ".join(d for d in data if isinstance(d, bytes)).upper().split()
    if b"X-GM-EXT-1" not in words:
        raise _Stop(REFUSED, "no_extensions")


def _search(imap: _Imap, args: Mapping[str, Any]) -> dict[str, Any]:
    _capable(imap)
    mailbox = args.get("mailbox", "INBOX")
    try:
        imap.examine(mailbox)
    except _Refused:
        raise _Stop(REFUSED, "no_mailbox") from None
    plain: list[str] = []
    if "since" in args:
        plain += ["SINCE", _imap_date(args["since"])]
    if "before" in args:
        plain += ["BEFORE", _imap_date(args["before"])]
    if args.get("unseen"):
        plain.append("UNSEEN")
    texts = [(key, args[key]) for key in ("query", "from", "to", "subject") if key in args]
    keyword = {"query": "X-GM-RAW", "from": "FROM", "to": "TO", "subject": "SUBJECT"}
    found: set[int] | None = None
    searches = [(keyword[key], value) for key, value in texts] or [(None, None)]
    for word, value in searches:
        if word is None:
            data = imap.uid_command("SEARCH", *(plain or ["ALL"]))
        else:
            data = imap.uid_command(
                "SEARCH", "CHARSET", "UTF-8", *plain, word, literal=value.encode("utf-8")
            )
        uids = {int(u) for d in data if isinstance(d, bytes) for u in d.split() if u.isdigit()}
        found = uids if found is None else found & uids
    ordered = sorted(found or set(), reverse=True)
    limit = int(args.get("limit", LIMIT_DEFAULT))
    chosen = ordered[:limit]
    messages = []
    if chosen:
        data = imap.uid_command("FETCH", ",".join(str(u) for u in chosen), SEARCH_ITEMS)
        by_uid = {}
        for values, literal in _fetched(data):
            headers = email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(
                literal
            )
            by_uid[values["uid"]] = {
                "id": values["msgid"],
                "thread": values["thrid"],
                "date": _rfc3339(values["date"] or ""),
                "from": _header(headers, "From"),
                "to": _header(headers, "To"),
                "subject": _header(headers, "Subject"),
                "size": int(values["size"]) if values["size"] else None,
                "unseen": "\\Seen" not in (values["flags"] or "").split(),
            }
        messages = [by_uid[str(u)] for u in chosen if str(u) in by_uid]
    return {
        "mailbox": mailbox,
        "total": len(ordered),
        "messages": messages,
        "truncated": len(messages) < len(ordered),
    }


def _all_mail(imap: _Imap) -> str:
    for box in _mailboxes(imap.command("LIST", '""', '"*"')):
        if box["special_use"] == "\\All":
            return str(box["name"])
    raise _Stop(REFUSED, "no_all")


def _read(imap: _Imap, args: Mapping[str, Any]) -> dict[str, Any]:
    _capable(imap)
    mailbox = _all_mail(imap)
    try:
        imap.examine(mailbox)
    except _Refused:
        raise _Stop(REFUSED, "no_all") from None
    data = imap.uid_command("SEARCH", "X-GM-MSGID", str(int(args["id"])))
    uids = [u for d in data if isinstance(d, bytes) for u in d.split() if u.isdigit()]
    if not uids:
        raise _Stop(REFUSED, "no_message")
    fetched = _fetched(imap.uid_command("FETCH", uids[0].decode("ascii"), READ_ITEMS))
    if not fetched:
        raise _Stop(REFUSED, "no_message")
    values, raw = fetched[0]
    size = int(values["size"]) if values["size"] else len(raw)
    return {
        "id": values["msgid"] or str(int(args["id"])),
        "thread": values["thrid"],
        "date": _rfc3339(values["date"] or ""),
        **_message(raw),
        "message_truncated": size > MESSAGE_FETCH,
    }


def _mailboxes_document(imap: _Imap) -> dict[str, Any]:
    boxes = _mailboxes(imap.command("LIST", '""', '"*"'))
    return {"mailboxes": boxes[:MAILBOXES_MAX], "truncated": len(boxes) > MAILBOXES_MAX}


def _operate(imap: _Imap, operation: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if operation == "gmail.check":
        return {"ok": True}
    if operation == "gmail.mailboxes":
        return _mailboxes_document(imap)
    if operation == "gmail.search":
        return _search(imap, args)
    return _read(imap, args)


# --- the scrub and the whole ------------------------------------------------------------------


def _secrets(password: str) -> list[bytes]:
    spaced = " ".join(password[i : i + 4] for i in range(0, len(password), 4))
    forms: list[bytes] = []
    for secret in (password, spaced):
        forms += proxy.secret_forms(secret)
    return sorted(set(forms), key=len, reverse=True)


def scrub(data: bytes, password: str) -> bytes:
    mark = TOKEN_MARK.encode("ascii")
    for form in _secrets(password):
        data = data.replace(form, mark)
    return data


def main(
    argv: Sequence[str],
    *,
    read_credential: CredentialReader | None = None,
    resolve: Callable[[str, int], list[str]] | None = None,
    connect: Callable[[str], Any] | None = None,
    clock: Callable[[], float] = time.time,
) -> Reply:
    """What the child writes for ``argv``; the keyring, resolver, connection and clock can be
    given (tests)."""
    password: str | None = None
    try:
        state, profile, operation, args = _arguments(argv)
        name = args["credential"]
        credential = _credential(read_credential or _read_credential, state, profile, name)
        password = credential.password
        signins = Signins(state, clock)
        signins.admit(name, credential.saved_at)
        sock = _connected(connect or _connect, _vetted(resolve or proxy.resolve))
        try:
            document = _session(sock, credential, operation, args, signins, name)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return Reply(OK, _bounded(document, password), b"")
    except _Stop as stop:
        err = (stop.line + "\n").encode("utf-8")
        return Reply(stop.code, b"", err if password is None else scrub(err, password))


def _session(
    sock: Any,
    credential: Credential,
    operation: str,
    args: Mapping[str, Any],
    signins: Signins,
    name: str,
) -> dict[str, Any]:
    try:
        imap = _Imap(sock)
        try:
            imap.sign_in(credential.address, credential.password)
        except _SignInRefused:
            signins.refused(name, credential.saved_at)
            raise _Stop(SIGN_IN, "sign_in") from None
        try:
            document = _operate(imap, operation, args)
        except _Refused:
            raise _Stop(REFUSED, "refused") from None
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError, _TooMuch):
            pass
        return document
    except _TooMuch:
        raise _Stop(CAP, "too_much") from None
    except imaplib.IMAP4.abort:
        raise _Stop(ADDRESS, "broken") from None
    except imaplib.IMAP4.error:
        raise _Stop(INTERNAL, "internal") from None
    except (OSError, EOFError):
        raise _Stop(ADDRESS, "broken") from None


def run() -> NoReturn:
    """The child's entry point: streams set aside, a top-level handler, one write of each stream,
    ``os._exit``."""
    try:
        out, err = proxy.set_aside_streams()
    except OSError:
        os._exit(INTERNAL)
    try:
        reply = main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 - its text could hold what the account holds
        reply = Reply(INTERNAL, b"", (INTERNAL_LINE + "\n").encode("ascii"))
    proxy.write_all(out, reply.stdout)
    proxy.write_all(err, reply.stderr)
    os._exit(reply.code)


if __name__ == "__main__":
    run()
