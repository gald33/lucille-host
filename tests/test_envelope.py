"""The first checks of a message read in a work room, and the journal (spec/PROTOCOL.md §10-§15).

Steps 1 to 4 of the refusal order, and the messages that get no reply, are held here to the
request fixtures, as ``tests/test_catalog.py`` holds steps 5 to 9 to them.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from lucille_host import envelope, journal, spec, state
from lucille_host.journal import Journal
from tests import _elsewhere
from tests._host import lock_held

EARLY = {"host_too_old", "bad_request", "expired"}
FIXTURES = spec.fixture_paths("request")


def _early(fixture: dict[str, Any], tmp_path: Path) -> tuple[str, str | None]:
    """What the host's first steps make of a fixture: ``("none", why)`` for no reply,
    ``("refused", code)`` for steps 2 to 4, or ``("later", None)`` when it goes on to
    ``jobs.check``."""
    host_state = {**spec.default_host_state(), **fixture.get("host_state", {})}
    received = host_state["received_at"]
    sent = fixture["body"]
    why = envelope.ignored(mine=False, kind="note", thread=None, body=sent)
    if why is not None:
        return "none", why
    journal = Journal(tmp_path / "journal.json", clock=lambda: received)
    for seq, seen in enumerate(host_state["seen_job_ids"]):
        journal.received(seen, room="r", seq=seq, received_at=received, not_after=None, body={})
    if journal.seen(sent["job_id"]):
        return "none", "duplicate"
    code = envelope.early_code(
        sent, received_at=received, now=received, host_version=host_state["host_version"]
    )
    return ("refused", code) if code is not None else ("later", None)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_steps_1_to_4_hold_to_the_request_fixtures(path: Path, tmp_path: Path) -> None:
    fixture = spec.load_fixture(path)
    expect = fixture["expect"]["reply"]
    found, detail = _early(fixture, tmp_path)
    if expect is None:
        assert found == "none", detail
    elif expect["status"] == "refused" and expect["code"] in EARLY:
        assert (found, detail) == ("refused", expect["code"])
    else:
        assert found == "later", detail


def test_the_fixtures_cover_steps_1_to_4(tmp_path: Path) -> None:
    seen: set[Any] = set()
    for path in FIXTURES:
        fixture = spec.load_fixture(path)
        folder = tmp_path / path.stem
        folder.mkdir()
        found, detail = _early(fixture, folder)
        seen.add(detail if found != "later" else "later")
    assert seen >= EARLY | {"later", "duplicate", "not_object", "not_request", "bad_job_id"}


def test_messages_the_host_does_not_answer() -> None:
    request = {"dir": "request", "job_id": "00000000-0000-4000-8000-000000000001"}
    assert envelope.ignored(mine=False, kind="note", thread=None, body=request) is None
    assert envelope.ignored(mine=True, kind="note", thread=None, body=request) == "own"
    assert envelope.ignored(mine=False, kind="status", thread=None, body=request) == "not_note"
    assert envelope.ignored(mine=False, kind="note", thread="t", body=request) == "threaded"
    assert envelope.ignored(mine=False, kind="note", thread=None, body=[1]) == "not_object"
    reply = dict(request, dir="reply")
    assert envelope.ignored(mine=False, kind="note", thread=None, body=reply) == "not_request"
    for job_id in ("00000000-0000-4000-8000-00000000000A", "x", 5, None, request["job_id"] + "\n"):
        malformed = dict(request, job_id=job_id)
        assert envelope.ignored(mine=False, kind="note", thread=None, body=malformed) == (
            "bad_job_id"
        ), job_id


def test_older_compares_the_numeric_triple() -> None:
    assert envelope.older("0.1.0.dev0", "0.1.0")
    assert not envelope.older("0.1.0", "0.1.0")
    assert envelope.older("0.9.9", "0.10.0")
    assert not envelope.older("1.0.0", "0.99.99")
    assert envelope.older("1.2", "0.0.0")
    assert envelope.older("1.2.3-beta", "1.2.3")


def test_the_not_after_range_counts_from_the_receipt() -> None:
    received = 1_000_000.0
    sent = {"not_after": int(received) + 86400}
    assert envelope.valid_not_after(sent, received) == int(received) + 86400
    assert envelope.valid_not_after({"not_after": int(received) + 86401}, received) is None
    for bad in (1.0, True, -1, 4294967296, "5", None):
        assert envelope.valid_not_after({"not_after": bad}, received) is None, bad
    assert envelope.valid_not_after({}, received) is None
    assert envelope.valid_not_after([], received) is None
    # A not_after in the past is valid, and draws `expired` once decided.
    base = {
        "v": "lucille.exec/1",
        "dir": "request",
        "job_id": "00000000-0000-4000-8000-000000000001",
        "op": "host.describe",
        "args": {},
    }
    past = dict(base, not_after=int(received) - 1)
    assert envelope.early_code(past, received_at=received, now=received, host_version="1.0.0") == (
        "expired"
    )
    # Expired is judged when the request is decided, not when it was received.
    later = dict(base, not_after=int(received) + 10)
    assert (
        envelope.early_code(later, received_at=received, now=received + 10, host_version="1.0.0")
        is None
    )
    assert envelope.early_code(
        later, received_at=received, now=received + 11, host_version="1.0.0"
    ) == ("expired")


def test_reply_ttl_rules() -> None:
    now = 1_000_000.0
    # (not_after - now) + 900, in whole seconds, never more than a day.
    assert envelope.reply_ttl(now + 100, now) == 1000
    assert envelope.reply_ttl(now + 100.6, now) == 1000
    assert envelope.reply_ttl(now + 86000, now) == 86400
    assert envelope.reply_ttl(now - 899, now) == 1
    # Under 1 s: not posted.
    assert envelope.reply_ttl(now - 899.5, now) is None
    assert envelope.reply_ttl(now - 1000, now) is None
    # With no valid not_after, from the time the request was received: at most 900 s.
    assert envelope.reply_ttl(now - 30, now) == 870


def test_prune_times() -> None:
    received = 1_000_000.0
    assert envelope.prune_at(int(received) + 100, received) == received + 100 + 900
    # Already past its not_after when it arrived: 15 minutes after the receipt.
    assert envelope.prune_at(int(received) - 100, received) == received + 900
    # No valid not_after: a day and 15 minutes after the receipt.
    assert envelope.prune_at(None, received) == received + 86400 + 900


def test_the_journal_forgets_a_job_id_at_its_prune_time(tmp_path: Path) -> None:
    now = [1_000_000.0]
    journal = Journal(tmp_path / "journal.json", clock=lambda: now[0])
    first = "00000000-0000-4000-8000-000000000001"
    other = "00000000-0000-4000-8000-000000000002"
    entry = journal.received(
        first, room="w", seq=3, received_at=now[0], not_after=int(now[0]) + 60, body={"a": 1}
    )
    assert entry.prune_at == now[0] + 60 + 900
    now[0] = entry.prune_at
    journal.received(other, room="w", seq=4, received_at=now[0], not_after=None, body={})
    assert journal.seen(first)
    now[0] += 1
    journal.decided(other, "run", None)
    assert not journal.seen(first) and journal.seen(other)


def test_a_pruned_entry_is_settled(tmp_path: Path) -> None:
    # A reply posted in its TTL's last second, while the worker records another request: the
    # worker's write prunes the entry before the poster records the post, which then stopped run.
    now = [1000.0]
    journal = Journal(tmp_path / "journal.json", clock=lambda: now[0])
    first = "00000000-0000-4000-8000-000000000001"
    other = "00000000-0000-4000-8000-000000000002"
    journal.received(first, room="w", seq=1, received_at=1000.0, not_after=1000, body={})
    journal.decided(first, "run", None)
    now[0] = 1901.0
    journal.received(other, room="w", seq=2, received_at=1901.0, not_after=1950, body={})
    assert not journal.seen(first)
    now[0] = 1903.0
    journal.sent(first)
    journal.decided(first, "refused", b"{}")
    assert not journal.seen(first)
    assert [job for job, _ in journal.pending()] == [other]


def test_the_journal_records_each_step(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.json", clock=lambda: 100.0)
    job = "00000000-0000-4000-8000-000000000001"
    journal.received(job, room="w", seq=7, received_at=100.0, not_after=200, body={"x": 1})
    entry = journal.get(job)
    assert entry is not None and entry.body == {"x": 1} and entry.decision is None
    assert journal.pending("w") == [(job, entry)] and journal.pending("other") == []
    journal.decided(job, "refused", b'{"status":"refused"}')
    entry = journal.get(job)
    assert entry is not None
    assert (entry.decision, entry.reply, entry.body) == ("refused", '{"status":"refused"}', None)
    journal.sent(job)
    entry = journal.get(job)
    assert entry is not None and entry.sent and entry.reply is None
    assert journal.pending() == []
    assert (tmp_path / "journal.json").stat().st_mode & 0o777 == 0o600
    # A request too large to keep is still recorded, without its body.
    big = "00000000-0000-4000-8000-000000000002"
    journal.received(
        big, room="w", seq=8, received_at=100.0, not_after=None, body={"x": "y" * 20000}
    )
    entry = journal.get(big)
    assert entry is not None and entry.body is None
    assert journal.abandon_room("w") == [big]
    assert journal.pending() == []


def test_the_journal_takes_a_job_id_once(tmp_path: Path) -> None:
    taking = Journal(tmp_path / "journal.json", clock=lambda: 100.0)
    job = "00000000-0000-4000-8000-000000000001"
    first = taking.received(job, room="w", seq=1, received_at=100.0, not_after=None, body={})
    assert first is not None
    # The same job id again, from any room: a duplicate, and the first entry stays as it was.
    again = taking.received(job, room="v", seq=9, received_at=101.0, not_after=None, body={})
    assert again is None
    assert taking.get(job) == first


def test_two_processes_never_take_one_job_id(tmp_path: Path) -> None:
    # Each process takes every job id it can, at the same time as the other. A journal whose
    # look and write are two steps, or whose lock keeps out only the threads of one process,
    # lets both take some.
    path = tmp_path / "journal.json"
    jobs = [str(uuid.UUID(int=n, version=4)) for n in range(1, 151)]
    racers = [_elsewhere.start("journal", str(path), *jobs) for _ in range(2)]
    for racer in racers:
        assert racer.stdin is not None
        racer.stdin.write("go\n")
        racer.stdin.flush()
    taken: list[list[str]] = []
    for racer in racers:
        out, err = racer.communicate(timeout=120)
        assert racer.returncode == 0, err
        taken.append(json.loads(out))
    first, second = taken
    assert set(first).isdisjoint(second)
    assert sorted(first + second) == sorted(jobs)
    kept = Journal(path, clock=lambda: 100.0)
    assert all(kept.seen(job) for job in jobs)


def test_the_journal_waits_for_another_process(tmp_path: Path) -> None:
    taking = Journal(tmp_path / "journal.json", clock=lambda: 100.0)
    done = threading.Event()

    def take() -> None:
        taking.received("job", room="w", seq=1, received_at=100.0, not_after=None, body={})
        done.set()

    with lock_held(tmp_path / journal.LOCK):
        thread = threading.Thread(target=take, daemon=True)
        thread.start()
        assert not done.wait(0.5), "the journal was written while another process held its lock"
    assert done.wait(10)
    assert taking.seen("job")


def test_the_journal_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = Journal(path, clock=lambda: 100.0)
    for text in ("[]", '{"v": 2, "jobs": {}}', '{"v": 1, "jobs": {"x": {"room": "w"}}}', "{"):
        path.write_text(text)
        with pytest.raises(state.StateError):
            journal.seen("x")
