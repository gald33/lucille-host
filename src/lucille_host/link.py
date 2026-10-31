"""Pairing with Lucille: taking its code, waiting for it to collect the answer, and the local gate.

What ``lucille-host pair`` and its sub-verbs do, made of the calls in ``_switchboard.py``:

- **The code.** It is decoded, then refused unless it names Lucille's hub, or the loopback hub the
  local configuration names (for the conformance suite), and carries no hub token. Only then is
  its note shown, sanitized (:func:`sanitize`), since its text is whatever the code's maker chose.
- **The answer.** The gate's record is written, then the code is answered with the host's own
  stores and ``base=None``. The policy is reset first, by the command (``cli.py``).
- **The wait.** The host resolves the pair every :data:`RESOLVE_EVERY` seconds, and at once when
  one of the library's notes arrives on the ``pair`` channel of pair:<name>; a long-poll of that
  channel is the clock. A resolve also sends again an answer that has not reached the hub. The
  wait ends when the SAS reaches the host. The library's ``swbp1/paired`` note is only a wake: it
  is posted best effort and never retried, so the periodic resolve is what finds the pair
  collected.
- **How a pairing ends early.** A leaked code (another machine answered it, or somebody interfered
  with the handshake), a pairing that ran out of time, and a pair that Lucille ended each stop the
  wait. Whatever is left of the pair on this computer is forgotten, the gate's record is deleted,
  and the command says which it was (:func:`outcome`).
- **The gate** (``gate.py``). Once the SAS has reached the host, ``pair`` shows it and asks for
  ``y`` if Lucille's page shows the same (:meth:`Session.show`, :meth:`Session.open_gate`).
  ``pair status`` reports ``none``, ``waiting``, ``sas_pending`` (showing the SAS again, with the
  deadline) or ``confirmed``; ``pair confirm`` is the ``y`` given later; ``n``, ``pair reject`` and
  the deadline forget the pair. A pair with no gate record is refused (``refused``), and ``pair
  confirm`` refuses a SAS no command has shown (``sas_ready``). ``lucille-host status`` reports
  these words from what this computer holds, with no hub call and without the SAS
  (:meth:`Session.pair_state`). Once the code is answered, the gate's record is changed, and its
  deadline judged, only under the record lock, which ``run`` holds through each step.
- **Forgetting** (``lucille-host forget``, :meth:`Session.forget`). Every work room held is left
  first, then the pair is forgotten here and Lucille is told, best effort; the gate's record and how
  far each room was read go too. The journal of requests stays: a job id is answered once per
  profile, whatever pair it came over. All of it is done under the record lock, and touches only
  what is that pair's: a gate record that names another pair stays, and so does a record of rooms
  that names another pair held here. A command that found the pair held, and finds once it holds the
  lock that another command forgot it meanwhile (``forget``, ``pair reject``, the deadline, or a
  resolve or an answer that found the pair over), or forgot it and paired again under the same name,
  does not forget it again, and says so (``none``): it does not tell Lucille again, nor say that
  Lucille could not be told. Every resolve, and the answer to a code, is made under the record lock.
  So no command forgets the pair within a resolve, which the library would then report as ``ended``;
  what is left of a pair the library drops in the call (this side ended it, the code leaked) is
  forgotten before the lock is let go; and the answer's gate record and the pair the library saves
  come together, so that ``forget`` never takes that record for one a dropped pair left. ``pair
  status`` and ``pair confirm`` hold the lock on to their judgement. A ``run`` whose own touch had
  the library drop the pair (the touch resolves it) resolves at once, in the same step and under the
  same lock, whatever back-off is owed; it finds no such pair, and forgets what is left as when a
  resolve drops it: its gate record shows that no other command forgot it. Once the library has
  dropped the pair, a work room that cannot be left keeps nothing: the gate record goes all the
  same, and ``forget``, with no pair held, leaves the room later
  (:meth:`Session.leave_left_over`). The rooms are marked abandoned in their record before any hub
  call, and a ``run`` going at the time stops its work in them on its next look at that record,
  without waiting for the lock (``loop.py``). It finds at its next step, once the command is done,
  that its pair's gate record is gone (or names another pair, or another pairing under the same
  name), whether or not Lucille was told, and stops there, touching nothing (:meth:`Session.start`
  notes which record it serves, :meth:`Session.serves`).

Once the gate is open, the same :class:`Session` keeps the pair and takes the work rooms Lucille
offers over it (spec/PROTOCOL.md sections 6, 7 and 9). It is driven by events, one at a time:
:meth:`Session.start`, :meth:`Session.note` for a message on the ``pair`` channel,
:meth:`Session.wake`, :meth:`Session.hub_error`, and :meth:`Session.step`, which does whatever is
then due. The rules:

- **Resolving.** At start, on the library's ``paired``, ``moved`` and ``ended`` notes, after a hub
  error, every :data:`RESOLVE_EVERY` seconds while Lucille has not collected the answer, and at
  least every :data:`RESOLVE_AT_LEAST` seconds; and after a touch, when the store keeps a
  generation of pair:<name> other than the one followed (another command followed a move first).
  A resolve that fails is tried again after a back-off: the hub's ``retry_after`` when it gave
  one, which for a read of the pair room the library returns (``Pair.retry_after``) rather than
  raises. A resolve is due at once, whatever back-off or rhythm counted from the last, when the
  wall clock now reads more than :data:`SET_BACK` seconds earlier than that resolve (it was set
  back), and after a touch that found the pair gone (for a pair the library no longer holds, a
  resolve makes no hub call). Once the gate is open, a touch follows every resolve the host makes,
  except one made because a touch failed: the next touch then comes on the rhythm.
- **Touching** (reading the pair's offers). Nothing before the gate opens. Then: when it opens, at
  start, on a ``shared`` note, after a resolve, on a wake, and :data:`TOUCH_EVERY` seconds after
  the last touch, or at once when the wall clock now reads more than :data:`SET_BACK` seconds
  earlier than that touch (it was set back).
- **Offers.** Each offer the library notes in the share store is written down before anything is
  decided (``rooms.py``), whichever call made the note, one that then refused included
  (:meth:`_switchboard.Link.notes`). Checks 1, 2 and 5 run before an accept, and a refusal by
  them is not final: the offers are considered again at start, when the gate opens, after a room
  is left, and when an offer is withdrawn. An accept that failed without refusing the offer (the
  hub, or the pair's room, could not be read) is tried again after the next touch that reads the
  pair's room. While more than one offer is pending, none is accepted, once those the share store
  no longer knows are dropped. Checks 3 and 4 run right after the accept, and a room that fails
  them, or that this computer left before, is left at once and never used. A room that passes
  them replaces the others held over the pair, which are abandoned.
- **Rooms held** are checked again (checks 3 and 4) at start and before each use, and whether a
  room's list still names the pair is judged only once a touch has run since the pair was made.
- **Abandoning** a room records it first, then stops whatever the room was doing (a callback the
  read loop sets), then leaves it. A move of pair:<name> (a heal, or a lapse), Lucille leaving the
  room, a room whose list no longer names the pair, a newer room, and the end of the pair each
  abandon it. A move abandons, unjudged, the room of an accept a stop interrupted too. When the
  pair ends or is forgotten, its rooms are left before the pair is.

Nothing here starts a process, and nothing is written outside the profile's own folders and its
keyring item.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Final

from lucille_host import _switchboard, gate, paths, rooms, state, store
from lucille_host.audit import AuditLog

__all__ = [
    "LEAKED",
    "LONG_POLL",
    "PAIR_AGAIN",
    "RESOLVE_AT_LEAST",
    "RESOLVE_EVERY",
    "SET_BACK",
    "TOUCH_EVERY",
    "Session",
    "Stopped",
    "open_session",
    "outcome",
    "sanitize",
]

# While Lucille has not collected the answer, the host resolves the pair this often.
RESOLVE_EVERY: Final = 5.0
# Otherwise it resolves at least this often, which keeps its record in the pair room fresh.
RESOLVE_AT_LEAST: Final = 86400.0
# Once the gate is open, it reads the pair's offers at least this often.
TOUCH_EVERY: Final = 300.0
# A wall clock that reads more than this much earlier than the last touch, or the last resolve,
# was set back, and the next one is due at once. A smaller step back, as a clock's jitter, puts it
# off by no more.
SET_BACK: Final = 60.0
# After a hub call of its own fails, it waits this long before the next, doubling up to the most.
BACKOFF_FIRST: Final = 1.0
BACKOFF_MOST: Final = 30.0
# The longest a hub holds a long-poll.
LONG_POLL: Final = 25.0
# The longest note shown.
NOTE_LIMIT: Final = 200
# What the host says when the hub refuses a write for this computer's clock.
SKEWED: Final = (
    "this computer's clock is more than 300 s from the hub's, so the hub refuses what the host "
    "writes; set the clock right"
)
# In the state folder: held while the room record is read, changed and written whole, so that
# `run` and another command (`forget`, say) never change it at once.
RECORD_LOCK: Final = ".record.lock"

# Switchboard's states for a code that leaked: another machine answered it, or somebody holding
# it interfered with the handshake.
LEAKED: Final = frozenset({"already_redeemed", "aborted", "superseded"})
# Its states for a pairing that ran out of time, or whose answer never reached Lucille.
PAIR_AGAIN: Final = frozenset({"expired", "never_confirmed", "unsent", "wrong_hub"})
# Its states for a pair that was ended: by Lucille, or on this computer by another command.
ENDED: Final = frozenset({"ended", "ended_by_peer"})

# Shown in place of: C0 and C1 controls, DEL, unpaired surrogates, line and paragraph separators,
# and the controls that reorder text (bidirectional marks, embeddings, overrides and isolates).
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\ud800-\udfff\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]"
)

_STOPPED_MESSAGES: Final = {
    "leaked": (
        "the pairing code leaked: another computer answered it, or somebody holding it "
        "interfered with the pairing, so this computer's half is gone. Start again in Lucille "
        "with a fresh code, and share it with nobody"
    ),
    "expired": (
        "the pairing did not finish in time: the code had expired, or Lucille did not collect "
        "this computer's answer. Start again in Lucille with a fresh code"
    ),
    "ended": "the pair was ended (by Lucille, or on this computer); this computer's copy is gone",
    "none": "the pair is gone from this computer",
}

# What the pairing verbs say of a pair with no gate record, which cannot be confirmed here.
_NO_GATE: Final = (
    "pair {name} has no local gate record, so it cannot be confirmed here: run "
    "`lucille-host pair reject` and start again in Lucille"
)
# What is said of a pair another command forgot while this one worked on it.
_GONE: Final = "pair {name} is gone from this computer: another command forgot it"
# What is said when a work room held over a pair this computer no longer holds could not be left.
_STRANDED: Final = (
    "pair {name} is gone from this computer, and a work room held over it could not be left: its "
    "keys stay in the share store until `lucille-host forget` leaves it"
)

# What a step may have to do, as flags set by events and cleared when done: a burst of events is
# one of each.
_RESOLVE: Final = "resolve"
_TOUCH: Final = "touch"
_JUDGE: Final = "judge"
_CONSIDER: Final = "consider"
# Why a resolve is made, when the reason changes what follows it.
_TOUCH_FAILED: Final = "touch failed"
# The library's notes that only make the host resolve the pair.
_RESOLVE_TAGS: Final = frozenset(
    {_switchboard.TAG_PAIRED, _switchboard.TAG_MOVED, _switchboard.TAG_ENDED}
)
# What the share store says of a name it holds nothing under, or no longer holds.
_NOT_HELD: Final = frozenset({"unknown", "left", "offered"})


class Stopped(Exception):
    """A pairing that ended, or a verb that cannot go on. ``state`` is the word printed first."""

    def __init__(self, word: str, message: str) -> None:
        super().__init__(message)
        self.state = word


def sanitize(text: str, limit: int = NOTE_LIMIT) -> str:
    """Text the other side chose, made safe to show on a terminal: each control, surrogate and
    reordering character becomes a space, and it is cut to ``limit`` characters."""
    return _UNSAFE.sub(" ", text)[:limit].strip()


def outcome(switchboard_state: str) -> str:
    """The host's word for a pairing call's refusal: ``leaked``, ``expired`` (pair again),
    ``ended``, ``none`` (no such pair), or ``error`` for anything that does not end the pair."""
    if switchboard_state in LEAKED:
        return "leaked"
    if switchboard_state in PAIR_AGAIN:
        return "expired"
    if switchboard_state in ENDED:
        return "ended"
    if switchboard_state == "unknown":
        return "none"
    return "error"


def iso(moment: float) -> str:
    """A time as UTC ISO 8601, to the second."""
    when = datetime.datetime.fromtimestamp(moment, datetime.UTC)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def _replaced(seen: gate.Gate | None, found: gate.Gate | None) -> bool:
    """Whether ``found``, a pair's gate record as read now, was made for another pairing than
    ``seen``, the record a command found when it looked for the pair held: the pair was forgotten
    and paired again under the same name meanwhile."""
    return seen is not None and found is not None and found.created_at != seen.created_at


def told_sentence(told: _switchboard.Told) -> str:
    if told.told:
        return "Lucille was told"
    return (
        "Lucille was not told (the hub could not be reached): it keeps its half until the "
        "computer is removed there"
    )


class Session:
    """One profile's pair: as the pairing verbs see it, and, once its gate is open, the work
    rooms Lucille offers over it."""

    def __init__(
        self, dirs: paths.Dirs, link: _switchboard.Link, *, say: Callable[[str], None]
    ) -> None:
        self.dirs = dirs
        self.link = link
        self.say = say
        self.audit = AuditLog(paths.ensure_dir(dirs.state))
        # Called with a room's name when the host abandons it, after the record and before the
        # leave: the read loop stops reading the room there and kills its job, with no reply.
        self.stop_room: Callable[[str], None] | None = None
        # The pair as the last resolve that read its room found it.
        self.pair: _switchboard.Pair | None = None
        self._name: str | None = None
        # Once :meth:`start` has run: when the gate record of the pair this session serves was
        # made. Another command that forgets the pair deletes that record, and a new pairing
        # writes another, so the record tells whether the pair served is still this profile's.
        self._made: float | None = None
        self._wanted: set[str] = set()
        self._cause = ""
        self._last_resolve: float | None = None
        self._last_touch: float | None = None
        self._retry_at = 0.0
        self._failures = 0
        self._was_open = False
        # Rooms a touch said Lucille left, until their list is judged: the reason to give.
        self._suspect: dict[str, str] = {}
        self._notes: collections.deque[_switchboard.ShareNote] = collections.deque()
        self._digesting = False
        # An accept failed for a reason that passes (the hub, or the pair's room, could not be
        # read): the offers are considered again after the next touch that reads the pair's room.
        self._retry = False
        # Set when the hub refused a write the library made for this session (an answer, a
        # touch, an accept) because of this computer's clock; cleared by a touch that reached
        # the pair's room, which wrote whatever was owed.
        self.skewed = False

    # --- the gate's record ---------------------------------------------------------------

    def gate(self, name: str) -> gate.Gate | None:
        """The gate of pair ``name``; None when its record is missing or names another pair."""
        found = gate.load(self.dirs.gate_file)
        return found if found is not None and found.pair == name else None

    def _save(self, record: gate.Gate) -> gate.Gate:
        gate.save(self.dirs.gate_file, record)
        return record

    def show(self, name: str) -> tuple[str, gate.Gate] | None:
        """The SAS to show the user, and the gate once it is recorded as shown: the deadline
        starts the first time. None when there is no SAS to show yet (Lucille has not collected
        the answer), or no gate record to note it in, since such a pair cannot be confirmed.

        Held under the record lock, as every later change to the gate's record is, so that no
        command forgetting the pair removes the record between this read and this write."""
        with self._record_lock():
            found = self.gate(name)
            sas = self.link.sas(name)
            if found is None or sas is None:
                return None
            if found.shown_at is None:
                found = self._save(gate.shown(found, self.link.now()))
                self.audit.write(
                    "pair", event="sas_shown", pair=name, deadline=iso(found.deadline or 0)
                )
            return sas, found

    # --- the pair ------------------------------------------------------------------------

    def held(self) -> list[str]:
        return self.link.names()

    def current(self) -> str | None:
        """The pair this profile holds, if any."""
        names = self.held()
        if not names:
            return None
        found = gate.load(self.dirs.gate_file)
        if found is not None and found.pair in names:
            return found.pair
        if len(names) == 1:
            return names[0]
        raise Stopped(
            "error",
            f"this profile holds {len(names)} pairs ({', '.join(names)}), and the host keeps one",
        )

    def _record_lock(self) -> contextlib.AbstractContextManager[None]:
        return store.file_lock(paths.ensure_dir(self.dirs.state) / RECORD_LOCK)

    def _serving(self, name: str) -> gate.Gate | None:
        """The gate of pair ``name``, read now; None when its record is missing or names another
        pair. Once :meth:`start` has run, the record must be the one the session started with:
        one that is gone, names another pair, or was made for another pairing under the same
        name means another command forgot the pair this session serves, and perhaps paired
        again. That raises :class:`Stopped` (``none``), and nothing the profile holds now is
        touched: it may be another pair's."""
        found = gate.load(self.dirs.gate_file)
        if self._made is None:
            return found if found is not None and found.pair == name else None
        if found is None or found.pair != name or not self.serves(found):
            raise Stopped("none", _GONE.format(name=name))
        return found

    def serves(self, found: gate.Gate | None) -> bool:
        """Whether ``found``, a gate record as read now, is the one this session serves since
        :meth:`start`: the same pair, made at the same time. False before :meth:`start`. It
        takes no lock and makes no hub call, so that ``run``'s other threads can ask while a
        command holds the record lock."""
        return (
            self._made is not None
            and found is not None
            and (found.pair, found.created_at) == (self._name, self._made)
        )

    def no_gate(self, name: str) -> Stopped:
        """Why pair ``name`` cannot be confirmed when it has no gate record: it is gone (another
        command forgot it), or it is held with no record (``pair reject`` forgets it)."""
        if name not in self.held():
            return Stopped("none", _GONE.format(name=name))
        return Stopped("refused", _NO_GATE.format(name=name))

    def _remove_gate(self, found: gate.Gate) -> None:
        """Delete the gate's record if it is still ``found``: the same pair, made at the same
        time. A record another pairing wrote since is not this one to delete. Under the record
        lock."""
        now = gate.load(self.dirs.gate_file)
        if now is not None and (now.pair, now.created_at) == (found.pair, found.created_at):
            gate.remove(self.dirs.gate_file)

    def _forget(
        self, name: str, why: str, *, dropped: bool = False, seen: gate.Gate | None = None
    ) -> _switchboard.Told:
        """Leave every work room held over the pair, then forget the pair here and tell Lucille
        best effort; then delete the gate's record. While the pair is held, a room that cannot be
        left keeps it: the rooms of a pair that is over must not stay in the share store with
        their keys.

        All of it under the record lock, and only what is this pair's: a gate record, or a record
        of rooms, that names another pair is left as it is, and a session whose own pair another
        command forgot already forgets nothing (:meth:`_serving`). So does a command that found the
        pair held (its gate record then, if it had one, is ``seen``), when before this one had the
        lock another command forgot it: the pair is no longer held, or its gate record was made
        since, for another pairing under the same name. That raises :class:`Stopped` (``none``),
        as :meth:`_require` does when no pair is held, and Lucille is not told again. A pair no
        longer held whose rooms are still held, or whose gate record is still there, is one the
        library dropped in a call whose process has not forgotten what is left: one that could
        not leave a room (it keeps no gate record), or one stopped part way. Those rooms are left
        now, and the gate record deleted, before saying so (:meth:`_leave_left`).

        ``dropped`` says a call of this session has just found the pair over, and the library may
        have dropped it itself, as ended from this side, say (:meth:`_end`). The pair being no
        longer held then shows nothing of another command, so it is forgotten as before, its rooms
        left, gate record or not. If the library did drop it, a room that cannot be left keeps
        nothing, since the pair is gone already: its gate record is deleted all the same, and
        :class:`Stopped` (``error``) says that ``forget`` leaves the room.

        A pair held when this began can be dropped by the library within a leave of its rooms,
        when the pair room says this side ended it (:meth:`_leave_held`). A room that cannot be
        left then keeps nothing either. And Lucille is not told again: the pair room says already
        what telling Lucille writes there, so it is said told, not that the hub could not be
        reached, as a ``forget`` of the library's would say of a pair it no longer holds."""
        with self._record_lock():
            found = self._serving(name)
            if _replaced(seen, found):
                raise Stopped("none", _GONE.format(name=name))
            gone = name not in self.held()
            if gone and not dropped:
                self._leave_left(name, found)
                raise Stopped("none", _GONE.format(name=name))
            self.audit.write("pair", event="forget", pair=name, why=why)
            if gone:
                self._leave_left(name, found)
                told = self.link.forget(name)
            else:
                self._leave_held(name, found)
                if name in self.held():
                    told = self.link.forget(name)
                else:
                    # A leave had the library drop the pair, as ended from this side.
                    told = _switchboard.Told(name, True)
            if found is not None:
                self._remove_gate(found)
        self.audit.write("pair", event="forgotten", pair=name, why=why, told=told.told)
        return told

    def _leave_left(self, name: str, found: gate.Gate | None) -> None:
        """Leave the work rooms still held over pair ``name``, which this computer no longer
        holds, and delete its gate record ``found``. A room that cannot be left stays held, and
        the record is deleted all the same, since the pair cannot come back: that raises
        :class:`Stopped` (``error``), saying that ``forget`` leaves the room
        (:meth:`leave_left_over`). Under the record lock."""
        try:
            self._leave_all(name)
        except (Stopped, store.StoreError):
            raise Stopped("error", _STRANDED.format(name=name)) from None
        finally:
            if found is not None:
                self._remove_gate(found)

    def _leave_held(self, name: str, found: gate.Gate | None) -> None:
        """:meth:`_leave_all` for pair ``name``, which this computer held when it began. A leave
        touches the pairs on the room's list (sb ``sharing.py:1733``), and the touch has the
        library drop a pair whose pair room says this side ended it (sb ``pairing.py:2312-2319``).
        A room that cannot be left after that keeps nothing, as in :meth:`_leave_left`: the gate
        record ``found`` is deleted all the same, and :class:`Stopped` (``error``) says that
        ``forget`` leaves the room. Under the record lock."""
        try:
            self._leave_all(name)
        except (Stopped, store.StoreError):
            if name in self.held():
                raise
            if found is not None:
                self._remove_gate(found)
            raise Stopped("error", _STRANDED.format(name=name)) from None

    def _found_held(self) -> tuple[str, gate.Gate | None]:
        """:meth:`_require`, and the gate record of that pair as read just before it looked
        (None when there was none), for :meth:`_forget` to tell it from the record of another
        pairing made since under the same name."""
        before = gate.load(self.dirs.gate_file)
        name = self._require()
        return name, before if before is not None and before.pair == name else None

    def forget(self, why: str = "forget") -> _switchboard.Told:
        """``lucille-host forget``: leave the pair's work rooms, then forget the pair here and
        tell Lucille, best effort; then drop how far each room was read."""
        name, seen = self._found_held()
        told = self._forget(name, why, seen=seen)
        state.remove_file(self.dirs.cursors_file)
        return told

    def leave_left_over(self) -> str | None:
        """``lucille-host forget`` when this computer holds no pair: leave the work rooms still
        held over a pair it no longer holds, which a call that dropped that pair could not leave
        (:meth:`_leave_left`), and drop how far each room was read. Returns that pair's name;
        None, doing nothing, while a pair is held, whose ``forget`` leaves them
        (:meth:`_leave_all`), or when there is nothing to leave. Lucille is not told: the pair is
        gone here already."""
        with self._record_lock():
            if self.held():
                return None
            record = rooms.load(self.dirs.rooms_file)
            name = record.pair
            if name is None or (not record.held and record.accepting is None):
                return None
            self._leave_left(name, self.gate(name))
        state.remove_file(self.dirs.cursors_file)
        return name

    def _end(self, name: str, switchboard_state: str) -> Stopped:
        """A pairing call said the pair is over: forget what is left of it here. Any state but
        ``unknown`` says the pair was there, and the call may have dropped it. ``unknown`` says the
        library held no such pair when the call began. For a command, another command forgot it.
        For ``run``, whose gate record :meth:`_forget` finds intact (:meth:`_serving`), no host
        command did: its own touch, earlier in the same step and under the same lock, had the
        library drop the pair (a touch resolves the pair within the library, sb
        ``sharing.py:1082-1090``), and what is left of it is forgotten here as after any other
        state."""
        word = outcome(switchboard_state)
        dropped = switchboard_state != "unknown" or self._made is not None
        self._forget(name, why=switchboard_state, dropped=dropped)
        return Stopped(word, f"{_STOPPED_MESSAGES[word]} ({switchboard_state})")

    def resolve(self, name: str) -> _switchboard.Pair:
        """Resolve once. A refusal that ends the pair forgets it and raises :class:`Stopped`.

        Under the record lock, which ``pair status``, ``pair confirm`` and a step of ``run`` hold
        already, and ``pair``'s wait takes here: a ``forget`` or ``pair reject`` comes before a
        resolve or after it, never within it. Within it, the pair would be forgotten while the
        library reads the pair room, and the library, saving what it read, then says that the pair
        was forgotten as ``ended`` (sb ``pairing.py:2644-2647``): ``pair`` would say the pair was
        ended, and forget it a second time. After it, the next resolve finds no such pair, and
        says the pair is gone. And a resolve in which the library drops the pair has what is left
        of it here forgotten before the lock is let go: the other command then finds it gone."""
        with self._record_lock():
            try:
                return self.link.resolve(name)
            except _switchboard.PairFailed as exc:
                if outcome(exc.state) == "error":
                    raise Stopped("error", f"pair {name} cannot be used ({exc.state})") from None
                raise self._end(name, exc.state) from None

    # --- a new code ----------------------------------------------------------------------

    def check_code(self, text: str, config: state.HostConfig) -> _switchboard.Code:
        """Decode a code, and refuse it unless it names an allowed hub and carries no token."""
        try:
            code = _switchboard.decode_code(text)
        except _switchboard.BadCode as exc:
            raise Stopped("refused", str(exc)) from None
        allowed = [_switchboard.LUCILLE_HUB] + ([config.hub] if config.hub else [])
        if not any(code.url.rstrip("/") == hub.rstrip("/") for hub in allowed):
            if state.valid_loopback_hub(code.url):
                raise Stopped(
                    "refused",
                    "this code names a hub on this computer, which is accepted only when "
                    "`lucille-host pair --hub` names it",
                )
            raise Stopped(
                "refused",
                f"this code is for another hub, not Lucille's ({_switchboard.LUCILLE_HUB})",
            )
        if code.carries_token:
            raise Stopped(
                "refused",
                "this code carries a hub token, and the host takes only codes that do not",
            )
        return code

    def accept(self, code: _switchboard.Code) -> str:
        """Write the gate's record, then answer the code. Returns the pair's name. All of it under
        the record lock, so that to another command the gate record and the pair the library
        saves within the answer come together: ``forget`` with no pair held takes a gate record
        with no pair for one a dropped pair left, and deletes it with the rooms that pair still
        holds (:meth:`leave_left_over`), which between the two would be this new pairing's
        record, under the same name. And when the code leaked, the library drops the pair again
        in the same call, and what is left of it is forgotten before another command can act on
        it under the record lock, with no line in the audit log, which never had it as accepted.
        When the code is not answered, only the record this call wrote is deleted."""
        saved = gate.Gate(pair=code.name, hub=code.url, created_at=self.link.now())
        with self._record_lock():
            self._save(saved)
            try:
                name = self.link.accept(code)
            except _switchboard.PairFailed as exc:
                if exc.state == "unsent":
                    # Saved, but the answer has not reached the hub; the wait sends it again.
                    self.skewed = exc.skewed
                    self.audit.write(
                        "pair", event="accepted", pair=code.name, sent=False, skewed=exc.skewed
                    )
                    return code.name
                self._remove_gate(saved)
                word = outcome(exc.state)
                if word == "error":
                    raise Stopped("error", f"the code was not answered ({exc.state})") from None
                self.link.forget(code.name)
                raise Stopped(word, f"{_STOPPED_MESSAGES[word]} ({exc.state})") from None
            except _switchboard.HubError as exc:
                self._remove_gate(saved)
                raise Stopped(
                    "error", f"{exc}: nothing was answered, and the code can be tried again"
                ) from None
        self.audit.write("pair", event="accepted", pair=name, sent=True)
        return name

    def wait(self, name: str) -> None:
        """Wait for Lucille to collect the answer; returns once the SAS has reached the host."""
        workspace: str | None = None
        cursor = 0
        reader: _switchboard.PairNotes | None = None
        try:
            while True:
                try:
                    pair = self.resolve(name)
                except _switchboard.HubError as exc:
                    time.sleep(max(RESOLVE_EVERY, exc.retry_after or 0.0))
                    continue
                if pair.sas_known:
                    return
                if not pair.checked and pair.retry_after is not None:
                    # The hub refused to read the pair room for now, and said for how long.
                    time.sleep(max(RESOLVE_EVERY, pair.retry_after))
                    continue
                if reader is None or pair.workspace != workspace:
                    if reader is not None:
                        reader.close()
                        reader = None
                    reader = self.link.pair_notes(pair)
                    workspace, cursor = pair.workspace, 0
                cursor = self._listen(reader, cursor)
        finally:
            if reader is not None:
                reader.close()

    def _listen(self, reader: _switchboard.PairNotes, cursor: int) -> int:
        """Wait until the next resolve is due, or one of the library's notes arrives first.
        Returns the cursor past what was read."""
        due = time.monotonic() + RESOLVE_EVERY
        while (left := due - time.monotonic()) > 0:
            try:
                notes = reader.wait(since=cursor, wait=min(left, LONG_POLL))
            except _switchboard.HubError:
                time.sleep(max(0.0, due - time.monotonic()))
                break
            if notes:
                cursor = max(cursor, *(note.seq for note in notes))
            if any(note.tag for note in notes):
                break
        return cursor

    # --- the gate ------------------------------------------------------------------------

    def open_gate(self, name: str) -> str:
        """``y``: the user says Lucille's page shows the SAS this computer showed. Opens the
        gate; past the deadline, forgets the pair and raises :class:`Stopped`. Returns what to
        print.

        Held under the record lock, which ``run`` holds through each step, so that a deadline is
        judged on the record as this writes it, and no command forgetting the pair removes the
        record between this read and this write."""
        with self._record_lock():
            found = self.gate(name)
            if found is None:
                raise self.no_gate(name)
            if found.opened_at is not None:
                return f"pair {name} is already confirmed"
            if found.shown_at is None:
                raise Stopped(
                    "sas_ready",
                    f"pair {name}: this computer has not shown the SAS yet, so there is nothing "
                    "to confirm; `lucille-host pair status` shows it",
                )
            now = self.link.now()
            if gate.past_deadline(found, now):
                raise self._expired(name)
            self._save(replace(found, opened_at=now))
            self.audit.write("pair", event="gate_opened", pair=name)
            return f"pair {name} is confirmed on this computer"

    def _expired(self, name: str) -> Stopped:
        told = self._forget(name, why="deadline")
        return Stopped(
            "expired",
            "the SAS was not confirmed in time, so the pair is forgotten; start again in "
            f"Lucille. {told_sentence(told)}",
        )

    def pending(self, name: str, sas: str, found: gate.Gate) -> str:
        """What ``pair`` and ``pair status`` say of a pair whose SAS is shown and not confirmed."""
        before = f" before {iso(found.deadline)}" if found.deadline is not None else ""
        return (
            f"pair {name}: the SAS is {sas}. If Lucille's page shows the same, confirm it with "
            f"`lucille-host pair confirm`{before}; if not, `lucille-host pair reject`"
        )

    def confirm(self) -> str:
        """``pair confirm``, the ``y`` given after ``pair`` has exited: resolve once, then open
        the gate on the SAS this computer showed. Under the record lock from the resolve to the
        judgement, as :meth:`status` is."""
        name = self._require()
        with self._record_lock():
            found = self.gate(name)
            if found is not None and found.opened_at is not None:
                return f"pair {name} is already confirmed"
            try:
                known = self.resolve(name).sas_known
            except _switchboard.HubError:
                known = self.link.sas(name) is not None  # judged on what this computer holds
            if not known:
                raise Stopped(
                    "waiting",
                    f"pair {name}: Lucille has not collected this computer's answer yet, so "
                    "there is no SAS to confirm",
                )
            return self.open_gate(name)

    def status(self) -> tuple[str, str]:
        """``pair status``: resolve once, and say where the pair is: ``none``, ``waiting``,
        ``sas_pending`` or ``confirmed``. While the gate is shut and the SAS is known, it shows
        the SAS, which starts the deadline the first time. A pair with no gate record, which
        cannot be confirmed here, raises :class:`Stopped` (``refused``), as ``pair confirm``
        does; so does one past its deadline (``expired``), which is forgotten."""
        name = self.current()
        if name is None:
            return "none", "this computer holds no pair"
        # Under the record lock from the resolve, which takes it too, to the judgement: a
        # `forget`, `pair reject`, `pair confirm` or `run` step in another process changes the
        # pair or its gate record only under that lock, so it waits for both, or was done before
        # the resolve, which then finds what it left.
        with self._record_lock():
            try:
                pair: _switchboard.Pair | None = self.resolve(name)
            except _switchboard.HubError:
                pair = None
            known = pair.sas_known if pair is not None else self.link.sas(name) is not None
            found = self.gate(name)
            if found is not None and found.opened_at is not None:
                return "confirmed", f"pair {name}"
            if not known:
                return "waiting", (
                    f"pair {name}: Lucille has not collected this computer's answer yet"
                )
            if found is None:
                raise self.no_gate(name)
            if gate.past_deadline(found, self.link.now()):
                raise self._expired(name)
            shown = self.show(name)
            if shown is None:
                raise self.no_gate(name)
            return "sas_pending", self.pending(name, *shown)

    def reject(self) -> _switchboard.Told:
        """``pair reject``, and ``n`` at ``pair``'s prompt: forget a pair whose gate has not
        opened, and tell Lucille. Decided under the record lock, so that a ``pair confirm`` in
        another shell either opens the gate first, and the pair is kept, or finds it gone."""
        name, seen = self._found_held()
        with self._record_lock():
            found = self.gate(name)
            if _replaced(seen, found):
                raise Stopped("none", _GONE.format(name=name))
            if found is not None and found.opened_at is not None:
                raise Stopped(
                    "refused",
                    f"pair {name} is confirmed on this computer; `lucille-host forget` ends it",
                )
            return self._forget(name, why="rejected", seen=seen)

    def pair_state(self) -> tuple[str, str]:
        """``lucille-host status``: where the pair is, from this computer's store and the gate's
        record alone, with no hub call. The words are ``pair status``'s (``none``, ``waiting``,
        ``sas_pending`` with the deadline, ``confirmed``), and three more: ``sas_ready`` while
        the SAS has reached this computer and no command has shown it, ``refused`` for a pair
        with no gate record, which cannot be confirmed here, and ``expired`` for a SAS not
        confirmed by the deadline, which the next command that resolves the pair forgets. It
        never shows the SAS, and so never starts the deadline: ``pair status`` does both."""
        try:
            name = self.current()
        except Stopped as exc:
            return exc.state, str(exc)
        if name is None:
            return "none", "this computer holds no pair"
        kept = self.link.kept(name)
        found = self.gate(name)
        if found is not None and found.opened_at is not None:
            generation = f" (generation {kept.generation})" if kept is not None else ""
            return "confirmed", f"pair {name}{generation}"
        if kept is None or not kept.sas_known:
            return "waiting", f"pair {name}: Lucille has not collected this computer's answer yet"
        if found is None:
            return "refused", _NO_GATE.format(name=name)
        if found.shown_at is None or found.deadline is None:
            return "sas_ready", (
                f"pair {name}: the SAS has reached this computer, and no command has shown it "
                "yet: `lucille-host pair status` shows it to compare with Lucille's page"
            )
        if gate.past_deadline(found, self.link.now()):
            return "expired", (
                f"pair {name}: the SAS was not confirmed before {iso(found.deadline)}; the next "
                "`lucille-host pair status`, `lucille-host pair confirm` or `run` forgets it"
            )
        return "sas_pending", (
            f"pair {name}: `lucille-host pair status` shows the SAS to compare with Lucille's "
            f"page, and `lucille-host pair confirm` confirms it before {iso(found.deadline)}"
        )

    def _require(self) -> str:
        name = self.current()
        if name is None:
            raise Stopped("none", "this computer holds no pair")
        return name

    # --- the work rooms: events ----------------------------------------------------------

    def start(self) -> None:
        """At start: resolve the pair; once its gate is open, finish an accept that a stop
        interrupted, leave what is owed, judge every room held, touch, and consider the offers.
        A pair with no gate record, which can never be confirmed here, is refused, as ``pair
        status`` and ``pair confirm`` refuse it. From here on the session serves the pair whose
        record it found: once another command forgets that pair, each step stops
        (:meth:`_serving`)."""
        name = self._require()
        found = self.gate(name)
        if found is None:
            raise Stopped("refused", _NO_GATE.format(name=name))
        self._name, self._made = name, found.created_at
        self._want_resolve("start")
        self._wanted |= {_TOUCH, _JUDGE, _CONSIDER}
        self.step()

    def note(self, tag: str | None) -> None:
        """A message on the ``pair`` channel of pair:<name>, by the library's tag in it. A
        ``paired``, ``moved`` or ``ended`` note only asks for a resolve, and a ``shared`` note
        only for a touch; anything else asks for nothing."""
        if tag in _RESOLVE_TAGS:
            self._want_resolve("note")
        elif tag == _switchboard.TAG_SHARED:
            self._wanted.add(_TOUCH)

    def wake(self) -> None:
        """The computer woke from sleep: read the offers again."""
        self._wanted.add(_TOUCH)

    def hub_error(self) -> None:
        """Another part of the host met a hub error: resolve, unless a resolve ran lately."""
        if self._last_resolve is None or self.link.now() - self._last_resolve >= BACKOFF_MOST:
            self._want_resolve("hub error")

    def _want_resolve(self, cause: str) -> None:
        # Any other reason outranks a failed touch, so that the resolve it asks for is followed
        # by a touch.
        if _RESOLVE not in self._wanted or self._cause == _TOUCH_FAILED:
            self._cause = cause
        self._wanted.add(_RESOLVE)

    def due(self) -> float:
        """When :meth:`step` next has something to do of its own accord, by the link's clock.
        An event can make it sooner. ``run`` does not ask: it steps at least once a second."""
        times: list[float] = []
        if _RESOLVE in self._wanted:
            times.append(self._retry_at)
        if self._last_resolve is None:
            times.append(0.0)
        elif self._last_resolve - self.link.now() > SET_BACK:
            # The wall clock was set back since the last resolve, by more than a jitter: a resolve
            # is due at once, whatever back-off or rhythm counted from that one.
            times.append(0.0)
        else:
            times.append(self._last_resolve + RESOLVE_AT_LEAST)
            if self.pair is not None and not self.pair.sas_known:
                times.append(self._last_resolve + RESOLVE_EVERY)
        if self._was_open:
            if (
                self._wanted & {_TOUCH, _JUDGE, _CONSIDER}
                or self._last_touch is None
                # The wall clock was set back since the last touch, by more than a jitter: the
                # next is due at once.
                or self._last_touch - self.link.now() > SET_BACK
            ):
                times.append(0.0)
            else:
                times.append(self._last_touch + TOUCH_EVERY)
        return min(times)

    def step(self) -> None:
        """Do what is due, in order: resolve; then, once the gate is open, leave and judge the
        rooms, touch, resolve again if the touch asked for it, and consider the offers. Raises
        :class:`Stopped` when the pair is over (its rooms left first) or cannot be used."""
        with self._record_lock():
            self._step()

    def _step(self) -> None:
        name = self._name or self._require()
        self._name = name
        found = self._serving(name)
        opened = found is not None and found.opened_at is not None
        if opened and not self._was_open:
            self._wanted |= {_TOUCH, _JUDGE, _CONSIDER}
        self._was_open = opened
        record = self._bound(name, found) if opened and found is not None else None
        self._resolve_if_due(name, record)
        if record is None:
            shut = self.gate(name)
            if shut is not None and gate.past_deadline(shut, self.link.now()):
                raise self._expired(name)
            return
        self._judge_if_wanted(name, record)
        self._touch_if_due(name, record)
        self._resolve_if_due(name, record)
        self._judge_if_wanted(name, record)
        # Each pass either takes one offer, drops one, or stops; a few are always enough.
        for _ in range(8):
            if _CONSIDER not in self._wanted or _RESOLVE in self._wanted:
                break
            self._consider(name, record)

    # --- the work rooms: what the events do ----------------------------------------------

    def _bound(self, name: str, found: gate.Gate) -> rooms.Record:
        record = rooms.load(self.dirs.rooms_file)
        if record.bind(name, found.created_at):
            self._keep(record)
        return record

    def _keep(self, record: rooms.Record) -> None:
        rooms.save(self.dirs.rooms_file, record)

    def _resolve_if_due(self, name: str, record: rooms.Record | None) -> None:
        now = self.link.now()
        if self._last_resolve is not None and self._last_resolve - now > SET_BACK:
            # The wall clock was set back since the last resolve: the back-off, or the rhythm,
            # counts from this one, rather than waiting for the clock to catch up.
            due = True
        elif _RESOLVE in self._wanted:
            due = now >= self._retry_at
        elif self._last_resolve is None:
            due = True
        else:
            since = now - self._last_resolve
            waiting = self.pair is not None and not self.pair.sas_known
            due = since >= RESOLVE_AT_LEAST or (waiting and since >= RESOLVE_EVERY)
        if due:
            self._resolve_now(name, record)

    def _resolve_now(self, name: str, record: rooms.Record | None) -> None:
        cause, self._cause = self._cause, ""
        self._wanted.discard(_RESOLVE)
        now = self.link.now()
        self._last_resolve = now
        try:
            pair = self.resolve(name)
        except _switchboard.HubError as exc:
            self._back_off(now, exc.retry_after)
            return
        if not pair.checked:
            self._back_off(now, pair.retry_after)
            return
        self._failures, self._retry_at = 0, 0.0
        self.pair = pair
        if record is None:
            return
        self._follow(name, record, pair)
        if cause != _TOUCH_FAILED:
            self._wanted.add(_TOUCH)

    def _back_off(self, now: float, retry_after: float | None) -> None:
        """A resolve failed: the next waits, ``retry_after`` when the hub said so, else a delay
        that doubles with each failure up to :data:`BACKOFF_MOST`, plus up to a quarter more."""
        self._failures += 1
        delay = min(BACKOFF_MOST, BACKOFF_FIRST * 2 ** min(self._failures - 1, 16))
        if retry_after is not None:
            delay = retry_after
        else:
            delay += delay * secrets.randbelow(1001) / 4000
        self._retry_at = now + delay
        self._want_resolve("hub error")

    def _follow(self, name: str, record: rooms.Record, pair: _switchboard.Pair) -> None:
        """A generation of pair:<name> other than the one last followed is a move, a heal or a
        lapse, either of which counts as a suspected leak: every room held over the pair is
        abandoned, before the new generation is recorded."""
        if record.generation == pair.generation:
            return
        if record.generation is not None:
            self.audit.write(
                "pair", event="moved", pair=name, was=record.generation, now=pair.generation
            )
            # A room an accept kept before a stop interrupted it was accepted over the pair as
            # it was before the move: it is abandoned with the others, never completed.
            self._adopt_accepting(record)
            record.accepting = None
            self._keep(record)
            for room in record.in_use():
                self._abandon(name, record, room, rooms.MOVED)
            self._digest(name, record)
        record.generation = pair.generation
        self._keep(record)

    def _touch_if_due(self, name: str, record: rooms.Record) -> None:
        now = self.link.now()
        if not (
            _TOUCH in self._wanted
            or self._last_touch is None
            or now - self._last_touch >= TOUCH_EVERY
            # Or at once when the wall clock was set back since the last touch: the rhythm then
            # counts from this one, rather than waiting for the clock to catch up.
            or self._last_touch - now > SET_BACK
        ):
            return
        self._wanted.discard(_TOUCH)
        self._last_touch = now
        try:
            touched: _switchboard.Touch | None = self.link.touch(name)
        except (_switchboard.HubError, _switchboard.RoomError):
            touched = None
        self._absorb(name, record, self.link.notes() if touched is None else touched.notes)
        if touched is not None:
            self.skewed = touched.skewed or (self.skewed and not touched.reached)
        if touched is None or not touched.reached:
            if touched is not None and touched.state == "gone":
                # The library holds the pair no more (it dropped it within the touch), or found it
                # over: the resolve that says which is made in this step, under this lock, whatever
                # back-off is owed. For a pair no longer held it makes no hub call.
                self._retry_at = 0.0
            # The resolve this asks for is followed by no touch: the next is on the rhythm.
            self._want_resolve(_TOUCH_FAILED)
            self._digest(name, record)
            return
        found = self.gate(name)
        if found is not None and found.touched_at is None:
            self._save(replace(found, touched_at=now))
        # A move of pair:<name> that another command followed first (`pair status` resolves
        # too) is already in the store: this touch then moves nothing, and no note says so. The
        # generation the store keeps does.
        kept = self.link.kept(name)
        if kept is None or kept.generation != record.generation:
            self._want_resolve("moved")
        self._wanted.add(_JUDGE)
        if self._retry:
            self._retry = False
            self._wanted.add(_CONSIDER)
        self._digest(name, record)

    def _judge_if_wanted(self, name: str, record: rooms.Record) -> None:
        if _JUDGE not in self._wanted:
            return
        self._wanted.discard(_JUDGE)
        for room, held in sorted(record.held.items()):
            if held.abandoned is not None:
                self._leave(name, record, room, held.abandoned)
        if record.accepting is not None:
            self._complete(name, record)
        for room in record.in_use():
            self._check(name, record, room)
        self._digest(name, record)

    # --- notes -------------------------------------------------------------------------

    def _absorb(
        self, name: str, record: rooms.Record, notes: tuple[_switchboard.ShareNote, ...]
    ) -> None:
        """Write down every offer the notes report, before anything is decided; queue the rest
        for :meth:`_digest`."""
        recorded = False
        for note in notes:
            if note.kind != "offered":
                self._notes.append(note)
            elif note.room and note.pair != name:
                self.audit.write(
                    "offer", event="refused", offer=sanitize(note.room), check=rooms.CHECK_PAIR
                )
            elif note.room and note.room not in record.offers:
                record.offers[note.room] = self.link.now()
                recorded = True
                self.audit.write("offer", event="seen", offer=sanitize(note.room))
        if recorded:
            self._keep(record)
            self._wanted.add(_CONSIDER)

    def _digest(self, name: str, record: rooms.Record) -> None:
        """Act on the queued notes, including those that acting on them adds."""
        if self._digesting:
            return
        self._digesting = True
        try:
            while self._notes:
                self._take(name, record, self._notes.popleft())
        finally:
            self._digesting = False

    def _take(self, name: str, record: rooms.Record, note: _switchboard.ShareNote) -> None:
        if note.pair is not None and note.pair != name:
            return
        if note.kind in ("left", "withdrawn"):
            if note.room in record.offers:
                del record.offers[note.room]
                self._keep(record)
                self.audit.write("offer", event="dropped", offer=sanitize(note.room), why=note.kind)
                self._wanted.add(_CONSIDER)
            if note.kind == "left" and note.room in record.held:
                # Judged by the room's list, which the library has taken the pair off.
                self._suspect[note.room] = rooms.LUCILLE_LEFT
                self._wanted.add(_JUDGE)
        elif note.kind in ("pair_moved", "pair_gone"):
            self._want_resolve(note.kind)
        else:
            self.audit.write(
                "share", event=note.kind, room=sanitize(note.room) if note.room else None
            )

    # --- rooms held ----------------------------------------------------------------------

    def check_room(self, room: str) -> _switchboard.Room | None:
        """The room held here as ``room``, if the host may read and post in it now: it passes
        checks 3 and 4, and its list names the pair, which is judged only once a touch has run
        since the pair was made. A room that fails is left here."""
        name = self._name or self._require()
        with self._record_lock():
            found = self._serving(name)
            if found is None or found.opened_at is None:
                return None
            record = self._bound(name, found)
            view = self._check(name, record, room)
            self._digest(name, record)
        return view

    def usable(self) -> list[_switchboard.Room]:
        """Every room the host may read and post in now, each checked as :meth:`check_room`
        checks it."""
        name = self._name or self._require()
        with self._record_lock():
            found = self._serving(name)
            if found is None or found.opened_at is None:
                return []
            record = self._bound(name, found)
            views = [self._check(name, record, room) for room in record.in_use()]
            self._digest(name, record)
        return [view for view in views if view is not None]

    def _check(self, name: str, record: rooms.Record, room: str) -> _switchboard.Room | None:
        held = record.held.get(room)
        found = self.gate(name)
        if held is None or held.abandoned is not None or found is None:
            return None
        try:
            view = self.link.room(room)
        except _switchboard.RoomError as exc:
            if exc.state in _NOT_HELD:
                # Gone from the share store without this host leaving it: never taken back.
                self.audit.write("room", event="gone", room=room)
                del record.held[room]
                record.left[held.workspace] = rooms.Left(room, rooms.GONE, self.link.now())
                self._keep(record)
            return None
        failed = rooms.failed_check(
            url=view.url,
            carries_token=view.carries_token,
            workspace=view.workspace,
            hub=found.hub,
            record=record,
        )
        if failed is not None:
            self._refuse(name, record, room, failed)
            return None
        if found.touched_at is None:
            return None
        if name not in view.pairs:
            self._abandon(name, record, room, self._suspect.pop(room, rooms.NOT_LISTED))
            return None
        self._suspect.pop(room, None)
        return view

    def _refuse(self, name: str, record: rooms.Record, room: str, check: str) -> None:
        """A room that fails check 3 or 4, or that this computer left before: left at once, and
        never read or posted in."""
        self.audit.write("room", event="refused", room=room, check=check)
        self._drop(name, record, room, check)
        if not record.in_use():
            record.no_room = check
            self._keep(record)

    def _abandon(self, name: str, record: rooms.Record, room: str, why: str) -> None:
        """Record that the room is abandoned, stop what it was doing, and leave it."""
        held = record.held.get(room)
        if held is None:
            return
        if held.abandoned is None:
            self.audit.write("room", event="abandoned", room=room, why=why)
        self._drop(name, record, room, why)

    def _drop(self, name: str, record: rooms.Record, room: str, why: str) -> None:
        self._leave(name, record, room, self._mark(record, room, why))

    def _mark(self, record: rooms.Record, room: str, why: str) -> str:
        """Mark the room abandoned in its record, unless it is already, and then stop what it was
        doing; returns why it is abandoned. ``run`` reads the mark with no lock, whichever command
        made it, and stops its work in the room there and then (``loop.py``)."""
        held = record.held[room]
        if held.abandoned is None:
            held.abandoned = why
            self._keep(record)
            if self.stop_room is not None:
                self.stop_room(room)
        return held.abandoned

    def _leave(self, name: str, record: rooms.Record, room: str, why: str) -> bool:
        """Leave the room; whether it is gone from the share store. A room that could not be
        left stays held, marked abandoned, and its leave is tried again."""
        held = record.held.get(room)
        if held is None:
            return True
        try:
            left = self.link.leave_room(room)
        except _switchboard.RoomError as exc:
            self._absorb(name, record, self.link.notes())
            if exc.state != "unknown":
                self.audit.write("room", event="leave_failed", room=room, state=exc.state)
                return False
            told = False
        except _switchboard.HubError:
            self._absorb(name, record, self.link.notes())
            self.audit.write("room", event="leave_failed", room=room, state="hub")
            return False
        else:
            self._absorb(name, record, left.notes)
            self.skewed = self.skewed or left.skewed
            told = left.told
        del record.held[room]
        record.left[held.workspace] = rooms.Left(room=room, why=why, at=self.link.now())
        self._keep(record)
        self.audit.write("room", event="left", room=room, why=why, told=told)
        # A refusal by check 5 is not final: with a room fewer, an offer may now be taken.
        self._wanted.add(_CONSIDER)
        return True

    def _leave_all(self, name: str) -> None:
        """Before a pair is forgotten: leave every room held, the one an interrupted accept may
        have kept included. Raises :class:`Stopped` if one cannot be left. A record of rooms that
        names another pair this profile holds is that pair's, made since: nothing in it is left.
        (Rooms recorded under a pair no longer held are left with this one: their keys must not
        stay behind either.)"""
        record = rooms.load(self.dirs.rooms_file)
        if record.pair is not None and record.pair != name and record.pair in self.held():
            self._notes.clear()
            return
        self._adopt_accepting(record)
        # Every room is marked abandoned before the first leave, which is a hub call: a `run`
        # beside this stops its work in each of them at once, not after the leaves.
        marked: dict[str, str] = {}
        for room, held in sorted(record.held.items()):
            if held.abandoned is None:
                self.audit.write("room", event="abandoned", room=room, why=rooms.PAIR_OVER)
            marked[room] = self._mark(record, room, rooms.PAIR_OVER)
        for room, why in marked.items():
            self._leave(name, record, room, why)
        self._notes.clear()
        if record.held:
            raise Stopped(
                "error",
                f"pair {name} is kept on this computer: a work room held over it could not be "
                "left, and its keys would stay behind; try again",
            )
        if record != rooms.Record(left=record.left, named=record.named):
            self._keep(rooms.Record(left=record.left, named=record.named))

    def _adopt_accepting(self, record: rooms.Record) -> None:
        """Hold, like any other, the room an accept kept before a stop interrupted it, if the
        share store holds it; the caller is about to leave it unjudged."""
        accepting = record.accepting
        if accepting is None or accepting.room in record.held:
            return
        try:
            view = self.link.room(accepting.room)
        except _switchboard.RoomError:
            return
        record.held[accepting.room] = rooms.Held(view.workspace, self.link.now())

    # --- offers --------------------------------------------------------------------------

    def consider_offers(self) -> None:
        """Consider the offers written down, by checks 1, 2 and 5; accept one that passes."""
        name = self._name or self._require()
        with self._record_lock():
            found = self._serving(name)
            if found is not None and found.opened_at is not None:
                record = self._bound(name, found)
            else:
                record = rooms.load(self.dirs.rooms_file)
            self._consider(name, record)

    def _consider(self, name: str, record: rooms.Record) -> None:
        self._wanted.discard(_CONSIDER)
        found = self.gate(name)
        if found is None or found.opened_at is None:
            if record.offers:
                self.audit.write(
                    "offer", event="refused", offers=len(record.offers), check=rooms.CHECK_GATE
                )
            return
        # An offer the share store knows nothing of any more was withdrawn or left while a note
        # saying so was lost: it must not keep every other offer waiting.
        for offer in sorted(record.offers):
            try:
                self.link.room(offer)
            except _switchboard.RoomError as exc:
                if exc.state == "unknown":
                    del record.offers[offer]
                    self._keep(record)
                    self.audit.write("offer", event="dropped", offer=sanitize(offer), why="unknown")
        if not record.offers:
            return
        if len(record.offers) > 1:
            offers = [sanitize(offer) for offer in sorted(record.offers)]
            self.audit.write("offer", event="several_pending", offers=offers)
            return
        (offer,) = record.offers
        over = self._over_pair(name, record)
        if len(over) >= rooms.CAP or len(record.held) >= rooms.CAP:
            self.audit.write("offer", event="refused", offer=sanitize(offer), check=rooms.CHECK_CAP)
            return
        local = rooms.local_name(record)
        record.accepting = rooms.Accepting(offer=offer, room=local, replaces=tuple(over))
        self._keep(record)
        self.audit.write(
            "room", event="accepting", offer=sanitize(offer), room=local, replaces=list(over)
        )
        try:
            taken = self.link.accept_room(name, offer, local)
        except _switchboard.RoomError as exc:
            # What the accept read before it refused: a new offer, or the pair moving.
            self._absorb(name, record, self.link.notes())
            self.skewed = self.skewed or exc.skewed
            self._not_accepted(record, offer, exc.state)
        except _switchboard.HubError:
            self._absorb(name, record, self.link.notes())
            self._not_accepted(record, offer, "hub")
        else:
            self.skewed = self.skewed or taken.skewed
            if taken.name != local:
                record.accepting = replace(record.accepting, room=taken.name)
                self._keep(record)
            self._absorb(name, record, taken.notes)
        self._complete(name, record)
        self._digest(name, record)

    def _over_pair(self, name: str, record: rooms.Record) -> list[str]:
        """The rooms held and in use whose list names the pair."""
        over = []
        for room in record.in_use():
            try:
                view = self.link.room(room)
            except _switchboard.RoomError:
                continue
            if name in view.pairs:
                over.append(room)
        return over

    def _not_accepted(self, record: rooms.Record, offer: str, why: str) -> None:
        self.audit.write("offer", event="not_accepted", offer=sanitize(offer), state=why)
        if why in ("withdrawn", "unknown"):
            record.offers.pop(offer, None)
            self._keep(record)
            self._wanted.add(_CONSIDER)
        elif why == "name_taken":
            # Another name is chosen when the offer is considered again.
            self._retry = True
        elif why != "ambiguous":
            # The hub or the pair's room could not be read, or the pair is not usable: find out
            # which, and try the offer again once a touch reads the pair's room.
            self._retry = True
            self._want_resolve("accept")

    def _complete(self, name: str, record: rooms.Record) -> None:
        """Judge the room an accept kept, by checks 3 and 4 (and whether it was left before). A
        room that fails is left at once and replaces nothing; one that passes replaces the rooms
        recorded before the accept."""
        accepting = record.accepting
        found = self.gate(name)
        if accepting is None or found is None or found.opened_at is None:
            return
        try:
            view = self.link.room(accepting.room)
        except _switchboard.RoomError as exc:
            if exc.state in _NOT_HELD:
                # The accept kept nothing.
                record.accepting = None
                self._keep(record)
            return
        if accepting.room not in record.held:
            record.held[accepting.room] = rooms.Held(view.workspace, self.link.now())
        record.offers.pop(accepting.offer, None)
        self._keep(record)
        failed = rooms.failed_check(
            url=view.url,
            carries_token=view.carries_token,
            workspace=view.workspace,
            hub=found.hub,
            record=record,
        )
        if failed is not None:
            record.accepting = None
            self._keep(record)
            self._refuse(name, record, accepting.room, failed)
            self._wanted.add(_CONSIDER)
            return
        self.audit.write("room", event="accepted", room=accepting.room)
        record.no_room = None
        self._keep(record)
        for old in accepting.replaces:
            if old != accepting.room:
                self._abandon(name, record, old, rooms.REPLACED)
        record.accepting = None
        self._keep(record)
        self._wanted.add(_CONSIDER)


def open_session(
    dirs: paths.Dirs,
    config: state.HostConfig,
    *,
    say: Callable[[str], None],
    warn: Callable[[str], None],
) -> Session:
    """Check that the host may start here, then open the profile's stores."""
    state.startup_checks()
    opened = _switchboard.open_link(dirs, config)
    if opened.warning:
        warn(opened.warning)
    return Session(dirs, opened, say=say)
