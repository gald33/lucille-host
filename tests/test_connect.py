"""Connect from Lucille's chat, the parts that need no hub (decisions/0002 §11.4 and §11.5).

The key and the nonce are checked against the ``cryptography`` package's own scrypt and HKDF, an
implementation the host does not use, and HKDF against RFC 5869's test vectors. Deliveries are
signed with throwaway keys, never Lucille's.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from switchboard import pairing

from lucille_host import _switchboard, connect, delivery, spec

# --- The connect code -------------------------------------------------------------------------


def test_a_drawn_code_is_ten_symbols_of_the_alphabet() -> None:
    for _ in range(200):
        code = connect.draw_code()
        assert len(code) == connect.SYMBOLS
        assert set(code) <= set(connect.ALPHABET)
        assert connect.parse_code(code) == code


def test_drawn_codes_use_the_whole_alphabet_and_differ() -> None:
    codes = {connect.draw_code() for _ in range(400)}
    assert len(codes) == 400
    assert set("".join(codes)) == set(connect.ALPHABET)


def test_the_alphabet_is_crockfords() -> None:
    assert connect.ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    assert len(connect.ALPHABET) == 32
    assert not set("ILOU") & set(connect.ALPHABET)


def test_a_code_is_shown_as_two_groups_of_five() -> None:
    assert connect.shown("7KQ4MX2D9P") == "7KQ4M-X2D9P"
    with pytest.raises(ValueError):
        connect.shown("7kq4m-x2d9p")


@pytest.mark.parametrize(
    "text",
    [
        "7KQ4MX2D9P",
        "7KQ4M-X2D9P",
        "7kq4m-x2d9p",
        " 7KQ4M X2D9P ",
        "7-K-Q-4-M-X-2-D-9-P",
        "7KQ4M--X2D9P",
    ],
)
def test_case_spaces_and_dashes_are_ignored(text: str) -> None:
    assert connect.parse_code(text) == "7KQ4MX2D9P"


def test_i_l_and_o_read_as_one_one_and_zero() -> None:
    assert connect.parse_code("IlLoO-01234") == "1110001234"
    assert connect.parse_code("ILLOO01234") == "1110001234"
    assert connect.parse_code("illoo-01234") == "1110001234"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "7KQ4MX2D9",
        "7KQ4MX2D9PQ",
        "7KQ4MX2D9U",
        "7KQ4MX2D9u",
        "7KQ4M_X2D9P",
        "7KQ4M\tX2D9P",
        "7KQ4M\nX2D9P",
        "7KQ4M X2D9P",
        "7KQ4MX2D9ı",  # the dotless i, which upper() would turn into I
        "7KQ4MX2D9K",  # the Kelvin sign
        "７KQ4MX2D9P",  # a fullwidth seven
    ],
)
def test_anything_else_is_not_a_code(text: str) -> None:
    assert connect.parse_code(text) is None


# --- The epoch, the key and the nonce -----------------------------------------------------------


def test_the_epoch_is_seconds_floor_divided_by_1200() -> None:
    assert connect.epoch(0) == 0
    assert connect.epoch(1199.999) == 0
    assert connect.epoch(1200) == 1
    assert connect.epoch(1_773_864_000) == 1_478_220
    with pytest.raises(ValueError):
        connect.epoch(-1)


def _cryptography_scrypt(code: str, at_epoch: int) -> bytes:
    salt = f"lucille-host/connect/v1/{at_epoch}".encode("ascii")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(code.encode("ascii"))


def _cryptography_hkdf(key: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=16, salt=None, info=b"lucille-host/connect/nonce"
    ).derive(key)


def test_the_key_matches_an_independent_scrypt() -> None:
    code, at = "7KQ4MX2D9P", 1_478_220
    key = connect.stretch(code, at)
    assert len(key) == 32
    assert key == _cryptography_scrypt(code, at)


def test_the_key_depends_on_the_epoch_and_the_code() -> None:
    key = connect.stretch("7KQ4MX2D9P", 1_478_220)
    assert connect.stretch("7KQ4MX2D9P", 1_478_221) != key
    assert connect.stretch("7KQ4MX2D9Q", 1_478_220) != key


def test_the_salt_writes_the_epoch_in_plain_decimal() -> None:
    # Epoch 0 is "0", with no sign and no padding.
    assert connect.stretch("0000000000", 0) == _cryptography_scrypt("0000000000", 0)


def test_the_key_is_stretched_only_from_a_canonical_code() -> None:
    for text in ("7kq4mx2d9p", "7KQ4M-X2D9P", "7KQ4MX2D9U"):
        with pytest.raises(ValueError):
            connect.stretch(text, 1)
    with pytest.raises(ValueError):
        connect.stretch("7KQ4MX2D9P", -1)


def test_the_nonce_matches_an_independent_hkdf() -> None:
    for key in (bytes(32), bytes(range(32)), os.urandom(32)):
        assert connect.nonce(key) == _cryptography_hkdf(key)
        assert len(connect.nonce(key)) == 16


def test_the_nonce_needs_a_32_byte_key() -> None:
    for key in (b"", bytes(31), bytes(33)):
        with pytest.raises(ValueError):
            connect.nonce(key)


# RFC 5869, appendix A: test cases 1 and 3 (case 3 has no salt and no info).
@pytest.mark.parametrize(
    ("ikm", "salt", "info", "length", "okm"),
    [
        (
            "0b" * 22,
            "000102030405060708090a0b0c",
            "f0f1f2f3f4f5f6f7f8f9",
            42,
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865",
        ),
        (
            "0b" * 22,
            "",
            "",
            42,
            "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8",
        ),
    ],
)
def test_hkdf_matches_rfc_5869(ikm: str, salt: str, info: str, length: int, okm: str) -> None:
    got = connect._hkdf_sha256(
        bytes.fromhex(ikm), bytes.fromhex(info), length, salt=bytes.fromhex(salt)
    )
    assert got.hex() == okm


def test_a_stretch_costs_its_memory_cap_and_no_more() -> None:
    # 128 * r * n bytes is not enough for OpenSSL's scrypt; the cap the host passes is.
    assert connect._SCRYPT_MAXMEM > 128 * connect.SCRYPT_R * connect.SCRYPT_N
    assert connect._SCRYPT_MAXMEM < 129 * 1024 * 1024
    start = time.monotonic()
    connect.stretch("0000000000", 1)
    assert time.monotonic() - start < 30


# --- The user's own username ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["ada", "ADA", "@ada", "@Ada", "telegram:ada", "Telegram:@Ada", "TELEGRAM:ADA", "  @ada  "],
)
def test_one_name_in_every_spelling(text: str) -> None:
    assert connect.normalize_username(text, "telegram") == "ada"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "@",
        "@@ada",
        "@telegram:ada",
        "slack:ada",
        "telegram:",
        "1ada",
        "_ada",
        "ada-lovelace",
        "ada.l",
        "ada lovelace",
        "a" * 65,
        "\tada",
        "ada\n",
        "Kda",  # the Kelvin sign, which a Unicode lowering would turn into k
        "adа",  # a Cyrillic а
        "telegram :ada",
    ],
)
def test_names_outside_the_grammar_are_refused(text: str) -> None:
    assert connect.normalize_username(text, "telegram") is None


def test_the_longest_name_is_64() -> None:
    assert connect.normalize_username("a" * 64, "telegram") == "a" * 64


def test_only_a_service_capsule_offers_is_taken() -> None:
    with pytest.raises(ValueError):
        connect.normalize_username("ada", "slack")


def _found(sender: str = "telegram:123456789", handle: str = "@Ada") -> delivery.Delivery:
    return delivery.Delivery(code="swbp1_x", kid="0" * 16, sender=sender, handle=handle)


def test_a_delivery_names_the_user_whatever_the_case_of_its_handle() -> None:
    assert connect.names_user(_found(handle="@Ada"), "telegram", "ada")
    assert connect.names_user(_found(handle="@ADA"), "telegram", "ada")


@pytest.mark.parametrize(
    "found",
    [
        _found(handle="@eve"),
        _found(handle=""),
        _found(handle="@ada_"),
        _found(handle="@ad"),
        _found(sender="slack:U024BE7LH"),
        _found(sender="telegramx:1"),
    ],
)
def test_another_account_does_not_name_the_user(found: delivery.Delivery) -> None:
    assert not connect.names_user(found, "telegram", "ada")


def test_names_user_takes_only_a_normalized_username() -> None:
    for name in ("@ada", "ADA", "telegram:ada", ""):
        with pytest.raises(ValueError):
            connect.names_user(_found(), "telegram", name)
    with pytest.raises(ValueError):
        connect.names_user(_found(), "slack", "ada")


# --- One read of the drop room ------------------------------------------------------------------

NONCE = bytes(range(16))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code() -> str:
    return pairing.PairingCode(
        url=_switchboard.LUCILLE_HUB,
        secret=os.urandom(32),
        initiator=os.urandom(32),
        expires_at=int(time.time()) + 900,
        commit=os.urandom(16),
        note="lucille-host/2 n=" + NONCE.hex(),
        hello=True,
    ).encode()


class Signer:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).digest()[:8].hex()
        self.keys = {self.kid: raw}

    def body(
        self, code: str | None = None, *, sender: str = "telegram:1", nonce: bytes = NONCE
    ) -> str:
        code = _code() if code is None else code
        signed = delivery.signed_bytes(self.kid, nonce, code, sender, "@ada")
        return json.dumps(
            {
                "t": delivery.TYPE,
                "code": code,
                "kid": self.kid,
                "from": sender,
                "handle": "@ada",
                "sig": _b64(self.private.sign(signed)),
            }
        )


@pytest.fixture
def signer() -> Signer:
    return Signer()


def _row(body: object) -> dict[str, object]:
    return {"body": body, "created_at": "2026-11-26T10:00:00Z", "sender": "x"}


HELLO_ROW = _row({"t": "lucille-host/hello/1"})


def _judge(rows: list[object], signer: Signer) -> connect.Read:
    return connect.judge(rows, NONCE, keys=signer.keys)


def test_the_hello_alone_is_waiting(signer: Signer) -> None:
    read = _judge([HELLO_ROW], signer)
    assert read == connect.Read(connect.WAITING)
    assert not read.unknown_key


def test_one_delivery_is_delivered(signer: Signer) -> None:
    code = _code()
    read = _judge([HELLO_ROW, _row(signer.body(code))], signer)
    assert read.outcome == connect.DELIVERED
    assert read.found is not None
    assert read.found.code == code
    assert read.ignored == ()


def test_the_same_delivery_twice_counts_once(signer: Signer) -> None:
    body = signer.body()
    read = _judge([HELLO_ROW, _row(body), _row(body)], signer)
    assert read.outcome == connect.DELIVERED


def test_the_same_delivery_re_encoded_counts_once(signer: Signer) -> None:
    body = signer.body()
    spaced = json.dumps(json.loads(body), indent=1)
    assert spaced != body
    read = _judge([HELLO_ROW, _row(body), _row(spaced)], signer)
    assert read.outcome == connect.DELIVERED


def test_two_deliveries_are_refused_together(signer: Signer) -> None:
    read = _judge([HELLO_ROW, _row(signer.body()), _row(signer.body())], signer)
    assert read == connect.Read(connect.TWO_DELIVERIES)
    assert read.found is None


def test_one_code_for_two_accounts_is_two_deliveries(signer: Signer) -> None:
    code = _code()
    rows = [HELLO_ROW, _row(signer.body(code)), _row(signer.body(code, sender="telegram:2"))]
    assert _judge(rows, signer).outcome == connect.TWO_DELIVERIES


def test_a_post_that_fails_the_checks_is_ignored_and_named(signer: Signer) -> None:
    other = Signer()
    rows = [
        HELLO_ROW,
        _row({"note": "hi"}),
        _row("not json"),
        _row(other.body()),
        _row(signer.body(nonce=bytes(16))),
    ]
    read = _judge(rows, signer)
    assert read.outcome == connect.WAITING
    assert read.ignored == (
        delivery.NOT_TEXT,
        delivery.NOT_JSON,
        delivery.UNKNOWN_KEY,
        delivery.FORGED,
    )
    assert read.unknown_key


def test_an_ignored_post_does_not_hide_a_delivery(signer: Signer) -> None:
    code = _code()
    rows = [_row("junk"), _row(signer.body(code)), HELLO_ROW, _row(7)]
    read = _judge(rows, signer)
    assert read.outcome == connect.DELIVERED
    assert read.found is not None and read.found.code == code
    assert read.ignored == (delivery.NOT_JSON, delivery.NOT_TEXT)


def test_a_room_without_the_hello_is_interference(signer: Signer) -> None:
    assert _judge([], signer) == connect.Read(connect.INTERFERENCE, why=connect.NO_HELLO)
    rows = [_row(signer.body())]
    assert _judge(rows, signer) == connect.Read(connect.INTERFERENCE, why=connect.NO_HELLO)


@pytest.mark.parametrize(
    "body",
    [
        {"t": "lucille-host/hello/1", "x": 1},
        {"t": "lucille-host/hello/2"},
        {"T": "lucille-host/hello/1"},
        json.dumps({"t": "lucille-host/hello/1"}),
        [{"t": "lucille-host/hello/1"}],
    ],
)
def test_only_the_exact_object_is_the_hello(signer: Signer, body: object) -> None:
    read = _judge([_row(body)], signer)
    assert read == connect.Read(connect.INTERFERENCE, why=connect.NO_HELLO)


def test_a_second_hello_is_interference(signer: Signer) -> None:
    rows = [HELLO_ROW, _row(signer.body()), HELLO_ROW]
    assert _judge(rows, signer) == connect.Read(connect.INTERFERENCE, why=connect.HELLO_TWICE)


def test_fifty_posts_is_interference_even_with_a_delivery(signer: Signer) -> None:
    rows: list[object] = [HELLO_ROW, _row(signer.body())] + [_row("x")] * 48
    assert len(rows) == connect.READ_LIMIT
    assert _judge(rows, signer) == connect.Read(connect.INTERFERENCE, why=connect.TOO_MANY)
    assert _judge(rows[:-1], signer).outcome == connect.DELIVERED


@pytest.mark.parametrize("row", [None, "text", ["body"], {"created_at": "x"}])
def test_a_row_that_is_not_a_post_is_interference(signer: Signer, row: object) -> None:
    assert _judge([HELLO_ROW, row], signer) == connect.Read(
        connect.INTERFERENCE, why=connect.NOT_A_POST
    )


def test_the_checks_come_in_order(signer: Signer) -> None:
    # Fifty posts with no hello: too many first. No hello and two hellos: never both. A missing
    # hello comes before any delivery is looked at.
    assert _judge([None] * connect.READ_LIMIT, signer).why == connect.TOO_MANY
    assert _judge([None], signer).why == connect.NOT_A_POST
    two = [_row(signer.body()), _row(signer.body())]
    assert _judge(two, signer).why == connect.NO_HELLO


def test_judge_uses_the_release_keys_by_default(signer: Signer) -> None:
    read = connect.judge([HELLO_ROW, _row(signer.body())], NONCE)
    assert read.outcome == connect.WAITING
    assert read.unknown_key


def test_judge_needs_a_16_byte_nonce(signer: Signer) -> None:
    with pytest.raises(ValueError):
        connect.judge([HELLO_ROW], NONCE[:15], keys=signer.keys)


def test_a_read_keeps_nothing_from_the_room_but_the_delivery(signer: Signer) -> None:
    code = _code()
    rows = [HELLO_ROW, _row("secret junk"), _row(signer.body(code))]
    read = _judge(rows, signer)
    assert "secret junk" not in repr(read)
    assert code not in repr(read)


# --- Lucille's official accounts ----------------------------------------------------------------


def _account(handle: str = "@lucille", account: str = "telegram:1001") -> connect.Account:
    return connect.Account(handle=handle, account=account)


def test_the_official_accounts_list_none_until_confirmed() -> None:
    # The list is filled in only once the maintainer confirms Lucille's accounts; until then the
    # code is shown with none named, and Connect works as it would with them.
    assert dict(connect.OFFICIAL_ACCOUNTS) == {"telegram": ()}
    assert connect.official_accounts("telegram") == ()
    assert connect.official_accounts("slack") == ()


def test_the_schema_bounds_the_accounts_as_the_core_does() -> None:
    schema = spec.load_cli_schema("connect")
    send_to = schema["$defs"]["connect.code_send_to"]["properties"]["send_to"]
    assert (send_to["minItems"], send_to["maxItems"]) == (1, connect.MOST_ACCOUNTS)
    entry = schema["$defs"]["official_account"]["properties"]
    assert entry["handle"] == {"$ref": "#/$defs/signed_handle"}
    assert entry["account"] == {"$ref": "#/$defs/signed_from"}
    assert schema["$defs"]["signed_handle"]["pattern"] == f"^{connect._HANDLE.pattern}$"
    assert schema["$defs"]["signed_from"]["pattern"] == f"^{connect._ACCOUNT_ID.pattern}$"


def test_the_official_accounts_cannot_be_changed_in_place() -> None:
    with pytest.raises(TypeError):
        connect.OFFICIAL_ACCOUNTS["telegram"] = (_account(),)  # type: ignore[index]


def test_a_list_of_official_accounts_in_its_grammar_is_taken() -> None:
    connect.check_accounts(
        {
            "telegram": (
                _account("@Lucille", "telegram:1001"),
                _account("@lucille_help", "telegram:abc-_9"),
                _account("@L", "telegram:" + "9" * 64),
                _account("@x" + "y" * 63, "telegram:4"),
            )
        }
    )


@pytest.mark.parametrize(
    "table",
    [
        {"slack": (_account(account="slack:1"),)},
        {"telegram": tuple(_account(f"@l{n}", f"telegram:{n}") for n in range(5))},
        {"telegram": (_account(handle="lucille"),)},
        {"telegram": (_account(handle="@1ucille"),)},
        {"telegram": (_account(handle="@lu-cille"),)},
        {"telegram": (_account(handle="@x" + "y" * 64),)},
        {"telegram": (_account(account="telegram:"),)},
        {"telegram": (_account(account="telegram:12 3"),)},
        {"telegram": (_account(account="telegram:" + "9" * 65),)},
        {"telegram": (_account(account="slack:1001"),)},
        {"telegram": (_account(account="1001"),)},
        {"telegram": (_account("@a", "telegram:1"), _account("@b", "telegram:1"))},
        {"telegram": (_account("@Ada", "telegram:1"), _account("@ada", "telegram:2"))},
        {"telegram": ({"handle": "@lucille", "account": "telegram:1"},)},
    ],
)
def test_a_list_of_official_accounts_outside_its_grammar_is_refused(table: Any) -> None:
    with pytest.raises(ValueError):
        connect.check_accounts(table)
