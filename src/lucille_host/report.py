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
  room on its own (spec/PROTOCOL.md section 9), and how many offers wait;
- what the latest ``lucille-host run`` wrote in ``state/status.json``: whether it runs, when it
  last reached the hub, and the clock. The clock is ``ok`` (with the skew measured on the last reply
  posted); ``skewed`` when the hub refused a write of the host's because this computer's clock is
  more than :data:`_switchboard.SKEW_WINDOW` seconds from its own (a reply it posted, or a write the
  library made for it in a touch, an accept or a leave), or when a skew past that was measured; or
  ``unknown`` before any reply was posted. A record of ``run``'s that is older than
  :data:`STALE_AFTER` was left by a ``run`` that stopped without saying so.

A file of the host's own that holds something else stops the command, as it stops every other verb.
"""

from __future__ import annotations

from typing import Any, Final

from lucille_host import __version__, _switchboard, link, loop, paths, policy, rooms, state
from lucille_host.controls import Controls

__all__ = ["STALE_AFTER", "run_record", "status"]

# A running ``run`` writes its record at least every minute: one older than this was left by a
# ``run`` that stopped without saying so.
STALE_AFTER: Final = 3 * loop.STATUS_EVERY

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


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def run_record(dirs: paths.Dirs) -> dict[str, Any] | None:
    """What the latest ``run`` wrote in ``state/status.json``; None when it wrote nothing. A file
    that holds something else is a :class:`state.StateError`."""
    data = state.read_json(dirs.status_file)
    if data is None:
        return None
    valid = (
        isinstance(data, dict)
        and set(data) == _RECORD_KEYS
        and data["v"] == 1
        and isinstance(data["running"], bool)
        and isinstance(data["pid"], int)
        and _number(data["written_at"])
        and (data["last_contact"] is None or _number(data["last_contact"]))
        and (data["skew"] is None or _number(data["skew"]))
        and isinstance(data["skewed"], bool)
        and _names(data["reading"])
        and _names(data["read_stuck"])
    )
    if not valid:
        raise state.StateError(f"{dirs.status_file} is not a lucille-host run record")
    return data


def _policy(dirs: paths.Dirs) -> list[str]:
    try:
        current = policy.load(dirs.policy_file)
    except policy.PolicyError as exc:
        return [f"policy: invalid: {exc}"]
    where = dirs.policy_file if dirs.policy_file.exists() else "no file yet: the fresh policy"
    eff = policy.effective(current, paths.os_family())
    lines = [f"policy: ok ({where})", "enabled: " + (", ".join(eff.enabled_ops) or "none")]
    lines += [
        f"disabled: {name}: an upgrade changed it; `lucille-host ops enable {name}` accepts it"
        for name in eff.changed_by_upgrade
    ]
    lines += [f"disabled: {name}: not on this computer" for name in eff.unavailable]
    lines += [f"disabled: {name}: not in this release's catalog" for name in eff.not_in_catalog]
    lines.append("folders: " + (", ".join(sorted(eff.roots)) or "none"))
    return lines


def _rooms(dirs: paths.Dirs, live: dict[str, Any] | None) -> list[str]:
    record = rooms.load(dirs.rooms_file)
    reading = set(live["reading"]) if live is not None else set()
    stuck = set(live["read_stuck"]) if live is not None else set()
    lines = []
    for name, held in sorted(record.held.items()):
        notes = [f"accepted {link.iso(held.accepted_at)}"]
        if held.abandoned is not None:
            notes.append(
                f"abandoned ({held.abandoned}): nothing is taken from it, and its leave is tried "
                "again"
            )
        elif name in stuck:
            notes.append(
                "read_stuck: a message there cannot be opened, and `run` reads no further there"
            )
        elif name in reading:
            notes.append("read by `run`")
        lines.append(f"work room: {name} ({'; '.join(notes)})")
    if not record.in_use():
        line = "work room: none: this computer holds no work room"
        if record.no_room is not None:
            line += (
                f"; it left its last one on its own: {record.no_room}: "
                f"{rooms.CHECKS[record.no_room]}"
            )
        lines.append(line)
    if record.offers:
        count = len(record.offers)
        lines.append(
            f"offers: {count} waiting"
            + (", and none is taken while more than one waits" if count > 1 else "")
        )
    return lines


def _clock(seen: dict[str, Any] | None) -> str:
    window = _switchboard.SKEW_WINDOW
    skew = None if seen is None else seen["skew"]
    if seen is not None and (seen["skewed"] or (skew is not None and abs(skew) > window)):
        return (
            f"clock: skewed: this computer's clock is more than {window} s from the hub's, so the "
            "hub refuses what the host writes; set the clock right"
        )
    if skew is None:
        return "clock: unknown: `run` measures it against the hub on each reply it posts"
    side = "behind" if skew > 0 else "ahead of"
    return (
        f"clock: ok: {abs(skew):.1f} s {side} the hub's, on the last reply posted (the hub "
        f"refuses posts past {window} s)"
    )


def _run(seen: dict[str, Any] | None, live: bool) -> list[str]:
    if seen is None:
        return [
            "run: no record: `lucille-host run` writes one while it runs",
            "last hub contact: none recorded",
            _clock(None),
        ]
    at = link.iso(seen["written_at"])
    if live:
        line = f"run: running (pid {seen['pid']}), as of {at}"
    elif seen["running"]:
        line = f"run: not seen since {at} (pid {seen['pid']}): it stopped without saying so"
    else:
        line = f"run: stopped at {at}"
    contact = seen["last_contact"]
    return [
        line,
        f"last hub contact: {link.iso(contact) if contact is not None else 'none recorded'}",
        _clock(seen),
    ]


def status(dirs: paths.Dirs, session: link.Session, config: state.HostConfig) -> list[str]:
    """The lines ``lucille-host status`` prints. Makes no hub call."""
    lines = [f"lucille-host {__version__}"]
    lines += _policy(dirs)
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
    word, detail = session.pair_state()
    lines.append(f"pair: {word}: {detail}")
    seen = run_record(dirs)
    live = (
        seen is not None
        and seen["running"]
        and session.link.now() - seen["written_at"] <= STALE_AFTER
    )
    lines += _rooms(dirs, seen if live else None)
    lines += _run(seen, live)
    return lines
