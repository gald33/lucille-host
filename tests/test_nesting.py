"""The host's own depth limit on the JSON and TOML it reads (src/lucille_host/nesting.py).

Every depth here past the limit is one each Python version's parsers read without trouble, so a
refusal can come only from the host's own check, and the same refusal on 3.11, 3.13 and 3.14.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from switchboard import pairing, sharing

from lucille_host import _switchboard, credentials, gmail, journal, nesting, policy, state, store
from tests._host import HostEnv
from tests._keyring import MemoryKeyring

MOST = nesting.FILES_MOST
TOO_DEEP = "the file store could not be used (a file in it nests too deep to be read)"


def _json(depth: int) -> str:
    return "[" * depth + "]" * depth


def _toml(depth: int) -> str:
    return "x = " + _json(depth) + "\n"


def _depth(value: Any) -> int:
    """How many levels of arrays and objects ``value`` nests."""
    if isinstance(value, dict):
        return 1 + max(map(_depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(_depth, value), default=0)
    return 0


# --- the check itself -----------------------------------------------------------------------------


def test_the_limit_leaves_room_for_a_request_in_the_journal() -> None:
    # The journal keeps a request's body three levels down, and the opener reads a body of up to
    # NEST_MOST levels inside the library's own object; Python 3.11's TOML parser, the shallowest,
    # reads several hundred.
    assert MOST == 128
    assert _switchboard.NEST_MOST + 1 + 3 < MOST < 300


def test_the_parsers_read_one_past_the_limit_on_this_python() -> None:
    # So every refusal below is the host's, on 3.11 as on 3.14.
    assert _depth(json.loads(_json(MOST + 1))) == MOST + 1
    assert _depth(tomllib.loads(_toml(MOST + 1))["x"]) == MOST + 1


@pytest.mark.parametrize("deeper", [nesting.json_deeper, nesting.toml_deeper])
def test_up_to_the_limit_and_one_past_it(deeper: Any) -> None:
    text = _json if deeper is nesting.json_deeper else _toml
    assert not deeper(text(MOST)) and deeper(text(MOST + 1))
    assert not deeper(text(3), 3) and deeper(text(4), 3)
    assert not deeper(text(0), 0) and deeper(text(1), 0)
    # Side by side is not deeper.
    beside = "x = [[1], [2], {a = [3]}]" if text is _toml else '[[1], [2], {"a": [3]}]'
    assert not deeper(beside, 3) and deeper(beside, 2)
    # Far past any parser's own limit, and quickly.
    assert deeper(text(1_000_000))


def test_json_brackets_in_strings_and_escapes_are_not_counted() -> None:
    assert not nesting.json_deeper('["[[[[", "{{{{", "\\"[[[[", "\\\\", "]]]]"]', 1)
    assert nesting.json_deeper('["\\\\", ["a"]]', 1)
    assert not nesting.json_deeper('{"[[[": "]]]", "k\\"[[": 1}', 1)
    # A string with no end takes the rest of the text, as the parser refuses there anyway.
    assert not nesting.json_deeper('["abc [[[[[[', 1)


def test_toml_strings_and_comments_are_not_counted() -> None:
    text = (
        'a = "[[[ \\" [[["\n'
        "b = '[[[[ \\'\n"
        'c = """[[[\n"[[" ""[[ \\""" [["""\n'
        "d = '''[[[\n''[['''\n"
        'e = """x"""""\n'
        "f = '''x'''''\n"
        "g = [1] # [[[[[[\n"
        'h = {k = "[[[["}\n'
        '"[[[" = 1\n'
    )
    parsed = tomllib.loads(text)
    assert parsed["e"] == 'x""' and parsed["f"] == "x''"
    assert not nesting.toml_deeper(text, 1)
    assert nesting.toml_deeper(text + "z = [[1]]\n", 1)
    # A multi-line string ends at its first three quotes plus up to two more, so a fourth quote
    # opens no string that hides what follows.
    for quotes in ('"""', "'''"):
        hiding = f"a = [{quotes}x{quotes}{quotes[0]}, [[1]]]\n"
        assert _depth(tomllib.loads(hiding)["a"]) == 3
        assert nesting.toml_deeper(hiding, 2), hiding
    # A table header counts as nesting, which only makes the count larger.
    assert nesting.toml_deeper("[[a]]\n", 1) and not nesting.toml_deeper("[[a]]\n", 2)


def _value(rng: random.Random, depth: int) -> Any:
    if depth == 0 or rng.random() < 0.2:
        return rng.choice(
            ["[", "]", "{", "}", '"', "\\", "'", "#", "a\nb", '"""', "'''", "[[{", 7, True, 1.5]
        )
    if rng.random() < 0.5:
        return [_value(rng, depth - 1) for _ in range(rng.randint(0, 3))]
    return {f"k{i}[": _value(rng, depth - 1) for i in range(rng.randint(0, 3))}


def _toml_value(value: Any, rng: random.Random) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item, rng) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f'"{key}" = {_toml_value(item, rng)}' for key, item in value.items())
        return "{" + inner + "}"
    if isinstance(value, str):
        if "'" not in value and "\n" not in value and rng.random() < 0.5:
            return f"'{value}'"
        if value == '"""':
            return "'''\"\"\"'''"
        return json.dumps(value)
    return json.dumps(value)


def test_the_count_is_the_parsers_own_depth() -> None:
    rng = random.Random(20261119)
    for _ in range(400):
        value = [_value(rng, rng.randint(0, 7))]
        depth = _depth(value)
        text = json.dumps(value, indent=rng.choice([None, 1]))
        assert json.loads(text) == value
        assert not nesting.json_deeper(text, depth) and nesting.json_deeper(text, depth - 1), text
        toml = f"x = {_toml_value(value, rng)}  # [[[\n"
        assert _depth(tomllib.loads(toml)["x"]) == depth, toml
        assert not nesting.toml_deeper(toml, depth) and nesting.toml_deeper(toml, depth - 1), toml


def test_a_large_file_is_checked_quickly() -> None:
    text = json.dumps({"jobs": {str(i): {"body": "x" * 200, "n": [i]} for i in range(20000)}})
    assert len(text) > 4_000_000
    started = time.monotonic()
    assert not nesting.json_deeper(text)
    assert time.monotonic() - started < 5


# --- each reader ----------------------------------------------------------------------------------


def test_a_state_file_one_past_the_limit_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(_json(MOST))
    assert _depth(state.read_json(path)) == MOST
    path.write_text(_json(MOST + 1))
    with pytest.raises(state.StateError, match=r"nests too deep to be read$"):
        state.read_json(path)


def test_a_policy_one_past_the_limit_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(_toml(MOST))
    with pytest.raises(policy.PolicyError) as at_limit:
        policy.load(path)
    assert "nests too deep" not in str(at_limit.value)
    path.write_text(_toml(MOST + 1))
    with pytest.raises(policy.PolicyError, match=r"nests too deep to be read$"):
        policy.load(path)


def test_the_journal_reads_back_a_request_as_deep_as_the_opener_reads(tmp_path: Path) -> None:
    body: Any = {}
    for _ in range(_switchboard.NEST_MOST):
        body = {"a": body}
    jobs = journal.Journal(tmp_path / "journal.json", clock=lambda: 1.0)
    jobs.received("job", room="r", seq=1, received_at=1.0, not_after=None, body=body)
    entry = jobs.get("job")
    assert entry is not None and entry.body == body


def test_the_file_store_refuses_a_file_one_past_the_limit(tmp_path: Path) -> None:
    pairs = _switchboard.FilePairs(pairing.PairStore(tmp_path, env={}))
    rooms = _switchboard._ShareFiles(tmp_path, env={})
    # A file of any name, at any level of the folder.
    (tmp_path / "voided").mkdir()
    planted = tmp_path / "voided" / "other.json"
    planted.write_text(_json(MOST))
    assert pairs.get("default") is None and pairs.pending("default") is None
    assert pairs.verdict("default") is None
    assert rooms.load() == {}
    planted.write_text(_json(MOST + 1))
    for read in (
        lambda: pairs.get("default"),
        lambda: pairs.pending("default"),
        lambda: pairs.verdict("default"),
    ):
        with pytest.raises(store.StoreError, match=re.escape(TOO_DEEP)):
            read()
    with pytest.raises(sharing.SharingError, match=r"nests too deep") as refused:
        rooms.load()
    assert refused.value.state == "store"
    # Nothing that reads no file is stopped.
    assert pairs.names() == []


def test_a_keyring_item_past_the_limit_is_not_parsed(
    tmp_path: Path, memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pair's item and the credentials' item: one that nests past the limit is one this
    # version did not write, as one that is not JSON is, and is never parsed.
    pairs = store.Item(tmp_path / "state", "default")
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    parsed = _spy_on_json(monkeypatch)
    for service, read in ((store.SERVICE, pairs.read), (credentials.SERVICE, creds.names)):
        for depth in (MOST + 1, 100_000):
            memory_keyring.items[(service, "default")] = _json(depth)
            with pytest.raises(store.StoreError, match="not one this version wrote"):
                read()
    assert parsed == []
    assert memory_keyring.writes == 0


def _spy_on_json(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    parsed: list[str] = []
    loads = json.loads

    def spy(text: str, *args: Any, **kwargs: Any) -> Any:
        parsed.append(text)
        return loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", spy)
    return parsed


def test_the_file_store_link_checks_its_share_store(host_env: HostEnv) -> None:
    # The link the host opens over the file store reads its share store through the check.
    link = _switchboard.open_link(host_env.dirs, state.HostConfig(store="file"))
    assert link.rooms.load() == {}
    (host_env.dirs.store_files / "deep.json").write_text(_json(MOST + 1))
    with pytest.raises(sharing.SharingError, match="nests too deep") as refused:
        link.rooms.load()
    assert refused.value.state == "store"


def _code(extra: Any) -> str:
    """A pairing code the library reads, with a field of its own that holds ``extra``."""

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {
        "v": 1,
        "u": "http://127.0.0.1:9",
        "s": b64(bytes(32)),
        "e": b64(bytes(32)),
        "c": b64(bytes(16)),
        "x": 4_000_000_000,
        "p": 0,
        "z": extra,
    }
    return _switchboard.CODE_PREFIX + b64(json.dumps(payload).encode())


def test_a_code_s_payload_past_the_limit_is_no_code_on_every_version() -> None:
    # A code the library reads, with a field nested deep: up to NEST_MOST levels it decodes, and
    # past that it is refused before the library's parser reads it, which on some versions would
    # read it and on others not.
    assert _switchboard.CODE_PREFIX == pairing.CODE_PREFIX
    nest = _switchboard.NEST_MOST
    assert _switchboard.decode_code(_code(json.loads(_json(nest - 1)))).url
    for depth in (nest, 300):
        with pytest.raises(_switchboard.BadCode, match="not a pairing code"):
            _switchboard.decode_code(_code(json.loads(_json(depth))))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32"])
def test_a_code_s_payload_in_another_encoding_is_no_code(encoding: str) -> None:
    # The library writes its codes in ASCII; its parser would read UTF-16 and UTF-32 too, where
    # the depth check does not count, so a payload in them is refused before it is read, deep or
    # not, on every version.
    for extra in (None, json.loads(_json(_switchboard.NEST_MOST))):
        payload = json.loads(base64.urlsafe_b64decode(_code(extra)[6:] + "==="))
        raw = json.dumps(payload).encode(encoding)
        code = _switchboard.CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with pytest.raises(_switchboard.BadCode, match="not a pairing code"):
            _switchboard.decode_code(code)


def test_a_parser_that_stops_short_is_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # Kept should the library's parser ever stop short of the host's own limit.
    def too_deep(text: str) -> object:
        raise RecursionError

    monkeypatch.setattr(pairing.PairingCode, "decode", too_deep)
    with pytest.raises(_switchboard.BadCode, match="not a pairing code"):
        _switchboard.decode_code(_code(None))


@pytest.mark.parametrize("depth", [MOST + 1, 2000, 200_000])
def test_a_pairing_code_nested_too_deep_is_no_code(depth: int) -> None:
    # Too deep for the library's parser on some versions and not on others: never a traceback,
    # on any, and the same refusal as any text that is not a code.
    payload = base64.urlsafe_b64encode(_json(depth).encode()).decode().rstrip("=")
    with pytest.raises(_switchboard.BadCode, match="not a pairing code"):
        _switchboard.decode_code(pairing.CODE_PREFIX + payload)


def test_dotted_keys_and_headers_count_as_the_tables_they_make(tmp_path: Path) -> None:
    # A key of n parts makes n - 1 tables; a header of n parts n; an array of tables one more.
    # Some patch levels of tomllib refuse a key of more than 1000 parts and others read it, so a
    # key past the host's limit is refused before the parser reads it, on every one.
    assert not nesting.toml_deeper("a" + ".a" * MOST + " = 1\n")
    assert nesting.toml_deeper("a" + ".a" * (MOST + 1) + " = 1\n")
    assert not nesting.toml_deeper("[a" + ".a" * (MOST - 1) + "]\n")
    assert nesting.toml_deeper("[a" + ".a" * MOST + "]\n")
    assert not nesting.toml_deeper("[a.b]\nc.d = [1]\n", 4) and nesting.toml_deeper(
        "[a.b]\nc.d = [1]\n", 3
    )
    assert not nesting.toml_deeper("[[a]]\n", 2) and nesting.toml_deeper("[[a]]\n", 1)
    # Dots in values, strings and comments are not parts of a key.
    text = 'a = 1.5\nb = "x.y.z"\nc = [1.5, 2.5] # d.e.f\n"g.h" = {i = 1.0}\n'
    assert _depth(tomllib.loads(text)) - 1 == 1
    assert not nesting.toml_deeper(text, 1) and nesting.toml_deeper(text, 0)
    for parts in (MOST + 2, 2000):
        path = tmp_path / "policy.toml"
        path.write_text("a" + ".a" * (parts - 1) + " = 1\n")
        with pytest.raises(policy.PolicyError, match=r"nests too deep to be read$"):
            policy.load(path)


def _toml_document(rng: random.Random) -> str:
    """A TOML document of headers, dotted keys, arrays and inline tables, its keys unique."""

    def fresh(prefix: str) -> str:
        return f"{prefix}{rng.randint(0, 10**9)}"

    def part() -> str:
        return rng.choice(["a%d", "b%d", "1%d", '"x.y%d"', "'q.r%d'", '"[[%d"']) % rng.randint(0, 9)

    def value(depth: int) -> str:
        r = rng.random()
        if depth <= 0 or r < 0.3:
            return rng.choice(["1", "1.5", "2e3", '"a.b"', "'[.]'", "true", "1979-05-27T07:32:00Z"])
        if r < 0.6:
            return "[" + ", ".join(value(depth - 1) for _ in range(rng.randint(0, 3))) + "]"
        items = [
            f"{fresh('u')}{'.' + part() if rng.random() < 0.5 else ''} = {value(depth - 1)}"
            for _ in range(rng.randint(0, 3))
        ]
        return "{" + ", ".join(items) + "}"

    lines = []
    for _ in range(rng.randint(1, 4)):
        if rng.random() < 0.3:
            dotted = "".join(f".s{rng.randint(0, 9)}" for _ in range(rng.randint(0, 3)))
            lines.append(f"[{fresh('h')}{dotted}]  # [[x.y")
        for _ in range(rng.randint(1, 3)):
            parts = [fresh("z"), *(part() for _ in range(rng.randint(0, 3)))]
            key = rng.choice([".", " . "]).join(parts)
            lines.append(f"{key} = {value(rng.randint(0, 4))}")
    return "\n".join(lines) + "\n"


def test_the_toml_count_is_the_parsers_depth_with_headers_and_dotted_keys() -> None:
    rng = random.Random(20261119)
    for _ in range(1500):
        text = _toml_document(rng)
        depth = _depth(tomllib.loads(text)) - 1
        assert not nesting.toml_deeper(text, depth) and nesting.toml_deeper(text, depth - 1), text
    # With arrays of tables, a header may name the last table of one: the count is never less.
    for _ in range(500):
        lines = []
        for _ in range(rng.randint(1, 4)):
            name = ".".join(f"t{rng.randint(0, 2)}" for _ in range(rng.randint(1, 3)))
            lines += [f"[[{name}]]" if rng.random() < 0.5 else f"[{name}]"]
            lines += [f"x{rng.randint(0, 10**9)} = [[1]]"]
        text = "\n".join(lines) + "\n"
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue
        assert nesting.toml_deeper(text, _depth(parsed) - 2), text


def test_the_gmail_childs_arguments_past_the_limit_are_not_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def args(depth: int) -> str:
        return '{"credential": "default", "x": ' + _json(depth - 1) + "}"

    parsed = _spy_on_json(monkeypatch)
    for depth in (MOST, MOST + 1, 100_000):
        parsed.clear()
        argv = ["--state", str(tmp_path), "--profile", "default", "--", "gmail.check", args(depth)]
        reply = gmail.main(argv, connect=lambda address: pytest.fail("connected"))
        # No gmail.check takes such arguments, so each is internal; past the limit the child
        # does not parse them at all.
        assert (reply.code, reply.stdout) == (gmail.INTERNAL, b"")
        assert reply.stderr == (gmail.INTERNAL_LINE + "\n").encode()
        assert (args(depth) in parsed) is (depth <= MOST)


def test_the_gmail_childs_sign_ins_past_the_limit_are_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read as no sign-ins, as a file that is not JSON is, and never parsed.
    path = tmp_path / gmail.SIGNINS_FILE
    path.write_text('{"default": ' + _json(MOST - 1) + "}")
    assert gmail._signins_load(path) == {"default": json.loads(_json(MOST - 1))}
    parsed = _spy_on_json(monkeypatch)
    for depth in (MOST + 1, 100_000):
        path.write_text(_json(depth))
        assert gmail._signins_load(path) == {}
    assert parsed == []
