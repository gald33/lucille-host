"""The local gate: nothing happens over a new pair until the user confirms, on the computer, that
the SAS it shows is the one Lucille's page shows.

Switchboard's ``confirmed`` flag means only that Lucille collected this computer's answer. Nobody
has compared anything by then, so the host keeps a gate of its own (spec/PROTOCOL.md section 16).
Until it opens, the host touches nothing, posts nothing, accepts nothing and runs nothing.

- Once Lucille has collected the answer, the pair's short authentication string (SAS), 8 digits
  shown as ``NNNN-NNNN``, reaches the host. ``pair`` shows it and asks for ``y`` if Lucille's page
  shows the same; ``pair status`` shows it again for as long as the gate is shut.
- ``y`` (or ``yes``) at that prompt, or ``pair confirm`` later, opens the gate. ``n`` (or ``no``),
  ``pair reject``, or the deadline passing forgets the pair. Any other answer is asked again.
- The gate opens only on a SAS this computer has shown. While it has not shown one, the gate stays
  shut and has no deadline.
- The deadline is :data:`WINDOW` after the SAS is first shown on this computer, whichever command
  showed it. Past it, the pair is forgotten.

The gate's record is ``state/pair.json``. The host writes it before it answers a code, and deletes
it whenever the pair is forgotten. A record that is missing, or names another pair, keeps the gate
shut. It holds no SAS, no code and no key: only the pair's name, the hub the code named, and when
each step happened.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

from lucille_host import state

__all__ = [
    "WINDOW",
    "Gate",
    "answer",
    "load",
    "past_deadline",
    "remove",
    "save",
    "shown",
]

VERSION: Final = 1
# How long the user has to confirm the SAS, from when this computer first showed it.
WINDOW: Final = 86400.0
_YES: Final = frozenset({"y", "yes"})
_NO: Final = frozenset({"n", "no"})
_TIMES: Final = ("shown_at", "deadline", "opened_at", "touched_at")
_FIELDS: Final = frozenset({"v", "pair", "hub", "created_at", *_TIMES})


@dataclass(frozen=True)
class Gate:
    """The gate of one pair."""

    pair: str
    # The hub the pairing code named.
    hub: str
    created_at: float
    # When this computer first showed the SAS, and the deadline that sets.
    shown_at: float | None = None
    deadline: float | None = None
    opened_at: float | None = None
    # When a touch of the pair first ran after the gate opened.
    touched_at: float | None = None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def load(path: Path) -> Gate | None:
    """The gate in ``path``, or None when there is none. Anything else stops the host
    (:class:`state.StateError`)."""
    data = state.read_json(path)
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != _FIELDS or data["v"] != VERSION:
        raise state.StateError(f"{path} is not a lucille-host gate record")
    fields = {key: data[key] for key in _FIELDS - {"v"}}
    valid = (
        isinstance(fields["pair"], str)
        and isinstance(fields["hub"], str)
        and _number(fields["created_at"])
        and all(fields[key] is None or _number(fields[key]) for key in _TIMES)
        and (fields["shown_at"] is None) == (fields["deadline"] is None)
        and (fields["opened_at"] is None or fields["shown_at"] is not None)
    )
    if not valid:
        raise state.StateError(f"{path} is not a lucille-host gate record")
    return Gate(**fields)


def save(path: Path, gate: Gate) -> None:
    state.write_json(path, {"v": VERSION, **asdict(gate)})


def remove(path: Path) -> bool:
    return state.remove_file(path)


def shown(gate: Gate, now: float) -> Gate:
    """The gate once this computer has shown the SAS: the deadline is set the first time only."""
    if gate.shown_at is not None:
        return gate
    return replace(gate, shown_at=now, deadline=now + WINDOW)


def past_deadline(gate: Gate, now: float) -> bool:
    return gate.deadline is not None and now > gate.deadline


def answer(entry: str) -> bool | None:
    """The user's answer to "does Lucille's page show the same SAS?": True for ``y`` or ``yes``,
    False for ``n`` or ``no`` (in either case, around any spaces), None for anything else."""
    word = entry.strip().lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    return None
