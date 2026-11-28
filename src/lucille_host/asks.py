"""Lucille asks, the user types it here: ``credential.ask``'s child, and the asks it leaves open.

decisions/0003, "Entry". Lucille may ask the host to collect a credential: a request
``credential.ask {service, name, reason?}``, which carries no value. Its child, this module run as
``-m lucille_host.asks``, writes the ask to a file in the profile's state folder and waits up to
:data:`WAIT_S` for the user's answer, given on this computer with ``lucille-host credential
answer`` (in a terminal: Capsule has no dialog for an ask yet). It then writes one JSON document,
``{"answer": "saved"}`` or ``{"answer": "declined"}``, and exits 0. That is all Lucille learns.

- **The child never reads or writes a credential.** It does not import ``credentials``, holds no
  value and connects to nothing. The value is typed into ``credential answer``, the user's own
  command, which saves it the way ``credential set`` does.
- **The hosts are the service's template's** (``services.py``), never a request's: the ask holds
  them, and ``credential answer`` binds the credential to them and to no other. A service with no
  template cannot be asked for; the schema holds the services that have one.
- **Lucille's words are kept apart.** ``reason`` is shown last, after the service, name and
  hosts, which the host lays out itself, cut into lines that each start "Lucille says:"
  (:func:`reason_lines`). Rule ``credential-ask-args`` keeps out of it every character that would
  move, hide or blank out text on a screen (:data:`UNSHOWABLE`) and every run of spaces
  (:data:`SPACES`).
- **Declining is the default.** An ask nobody answers within :data:`WAIT_S` is declined. So is
  an ask made while another is open, and an ask made less than :data:`GAP_S` after any ask
  ended, without being shown: Lucille cannot nag.
- While the child waits, ``run`` runs no other request: it runs one at a time.

The file is ``credential-asks.json`` (:data:`FILE`), written whole and renamed over, under a lock
of its own, ``.credential-asks.lock``. It holds no value. Anything that cannot be read as this
module writes it is read as no ask at all.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from lucille_host import nesting, services

__all__ = [
    "ANSWERS",
    "FILE",
    "GAP_S",
    "ID",
    "LINE_WIDTH",
    "LUCILLE_SAYS",
    "REASON_MAX",
    "SPACES",
    "UNSHOWABLE",
    "WAIT_S",
    "Ask",
    "AskGone",
    "Asks",
    "main",
    "reason_lines",
    "reason_showable",
]

FILE: Final = "credential-asks.json"
_LOCK: Final = ".credential-asks.lock"
VERSION: Final = 1
# How long an ask waits for the user. The catalog's time limit for credential.ask is longer, so the
# child answers before the host would stop it.
WAIT_S: Final = 240
# After any ask ends, how long a new ask, for any credential, is declined without being shown.
GAP_S: Final = 600
# How often the child looks for the answer.
POLL_S: Final = 0.25
# An ask's id: 16 hexadecimal digits, from the system's random source.
ID: Final = re.compile(r"[0-9a-f]{16}")
ANSWERS: Final = ("saved", "declined")
# The most characters of ``reason``, as the catalog's schema says.
REASON_MAX: Final = 300
# What may not be in ``reason``, so that nothing of Lucille's words can move, hide or blank out
# text where the host shows them (rule credential-ask-args, spec/PROTOCOL.md section 10):
# - C0 and C1 controls and DEL, line breaks and tabs among them (U+0000 to U+001F, U+007F to U+009F);
# - the line and paragraph separators (U+2028, U+2029);
# - the characters that reorder text (U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to U+2069);
# - white space other than U+0020 (U+00A0, U+1680, U+2000 to U+200A, U+202F, U+205F, U+3000);
# - characters of no width (U+00AD, U+034F, U+180B to U+180F, U+200B to U+200D, U+2060 to U+2065,
#   U+206A to U+206F, U+FE00 to U+FE0F, U+FEFF, U+FFF9 to U+FFFB, U+1D173 to U+1D17A, U+E0000 to
#   U+E007F, U+E0100 to U+E01EF);
# - characters drawn blank (U+115F, U+1160, U+2800, U+3164, U+FFA0).
UNSHOWABLE: Final = re.compile(
    "[\\x00-\\x1f\\x7f-\\x9f\\u00a0\\u00ad\\u034f\\u061c\\u115f\\u1160\\u1680\\u180b-\\u180f"
    "\\u2000-\\u200f\\u2028-\\u202f\\u205f-\\u206f\\u2800\\u3000\\u3164\\ufe00-\\ufe0f\\ufeff"
    "\\uffa0\\ufff9-\\ufffb\\U0001d173-\\U0001d17a\\U000e0000-\\U000e007f\\U000e0100-\\U000e01ef]"
)
# U+0020 at the start or the end of ``reason``, or two in a row: a run of spaces can push the rest
# of Lucille's words to where a line of the host's starts.
SPACES: Final = re.compile("^ | $|  ")
# The most characters of Lucille's words on one line where the host shows them; each line starts
# with LUCILLE_SAYS, so none reads as one of the host's.
LINE_WIDTH: Final = 64
LUCILLE_SAYS: Final = "Lucille says: "

OK: Final = 0
INTERNAL: Final = 6
INTERNAL_LINE: Final = "lucille-host ask: internal error"


def reason_showable(reason: str) -> bool:
    """Whether ``reason`` holds no character :data:`UNSHOWABLE` names, no space at its start or
    end and no two in a row (:data:`SPACES`), and is not empty."""
    return bool(reason) and UNSHOWABLE.search(reason) is None and SPACES.search(reason) is None


def _columns(text: str) -> int:
    """How many columns ``text`` takes on a terminal, at most: two for a wide character (East Asian
    width W or F), one for any other."""
    import unicodedata  # noqa: PLC0415 - only the command line shows an ask

    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def reason_lines(reason: str) -> list[str]:
    """Lucille's words as the host shows them: cut at its spaces into lines of at most
    :data:`LINE_WIDTH` columns (a word wider than that is cut where it must be), each line marked
    :data:`LUCILLE_SAYS`, so that no line of them reads as one of the host's. Each line, mark
    included, fits in 78 columns, so a terminal that wide never wraps one itself."""
    lines: list[str] = []
    line = ""
    for word in reason.split(" "):
        while _columns(word) > LINE_WIDTH:
            if line:
                lines.append(line)
                line = ""
            cut = len(word)
            while _columns(word[:cut]) > LINE_WIDTH:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        if not line:
            line = word
        elif _columns(line) + 1 + _columns(word) <= LINE_WIDTH:
            line = f"{line} {word}"
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return [LUCILLE_SAYS + each for each in lines]


@dataclass(frozen=True)
class Ask:
    """One open ask, as the file holds it. Never a value."""

    id: str
    service: str
    name: str
    hosts: tuple[str, ...]
    reason: str | None
    asked_at: float
    until: float
    answer: str | None = None

    def fields(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "name": self.name,
            "hosts": list(self.hosts),
            "reason": self.reason,
            "asked_at": self.asked_at,
            "until": self.until,
            "answer": self.answer,
        }


class AskGone(LookupError):
    """No open ask has that id: none was made, it was answered, or it lapsed."""


def _ask(entry: object) -> Ask | None:
    """An entry of the file as an :class:`Ask`, or None for one this module would not write."""
    if not isinstance(entry, dict):
        return None
    try:
        ask = Ask(
            id=entry["id"],
            service=entry["service"],
            name=entry["name"],
            hosts=tuple(entry["hosts"]),
            reason=entry["reason"],
            asked_at=float(entry["asked_at"]),
            until=float(entry["until"]),
            answer=entry["answer"],
        )
    except (KeyError, TypeError, ValueError):
        return None
    template = services.SERVICES.get(ask.service) if isinstance(ask.service, str) else None
    if (
        not isinstance(ask.id, str)
        or ID.fullmatch(ask.id) is None
        or template is None
        or ask.hosts != template.hosts
        or not isinstance(ask.name, str)
        or services.NAME.fullmatch(ask.name) is None
        or not (
            ask.reason is None
            or (
                isinstance(ask.reason, str)
                and len(ask.reason) <= REASON_MAX
                and reason_showable(ask.reason)
            )
        )
        or ask.answer not in (None, *ANSWERS)
    ):
        return None
    return ask


class Asks:
    """The profile's open asks, in its state folder, under a lock of their own."""

    def __init__(
        self,
        state: Path | str,
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self.path = Path(state) / FILE
        self.lock = Path(state) / _LOCK
        self.clock = clock
        self.new_id = new_id

    # --- the file ---------------------------------------------------------------------------

    def _load(self) -> tuple[dict[str, Ask], dict[str, float]]:
        try:
            text = self.path.read_text(encoding="utf-8")
            doc = None if nesting.json_deeper(text) else json.loads(text)
        except (OSError, ValueError, RecursionError):
            doc = None
        if not isinstance(doc, dict) or doc.get("v") != VERSION:
            return {}, {}
        raw_open = doc.get("open") if isinstance(doc.get("open"), dict) else {}
        raw_last = doc.get("last") if isinstance(doc.get("last"), dict) else {}
        open_asks = {}
        for key, entry in raw_open.items():
            ask = _ask(entry)
            if ask is not None and ask.id == key:
                open_asks[key] = ask
        last = {
            key: float(value)
            for key, value in raw_last.items()
            if isinstance(key, str) and isinstance(value, int | float)
        }
        return open_asks, last

    def _save(self, open_asks: dict[str, Ask], last: dict[str, float]) -> None:
        doc = {
            "v": VERSION,
            "open": {key: ask.fields() for key, ask in open_asks.items()},
            "last": last,
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _locked(self) -> Any:
        from lucille_host import store  # noqa: PLC0415 - the lock only, where one is taken

        return store.file_lock(self.lock)

    def _tidied(
        self, open_asks: dict[str, Ask], last: dict[str, float], now: float
    ) -> tuple[dict[str, Ask], dict[str, float]]:
        """The asks still waiting for an answer, and when each credential's last ask ended, with
        a lapsed ask counted as ended when it lapsed and old entries of ``last`` dropped."""
        kept = {}
        for key, ask in open_asks.items():
            if ask.until <= now and ask.answer is None:
                credential = f"{ask.service}/{ask.name}"
                last[credential] = max(last.get(credential, 0.0), ask.until)
            else:
                kept[key] = ask
        last = {key: when for key, when in last.items() if 0 <= now - when < GAP_S}
        return kept, last

    # --- the child's side -------------------------------------------------------------------

    def make(self, service: str, name: str, reason: str | None) -> Ask | None:
        """Open an ask, or None when it is declined without being shown: another ask is open, or
        an ask, for this credential or any other, ended less than :data:`GAP_S` ago. So Lucille
        cannot nag by asking under one name after another."""
        template = services.SERVICES[service]
        now = self.clock()
        with self._locked():
            open_asks, last = self._tidied(*self._load(), now)
            if any(ask.answer is None for ask in open_asks.values()):
                return None
            if last:
                return None
            ask = Ask(
                id=self.new_id(),
                service=service,
                name=name,
                hosts=template.hosts,
                reason=reason,
                asked_at=now,
                until=now + WAIT_S,
            )
            open_asks[ask.id] = ask
            self._save(open_asks, last)
        return ask

    def collect(self, ask: Ask) -> str | None:
        """The answer to ``ask`` once it has one, closing it; ``declined`` once it lapsed; None
        while it waits."""
        now = self.clock()
        with self._locked():
            open_asks, last = self._load()
            held = open_asks.get(ask.id)
            if held is not None and held.answer is None and now < held.until:
                return None
            open_asks.pop(ask.id, None)
            last[f"{ask.service}/{ask.name}"] = now
            open_asks, last = self._tidied(open_asks, last, now)
            self._save(open_asks, last)
        return held.answer if held is not None and held.answer is not None else "declined"

    # --- the user's side --------------------------------------------------------------------

    def waiting(self) -> list[Ask]:
        """The asks waiting for an answer now, oldest first."""
        now = self.clock()
        with self._locked():
            open_asks, _ = self._tidied(*self._load(), now)
        waiting = [ask for ask in open_asks.values() if ask.answer is None]
        return sorted(waiting, key=lambda ask: ask.asked_at)

    def get(self, ask_id: str) -> Ask:
        """The ask with that id, while it waits for an answer; :class:`AskGone` otherwise."""
        for ask in self.waiting():
            if ask.id == ask_id:
                return ask
        raise AskGone(ask_id)

    def answer(self, ask_id: str, answer: str) -> Ask:
        """Record the user's answer, for the child to collect; :class:`AskGone` when the ask no
        longer waits for one."""
        if answer not in ANSWERS:
            raise ValueError(answer)
        now = self.clock()
        with self._locked():
            open_asks, last = self._load()
            ask = open_asks.get(ask_id)
            if ask is None or ask.answer is not None or ask.until <= now:
                raise AskGone(ask_id)
            answered = dataclasses.replace(ask, answer=answer)
            open_asks[ask_id] = answered
            self._save(open_asks, last)
        return answered


# --- the child ------------------------------------------------------------------------------


class _Internal(Exception):
    pass


def _arguments(argv: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """(state folder, args), by position: ``--state S --profile P -- <args>``. Anything else is
    internal. The args are checked against the catalog's schema and rule again."""
    if len(argv) != 6 or argv[0] != "--state" or argv[2] != "--profile" or argv[4] != "--":
        raise _Internal
    state, profile, text = argv[1], argv[3], argv[5]
    from lucille_host import ops, paths  # noqa: PLC0415 - after the shape of argv is known

    if not os.path.isabs(state) or not paths.valid_profile(profile):
        raise _Internal
    if nesting.json_deeper(text):
        raise _Internal
    try:
        args = json.loads(text)
    except (ValueError, RecursionError):
        raise _Internal from None
    if not ops.args_valid(ops.OPERATIONS["credential.ask"], args):
        raise _Internal
    return state, args


def main(
    argv: Sequence[str],
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, bytes, bytes]:
    """(exit code, standard output, standard error) for ``argv``; the clock and the sleep can be
    given (tests)."""
    try:
        state, args = _arguments(argv)
        asks = Asks(state, clock)
        ask = asks.make(args["service"], args["name"], args.get("reason"))
        answer = "declined"
        if ask is not None:
            while True:
                collected = asks.collect(ask)
                if collected is not None:
                    answer = collected
                    break
                sleep(POLL_S)
    except _Internal:
        return INTERNAL, b"", (INTERNAL_LINE + "\n").encode("ascii")
    document = json.dumps({"answer": answer}, separators=(",", ":")).encode("ascii")
    return OK, document, b""


def _set_aside_streams() -> tuple[int, int]:
    """Copies of standard output and standard error for the reply alone, with descriptors 1 and 2
    pointed at the null device from here on, as ``proxy.py``'s child does: only what
    :func:`main` returns is written."""
    out = os.dup(1)
    err = os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
    finally:
        os.close(null)
    return out, err


def _write(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        except OSError:
            return
        view = view[written:]


def run() -> NoReturn:
    """The child's entry point: the streams set aside, the top-level handler, one write of each
    stream, ``os._exit``."""
    try:
        out, err = _set_aside_streams()
    except OSError:
        os._exit(INTERNAL)
    try:
        code, stdout, stderr = main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 - one fixed line, whatever went wrong
        code, stdout, stderr = INTERNAL, b"", (INTERNAL_LINE + "\n").encode("ascii")
    _write(out, stdout)
    _write(err, stderr)
    os._exit(code)


if __name__ == "__main__":
    run()
