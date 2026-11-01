"""What ``lucille-host status`` shows: the profile's own files and store, read with no hub call.

Each is a line ``label: ...``, with the state word first after the label where there is a state to
name (``pair: confirmed: ...``, ``clock: skewed: ...``):

- the version;
- the local policy: ``ok`` and where it is, or ``invalid`` and why; the operations it enables, any
  it holds disabled (an upgrade changed one, or this computer cannot run it), and the folder
  aliases it holds;
- whether the local controls hold the host paused;
- where the pair is kept (the OS keyring, or files under ``--store-file``), and the hub: Lucille's,
  and the loopback hub ``pair --hub`` named, when one is configured;
- the pair, in the words ``pair status`` uses (:meth:`link.Session.pair_state`), from this
  computer's store and the gate's record: never the SAS, which ``pair status`` shows;
- each work room held, or ``none``, with the check its last room failed when the host left that
  room on its own (spec/PROTOCOL.md section 9), and how many offers wait; with no pair held, the
  room an accept a stop interrupted may have kept is named too, since ``forget`` leaves it;
- what the latest ``lucille-host run`` wrote in ``state/status.json``: whether it runs, when it
  last reached the hub, and the clock. The clock is ``ok`` (with the skew measured on the last reply
  posted); ``skewed`` when the hub refused a write of the host's because this computer's clock is
  more than :data:`_switchboard.SKEW_WINDOW` seconds from its own (a reply it posted, or a write the
  library made for it in a touch, an accept or a leave), or when a skew past that was measured; or
  ``unknown`` before any reply was posted. A record of ``run``'s that is older than
  :data:`STALE_AFTER` was left by a ``run`` that stopped without saying so.

A file of the host's own that holds something else stops the command, as it stops every other verb.
A store that cannot be read does not: the pair is reported as ``unreadable``, and the rest as it is,
so that a shell learns whether the host is paused even where the keyring cannot be used.

The lines and the ``--json`` object (``status``, spec/CLI.md section 6) are made together, from the
same values read once (:func:`collect`), so that they cannot disagree.
"""

from __future__ import annotations

import time
from typing import Any, Final

from lucille_host import __version__, _switchboard, link, loop, paths, policy, rooms, state, store
from lucille_host.controls import Controls

__all__ = ["STALE_AFTER", "collect", "run_record", "snapshot", "status", "unreadable"]

# A running ``run`` writes its record at least every minute: one older than this was left by a
# ``run`` that stopped without saying so.
STALE_AFTER: Final = 3 * loop.STATUS_EVERY

# Why a room was abandoned, or left, as ``--json`` names it: a word to branch on, where the text
# says it in words.
WHY: Final = rooms.WORDS

_RECORD_KEYS: Final = frozenset(
    {
        "v",
        "running",
        "pid",
        "written_at",
        "last_contact",
        "skew",
        "skewed",
        "reading",
        "read_stuck",
    }
)


# The largest process id the host's systems give.
_PID_MOST: Final = 2**31 - 1


def _skew(value: Any) -> bool:
    """A skew in seconds: a number, not a boolean, never NaN or infinite."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and -state.LATEST <= value <= state.LATEST
    )


def _pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _PID_MOST


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def run_record(dirs: paths.Dirs) -> dict[str, Any] | None:
    """What the latest ``run`` wrote in ``state/status.json``; None when it wrote nothing. A file
    that holds something else, a value ``run`` never writes included (a pid out of range, a time
    that is not one, a skew that is NaN), is a :class:`state.StateError`."""
    data = state.read_json(dirs.status_file)
    if data is None:
        return None
    valid = (
        isinstance(data, dict)
        and set(data) == _RECORD_KEYS
        and data["v"] == 1
        and isinstance(data["running"], bool)
        and _pid(data["pid"])
        and state.valid_time(data["written_at"])
        and (data["last_contact"] is None or state.valid_time(data["last_contact"]))
        and (data["skew"] is None or _skew(data["skew"]))
        and isinstance(data["skewed"], bool)
        and _names(data["reading"])
        and _names(data["read_stuck"])
    )
    if not valid:
        raise state.StateError(f"{dirs.status_file} is not a lucille-host run record")
    return data


def _policy(dirs: paths.Dirs) -> tuple[list[str], dict[str, Any]]:
    try:
        current = policy.load(dirs.policy_file)
    except policy.PolicyError as exc:
        return [f"policy: invalid: {exc}"], {"state": "invalid", "error": str(exc)}
    exists = dirs.policy_file.exists()
    where = dirs.policy_file if exists else "no file yet: the fresh policy"
    eff = policy.effective(current, paths.os_family())
    lines = [f"policy: ok ({where})", "enabled: " + (", ".join(eff.enabled_ops) or "none")]
    lines += [
        f"disabled: {name}: an upgrade changed it; `lucille-host ops enable {name}` accepts it"
        for name in eff.changed_by_upgrade
    ]
    lines += [f"disabled: {name}: not on this computer" for name in eff.unavailable]
    lines += [f"disabled: {name}: not in this release's catalog" for name in eff.not_in_catalog]
    folders = sorted(eff.roots)
    lines.append("folders: " + (", ".join(folders) or "none"))
    disabled = [
        {"name": name, "why": why}
        for names, why in (
            (eff.changed_by_upgrade, "disabled_by_upgrade"),
            (eff.unavailable, "unavailable"),
            (eff.not_in_catalog, "not_in_catalog"),
        )
        for name in names
    ]
    found = {
        "state": "ok",
        "file": str(dirs.policy_file) if exists else None,
        "enabled": list(eff.enabled_ops),
        "disabled": disabled,
        "folders": folders,
    }
    return lines, found


def _rooms(
    dirs: paths.Dirs, live: dict[str, Any] | None, paired: bool | None
) -> tuple[list[str], dict[str, Any]]:
    """The work room lines, and the fields of the object they make. ``paired`` is None when the
    store that keeps the pair could not be read: no room is then said to be over a pair gone."""
    record = rooms.load(dirs.rooms_file)
    reading = set(live["reading"]) if live is not None else set()
    stuck = set(live["read_stuck"]) if live is not None else set()
    gone = paired is False
    # With no pair held, nothing tries a leave again on its own: `forget` leaves what is left.
    again = "`lucille-host forget` leaves it" if gone else "its leave is tried again"
    lines = []
    held_rooms: list[dict[str, Any]] = []
    for name, held in sorted(record.held.items()):
        accepted = link.iso(held.accepted_at)
        notes = [f"accepted {accepted}"]
        room: dict[str, Any] = {"name": name, "accepted_at": accepted}
        if held.abandoned is not None:
            notes.append(f"abandoned ({held.abandoned}): nothing is taken from it, and {again}")
            room = {"state": "abandoned", **room, "why": WHY[held.abandoned]}
        elif gone:
            # Left by a process that stopped after the library dropped the pair, before it left
            # the pair's rooms.
            notes.append(
                "the pair it was taken over is gone from this computer: nothing is taken from it, "
                f"and {again}"
            )
            room = {"state": "pair_gone", **room}
        elif name in stuck:
            notes.append(
                "read_stuck: a message there cannot be opened, and `run` reads no further there"
            )
            room = {"state": "read_stuck", **room}
        elif name in reading:
            notes.append("read by `run`")
            room = {"state": "reading", **room}
        else:
            room = {"state": "not_read", **room}
        lines.append(f"work room: {name} ({'; '.join(notes)})")
        held_rooms.append(room)
    kept = record.accepting
    left_because = None
    if kept is not None and kept.room not in record.held and gone:
        # An accept a stop interrupted, and then the library dropped the pair: no start will judge
        # that room now, and `forget` leaves it, if the accept kept it.
        lines.append(
            f"work room: {kept.room} (an accept a stop interrupted may have kept it; the pair it "
            f"was taken over is gone from this computer: nothing is taken from it, and {again})"
        )
        held_rooms.append({"state": "accept_interrupted", "name": kept.room})
    elif not record.held:
        line = "work room: none: this computer holds no work room"
        if record.no_room is not None:
            line += (
                f"; it left its last one on its own: {record.no_room}: "
                f"{rooms.CHECKS[record.no_room]}"
            )
            left_because = WHY[record.no_room]
        lines.append(line)
    if record.offers:
        count = len(record.offers)
        lines.append(
            f"offers: {count} waiting"
            + (", and none is taken while more than one waits" if count > 1 else "")
        )
    found = {
        "work_rooms": held_rooms,
        "last_left_because": left_because,
        "offers_waiting": len(record.offers),
    }
    return lines, found


def _clock(seen: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    window = _switchboard.SKEW_WINDOW
    found = loop.clock_view(
        None if seen is None else seen["skew"], seen is not None and seen["skewed"]
    )
    if found["state"] == "skewed":
        return (
            f"clock: skewed: this computer's clock is more than {window} s from the hub's, so the "
            "hub refuses what the host writes; set the clock right"
        ), found
    if found["state"] == "unknown":
        return "clock: unknown: `run` measures it against the hub on each reply it posts", found
    skew = found["skew_s"]
    side = "behind" if skew > 0 else "ahead of"
    return (
        f"clock: ok: {abs(skew):.1f} s {side} the hub's, on the last reply posted (the hub "
        f"refuses posts past {window} s)"
    ), found


def _run(seen: dict[str, Any] | None, live: bool) -> tuple[list[str], dict[str, Any]]:
    clock, clock_found = _clock(seen)
    if seen is None:
        return [
            "run: no record: `lucille-host run` writes one while it runs",
            "last hub contact: none recorded",
            clock,
        ], {"run": {"state": "none"}, "last_hub_contact": None, "clock": clock_found}
    at = link.iso(seen["written_at"])
    if live:
        line = f"run: running (pid {seen['pid']}), as of {at}"
        run = {"state": "running", "pid": seen["pid"], "as_of": at}
    elif seen["running"]:
        line = f"run: not seen since {at} (pid {seen['pid']}): it stopped without saying so"
        run = {"state": "not_seen", "pid": seen["pid"], "as_of": at}
    else:
        line = f"run: stopped at {at}"
        run = {"state": "stopped", "as_of": at}
    contact = seen["last_contact"]
    contact_at = link.iso(contact) if contact is not None else None
    return [
        line,
        f"last hub contact: {contact_at or 'none recorded'}",
        clock,
    ], {"run": run, "last_hub_contact": contact_at, "clock": clock_found}


def unreadable(exc: BaseException) -> tuple[str, str] | None:
    """The code and the sentence for a store that cannot be read, which ``status`` reports in its
    ``pair`` rather than stopping; None for any other error."""
    if isinstance(exc, store.InsecureKeyring):
        return "insecure_keyring", str(exc)
    if isinstance(exc, store.StoreError):
        return "store_error", exc.strerror or str(exc)
    if isinstance(exc, _switchboard.PairFailed):
        return "pair_unusable", f"the pair cannot be used here ({exc.state})"
    return None


def _pair(view: link.PairView) -> dict[str, Any]:
    found: dict[str, Any] = {"state": view.word}
    if view.word == "several_pairs":
        found["message"] = view.message
        return found
    if view.word != "none":
        found["name"] = view.name
    if view.word in ("sas_pending", "expired"):
        found["deadline"] = link.iso(view.deadline or 0)
    if view.word == "confirmed":
        found["generation"] = view.generation
    return found


def collect(
    dirs: paths.Dirs,
    session: link.Session | None,
    config: state.HostConfig,
    *,
    failure: BaseException | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """The lines ``lucille-host status`` prints, and the ``status`` object of ``--json``, from the
    same values. ``session`` is None when the store could not be opened (``failure`` says why).
    Makes no hub call."""
    lines = [f"lucille-host {__version__}"]
    policy_lines, policy_found = _policy(dirs)
    lines += policy_lines
    paused = Controls(dirs.state).paused()
    lines.append(
        "controls: paused: nothing starts until `lucille-host resume`"
        if paused
        else "controls: not paused"
    )
    if config.store == "file":
        lines.append(f"store: files in {dirs.store_files}, unencrypted: weaker than the OS keyring")
    else:
        lines.append("store: the OS keyring")
    hub = f"hub: {_switchboard.LUCILLE_HUB}"
    if config.hub is not None:
        hub += f", and the loopback hub {config.hub} that `lucille-host pair --hub` named"
    lines.append(hub)
    paired: bool | None = None
    view: link.PairView | None = None
    cannot = None
    if session is None:
        cannot = unreadable(failure) if failure is not None else None
        if cannot is None:
            raise ValueError("status needs the session, or the store error that kept it shut")
    else:
        try:
            view = session.pair_view()
            paired = bool(session.held())
        except (store.StoreError, _switchboard.PairFailed) as exc:
            cannot = unreadable(exc)
    pair_found: dict[str, Any]
    if view is None or cannot is not None:
        code, message = cannot or ("store_error", "")
        lines.append(f"pair: unreadable: {message}")
        pair_found = {"state": "unreadable", "code": code, "message": message}
    else:
        word, detail = link.pair_words(view)
        lines.append(f"pair: {word}: {detail}")
        pair_found = _pair(view)
    seen = run_record(dirs)
    now = session.link.now() if session is not None else time.time()
    live = seen is not None and seen["running"] and now - seen["written_at"] <= STALE_AFTER
    room_lines, rooms_found = _rooms(dirs, seen if live else None, paired)
    lines += room_lines
    run_lines, run_found = _run(seen, live)
    lines += run_lines
    found = {
        "host_version": __version__,
        "policy": policy_found,
        "paused": paused,
        "store": config.store,
        "hub": _switchboard.LUCILLE_HUB,
        "loopback_hub": config.hub,
        "pair": pair_found,
        **rooms_found,
        "run": run_found["run"],
        "last_hub_contact": run_found["last_hub_contact"],
        "clock": run_found["clock"],
    }
    return lines, found


def status(dirs: paths.Dirs, session: link.Session, config: state.HostConfig) -> list[str]:
    """The lines ``lucille-host status`` prints. Makes no hub call."""
    return collect(dirs, session, config)[0]


def snapshot(dirs: paths.Dirs, session: link.Session, config: state.HostConfig) -> dict[str, Any]:
    """The ``status`` object of ``lucille-host status --json``. Makes no hub call."""
    return collect(dirs, session, config)[1]
