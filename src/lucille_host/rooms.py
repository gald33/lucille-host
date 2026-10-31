"""The host's own record of the work rooms Lucille offers over the pair: ``state/rooms.json``.

Switchboard reports a pending offer to the host once, as a note from the first touch that sees it,
and none of the sharing calls the host uses lists the offers waiting (spec/PROTOCOL.md sections 6
and 19). So the host writes down each offer a call reports before it decides on it, and keeps the
rest of what its accept policy needs here, apart from the share store:

- ``pair`` and ``made``: the pair the offers belong to, by its name and the time its gate record
  was written (a pair made again under the same name is another pair);
- ``generation``: the generation of pair:<name> the host last followed; a resolve that gives
  another one is a move (a heal, or a lapse), which the library reports once at most;
- ``offers``: each offer not yet decided, by the name Lucille gave it, with when it was first
  reported;
- ``accepting``: written before an accept: the offer, the name the host keeps the room under, and
  the rooms it replaces once it passes checks 3 and 4, so that a stop between the accept and the
  replacement is completed at the next start;
- ``held``: each work room the host holds, by its own name for it (``room-<n>``, never reused,
  so that no name Lucille chose can collide with one the host holds), with its workspace, and why
  it was abandoned if its leave has not run yet;
- ``left``: every room this profile has left, by workspace, with why: kept across pairs, so that
  the host never accepts a room again once it has left it;
- ``no_room``: the check that failed, when the host left its only room on its own and holds none.

It holds no key and no SAS: rooms' keys are in the share store, beside the pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lucille_host import state

__all__ = [
    "CHECKS",
    "Accepting",
    "Held",
    "Left",
    "Record",
    "failed_check",
    "load",
    "local_name",
    "same_hub",
    "save",
]

VERSION: Final = 1
# At most this many rooms whose list names the current pair, and at most this many held in all
# (check 5): the current room, and its replacement while one is handed over.
CAP: Final = 2

# The checks of the accept policy (spec/PROTOCOL.md section 6). A refusal by the first two or the
# last, before an accept, is not final: the offer is considered again.
CHECK_PAIR: Final = "check 1"
CHECK_GATE: Final = "check 2"
CHECK_CAP: Final = "check 5"
# Why the host left a room on its own, as its log and ``status`` name it.
CHECK_HUB: Final = "check 3"
CHECK_TOKEN: Final = "check 4"
LEFT_BEFORE: Final = "left before"
CHECKS: Final = {
    CHECK_HUB: "the room's hub is not the hub the pairing code named",
    CHECK_TOKEN: "Lucille's offer of the room carried a hub token",
    LEFT_BEFORE: "this computer had left the room before, and never takes one back",
}
# Why the host abandoned a room it held.
REPLACED: Final = "replaced"
LUCILLE_LEFT: Final = "left by Lucille"
MOVED: Final = "pair moved"
NOT_LISTED: Final = "not over the current pair"
PAIR_OVER: Final = "pair over"
# A room that left the share store without this host leaving it (another command left it).
GONE: Final = "gone"

_KEYS: Final = frozenset(
    {"v", "pair", "made", "generation", "offers", "accepting", "held", "left", "no_room", "named"}
)


@dataclass
class Held:
    """A work room the host holds."""

    workspace: str
    accepted_at: float
    # Set once the host abandons the room; the room stays here until its leave has run.
    abandoned: str | None = None


@dataclass(frozen=True)
class Accepting:
    """An accept about to be made, or made and not yet judged."""

    offer: str
    room: str
    replaces: tuple[str, ...]


@dataclass(frozen=True)
class Left:
    """A room this profile left."""

    room: str
    why: str
    at: float


@dataclass
class Record:
    pair: str | None = None
    made: float | None = None
    generation: int | None = None
    offers: dict[str, float] = field(default_factory=dict)
    accepting: Accepting | None = None
    held: dict[str, Held] = field(default_factory=dict)
    left: dict[str, Left] = field(default_factory=dict)
    no_room: str | None = None
    named: int = 0

    def bind(self, pair: str, made: float) -> bool:
        """Make this the record of pair ``pair`` made at ``made``; whether it was another's.

        The offers and the generation belong to a pair and start again with a new one. The rooms
        held stay, to be judged and left; so do the rooms left, and the names given."""
        if self.pair == pair and self.made == made:
            return False
        self.pair, self.made = pair, made
        self.generation = None
        self.offers = {}
        self.no_room = None
        return True

    def in_use(self) -> list[str]:
        """The rooms held and not abandoned."""
        return sorted(name for name, held in self.held.items() if held.abandoned is None)


def same_hub(room_url: str, code_url: str) -> bool:
    """Check 3: the two URLs are the same string once every trailing ``/`` is removed."""
    return room_url.rstrip("/") == code_url.rstrip("/")


def failed_check(
    *, url: str, carries_token: bool, workspace: str, hub: str, record: Record
) -> str | None:
    """Which check a room the host holds fails, if any: whether it was left before (the host
    never takes a room back), check 3 (its hub) and check 4 (a hub token in the offer)."""
    if workspace in record.left:
        return LEFT_BEFORE
    if not same_hub(url, hub):
        return CHECK_HUB
    if carries_token:
        return CHECK_TOKEN
    return None


def local_name(record: Record) -> str:
    """The next name of the host's own for a room it accepts; the caller saves the count."""
    record.named += 1
    return f"room-{record.named}"


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _invalid(path: Path) -> state.StateError:
    return state.StateError(f"{path} is not a lucille-host room record")


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def load(path: Path) -> Record:
    """The record in ``path``; an empty one when there is none. Anything else stops the host
    (:class:`state.StateError`)."""
    data = state.read_json(path)
    if data is None:
        return Record()
    if not isinstance(data, dict) or set(data) != _KEYS or data["v"] != VERSION:
        raise _invalid(path)
    try:
        return _record(data)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _invalid(path) from None


def _record(data: dict[str, Any]) -> Record:
    pair, made, generation = data["pair"], data["made"], data["generation"]
    named, no_room = data["named"], data["no_room"]
    if not (
        (pair is None or isinstance(pair, str))
        and (made is None or _number(made))
        and (generation is None or _integer(generation))
        and _integer(named)
        and named >= 0
        and (no_room is None or no_room in CHECKS)
    ):
        raise ValueError
    offers = data["offers"]
    if not isinstance(offers, dict) or not all(_number(at) for at in offers.values()):
        raise ValueError
    accepting = data["accepting"]
    if accepting is not None:
        if set(accepting) != {"offer", "room", "replaces"} or not _strings(accepting["replaces"]):
            raise ValueError
        if not (isinstance(accepting["offer"], str) and isinstance(accepting["room"], str)):
            raise ValueError
        accepting = Accepting(accepting["offer"], accepting["room"], tuple(accepting["replaces"]))
    held: dict[str, Held] = {}
    for name, entry in data["held"].items():
        if set(entry) != {"workspace", "accepted_at", "abandoned"}:
            raise ValueError
        if not (
            isinstance(entry["workspace"], str)
            and _number(entry["accepted_at"])
            and (entry["abandoned"] is None or isinstance(entry["abandoned"], str))
        ):
            raise ValueError
        held[name] = Held(entry["workspace"], entry["accepted_at"], entry["abandoned"])
    left: dict[str, Left] = {}
    for workspace, entry in data["left"].items():
        if set(entry) != {"room", "why", "at"}:
            raise ValueError
        if not (isinstance(entry["room"], str) and isinstance(entry["why"], str)):
            raise ValueError
        if not _number(entry["at"]):
            raise ValueError
        left[workspace] = Left(entry["room"], entry["why"], entry["at"])
    return Record(
        pair=pair,
        made=made,
        generation=generation,
        offers=dict(offers),
        accepting=accepting,
        held=held,
        left=left,
        no_room=no_room,
        named=named,
    )


def save(path: Path, record: Record) -> None:
    accepting = record.accepting
    state.write_json(
        path,
        {
            "v": VERSION,
            "pair": record.pair,
            "made": record.made,
            "generation": record.generation,
            "offers": dict(record.offers),
            "accepting": None
            if accepting is None
            else {
                "offer": accepting.offer,
                "room": accepting.room,
                "replaces": list(accepting.replaces),
            },
            "held": {
                name: {
                    "workspace": held.workspace,
                    "accepted_at": held.accepted_at,
                    "abandoned": held.abandoned,
                }
                for name, held in record.held.items()
            },
            "left": {
                workspace: {"room": gone.room, "why": gone.why, "at": gone.at}
                for workspace, gone in record.left.items()
            },
            "no_room": record.no_room,
            "named": record.named,
        },
    )
