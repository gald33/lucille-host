"""The golden fixtures: each validates, or fails, as labelled, and together they cover the spec.

A fixture labels its message in two ways (spec/PROTOCOL.md, "Fixtures"):

- ``schema_valid``: what the JSON Schema alone says;
- ``valid``: whether a conforming sender may send it, by the schema and by the rules the schema
  cannot express. A fixture the schema accepts but a prose rule refuses names that rule in
  ``prose_rule``.

A request fixture also says what the host replies, given its ``host_state``. That is checked here
against a reference reading of the spec's refusal order (``_support.decide``), so that the expected
replies agree with the spec and with each other.

A ``host.describe`` fixture holds one document ``host.describe`` returns, labelled the same way.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from lucille_host import spec
from tests.spec import _support

REQUEST_FIXTURES = spec.fixture_paths("request")
REPLY_FIXTURES = spec.fixture_paths("reply")
DESCRIBE_FIXTURES = spec.fixture_paths("host.describe")
DECODE_FIXTURES = spec.fixture_paths("decode")
FAKE_JOB_ID = re.compile(r"00000000-0000-4000-8000-[0-9]{12}\Z")
# The two fake job ids with a letter in every group, one in a valid request and one in a valid
# reply (spec/PROTOCOL.md, "Fixtures").
HEX_JOB_IDS = ("abcdefab-cdef-4abc-8def-abcdefabcdef", "fedcbafe-dcba-4fed-8cba-fedcbafedcba")
# The order of a fixture's keys: the order of the table in spec/PROTOCOL.md, "Fixtures".
KEY_ORDER = (
    "description",
    "valid",
    "schema_valid",
    "prose_rule",
    "host_state",
    "expect",
    "op",
    "body",
)
# Codes a valid request can never draw: each says the sender broke the spec.
SENDER_FAULTS = {"bad_request", "bad_args", "unknown_op"}


def _ids(paths: list[Path]) -> list[str]:
    return [path.stem for path in paths]


def _all() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        kind: {path.stem: spec.load_fixture(path) for path in spec.fixture_paths(kind)}
        for kind in spec.FIXTURE_KINDS
    }


def test_there_are_fixtures_of_every_kind() -> None:
    assert len(REQUEST_FIXTURES) >= 60
    assert len(REPLY_FIXTURES) >= 30
    assert len(DESCRIBE_FIXTURES) >= 10
    assert len(DECODE_FIXTURES) >= 5


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_ids(REQUEST_FIXTURES))
def test_request_fixture_is_labelled_correctly(path: Path) -> None:
    fixture = spec.load_fixture(path)
    assert _support.label_errors("request", fixture, spec.load_schema("request")) == []


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_ids(REQUEST_FIXTURES))
def test_request_fixture_expects_the_reply_the_spec_gives(path: Path) -> None:
    fixture = spec.load_fixture(path)
    state = _support.host_state(fixture.get("host_state"))
    assert set(fixture.get("host_state", {})) <= set(spec.default_host_state())
    expect = fixture["expect"]
    assert set(expect) <= {"reply", "effective_caps"}
    assert _support.decide(fixture["body"], state) == expect["reply"]
    if "effective_caps" in expect:
        assert expect["reply"] == {"status": "done"}
        assert _support.effective_caps(fixture["body"], state) == expect["effective_caps"]
    if fixture["valid"]:
        reply = expect["reply"]
        if reply is None:
            # The only valid request that gets no reply is a duplicate.
            assert fixture["body"]["job_id"] in state["seen_job_ids"]
        else:
            assert reply.get("code") not in SENDER_FAULTS


@pytest.mark.parametrize("path", REPLY_FIXTURES, ids=_ids(REPLY_FIXTURES))
def test_reply_fixture_is_labelled_correctly(path: Path) -> None:
    fixture = spec.load_fixture(path)
    assert _support.label_errors("reply", fixture, spec.load_schema("reply")) == []


@pytest.mark.parametrize("path", DESCRIBE_FIXTURES, ids=_ids(DESCRIBE_FIXTURES))
def test_host_describe_fixture_is_labelled_correctly(path: Path) -> None:
    fixture = spec.load_fixture(path)
    schema = spec.load_schema("host.describe")
    assert _support.label_errors("host.describe", fixture, schema) == []


def test_the_largest_document_fixture_is_the_largest_the_schema_allows() -> None:
    fixture = _all()["host.describe"]["valid-largest"]
    assert fixture["body"] == _support.largest_host_describe(spec.load_schema("host.describe"))


def test_the_shortest_document_fixture_is_the_shortest_the_schema_allows() -> None:
    fixture = _all()["host.describe"]["valid-shortest"]
    schema = spec.load_schema("host.describe")
    assert fixture["body"] == _support.shortest_host_describe(schema)
    assert fixture["valid"]
    shortest = _support.serialized_size(fixture["body"])
    for other in _all()["host.describe"].values():
        if other["valid"]:
            assert _support.serialized_size(other["body"]) >= shortest


def test_host_describe_reply_carries_its_document_in_stdout() -> None:
    fixtures = [f for f in _all()["reply"].values() if f.get("op") == "host.describe"]
    assert fixtures, "a fixture of host.describe's reply"
    for fixture in fixtures:
        body = fixture["body"]
        assert fixture["valid"]
        assert (body["status"], body["exit_code"], body["stderr"]) == ("done", 0, "")
        assert (body["stdout_truncated"], body["stderr_truncated"]) == (False, False)
        document = json.loads(body["stdout"])
        assert _support.schema_accepts("host.describe", document)
        assert document["protocol"] == spec.PROTOCOL
        assert document["host_version"] == body["host_version"]
        assert document["policy_digest"] == body["policy_digest"]


@pytest.mark.parametrize("path", DECODE_FIXTURES, ids=_ids(DECODE_FIXTURES))
def test_decode_fixture(path: Path) -> None:
    fixture = spec.load_fixture(path)
    assert set(fixture) == {"description", "raw_base64", "text"}
    raw = base64.b64decode(fixture["raw_base64"], validate=True)
    # The spec's decoding: UTF-8 with errors="replace", then U+0000 removed.
    assert raw.decode("utf-8", errors="replace").replace("\x00", "") == fixture["text"]
    assert not _support.bad_string(fixture["text"])


def test_decode_fixtures_cover_non_utf8_and_nul() -> None:
    raws = [base64.b64decode(spec.load_fixture(p)["raw_base64"]) for p in DECODE_FIXTURES]
    assert any(b"\x00" in raw for raw in raws)
    undecodable = []
    for raw in raws:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            undecodable.append(raw)
    assert undecodable


def test_one_valid_reply_per_shape_and_per_code() -> None:
    replies = [f["body"] for f in _all()["reply"].values() if f["valid"]]
    assert {r["status"] for r in replies} == {"done", "refused", "failed"}
    refused = {r["code"] for r in replies if r["status"] == "refused"}
    failed = {r["code"] for r in replies if r["status"] == "failed"}
    assert refused == set(_support.REFUSED_CODES)
    assert failed == set(_support.FAILED_CODES)


def test_one_request_per_operation_and_per_code() -> None:
    requests = list(_all()["request"].values())
    ran = {
        f["body"]["op"]
        for f in requests
        if f["valid"] and f["expect"]["reply"] == {"status": "done"}
    }
    assert ran == set(_support.CATALOG)
    codes = {f["expect"]["reply"].get("code") for f in requests if f["expect"]["reply"]} - {None}
    assert codes == set(_support.REFUSED_CODES) | set(_support.FAILED_CODES)
    assert any(f["expect"]["reply"] is None for f in requests)


def test_the_listed_invalid_requests_are_there() -> None:
    requests = _all()["request"]
    for key in ("argv", "exe", "env", "cwd"):
        fixture = requests[f"bad-request-extra-key-{key}-files-sha256"]
        assert key in fixture["body"]
        assert fixture["expect"]["reply"] == _support.refused("bad_request")
    assert requests["bad-request-wrong-v"]["body"]["v"] != spec.PROTOCOL
    for name in (
        "bad-request-request-past-size-limit",
        "bad-args-path-oversize",
        "bad-request-op-oversize",
    ):
        assert not requests[name]["valid"]
    nul = requests["bad-request-nul-in-args"]
    assert any(isinstance(v, str) and "\x00" in v for v in _support.walk(nul["body"]))
    assert nul["expect"]["reply"] == _support.refused("bad_request")
    for rule in ("strings", "integers", "not-after-range"):
        assert any(f.get("prose_rule") == rule for f in requests.values()), rule
    replies = _all()["reply"]
    for rule in ("strings", "integers", "stream-size"):
        assert any(f.get("prose_rule") == rule for f in replies.values()), rule


def test_the_request_size_bound_has_a_fixture_on_each_side() -> None:
    # Each holds the same fault, a path over 1024 characters, so only the size picks the code.
    requests = _all()["request"]
    at = requests["bad-args-request-at-size-limit"]
    past = requests["bad-request-request-past-size-limit"]
    assert _support.serialized_size(at["body"]) == _support.REQUEST_LIMIT
    assert _support.serialized_size(past["body"]) == _support.REQUEST_LIMIT + 1
    assert at["expect"]["reply"] == _support.refused("bad_args")
    assert past["expect"]["reply"] == _support.refused("bad_request")


def test_fixture_job_ids_are_obviously_fake_and_distinct() -> None:
    well_formed = _support.ecma_regex(spec.load_schema("request")["$defs"]["job_id"]["pattern"])
    seen: list[str] = []
    for kind in ("request", "reply"):
        for fixture in _all()[kind].values():
            body = fixture["body"]
            job_id = body.get("job_id") if isinstance(body, dict) else None
            if isinstance(job_id, str) and well_formed.search(job_id):
                assert FAKE_JOB_ID.match(job_id) or job_id in HEX_JOB_IDS, job_id
                seen.append(job_id)
    assert len(seen) == len(set(seen))
    for kind, job_id in zip(("request", "reply"), HEX_JOB_IDS, strict=True):
        assert any(f["valid"] and f["body"]["job_id"] == job_id for f in _all()[kind].values())


def test_fixture_keys_come_in_the_order_the_spec_lists_them() -> None:
    for kind in ("request", "reply", "host.describe"):
        for name, fixture in _all()[kind].items():
            assert list(fixture) == [k for k in KEY_ORDER if k in fixture], f"{kind}/{name}"


def test_default_host_state() -> None:
    state = spec.default_host_state()
    assert set(state) == {
        "received_at",
        "host_version",
        "enabled_ops",
        "roots",
        "resolves_outside_root",
        "paused",
        "seen_job_ids",
        "caps",
        "event",
    }
    assert state["caps"] == {"stdout_bytes": 65536, "stderr_bytes": 65536}
    assert set(state["enabled_ops"]) == set(_support.CATALOG)
    for fixture in _all()["request"].values():
        event = fixture.get("host_state", {}).get("event")
        assert event is None or event in _support.EVENTS
