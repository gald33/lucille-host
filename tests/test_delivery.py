"""The host's checks on Lucille's signed delivery (decisions/0002 §11.5), and the load-time checks
on the keys a release pins.

Every delivery here is signed with a throwaway key made for the test, or with RFC 8032's own test
key, never with Lucille's: the tests pass that key in through ``verify``'s ``keys``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from switchboard import pairing

from lucille_host import _switchboard, delivery

NONCE = bytes(range(16))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code() -> str:
    """A pairing code in its one encoding, as the library makes one."""
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
    """A throwaway Ed25519 key, pinned under its own id for the test."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.kid = hashlib.sha256(self.raw).digest()[:8].hex()

    @property
    def keys(self) -> dict[str, bytes]:
        return {self.kid: self.raw}

    def fields(
        self,
        *,
        code: str | None = None,
        sender: str = "telegram:123456789",
        handle: str = "@ada",
        nonce: bytes = NONCE,
        kid: str | None = None,
        message: bytes | None = None,
    ) -> dict[str, str]:
        code = _code() if code is None else code
        kid = self.kid if kid is None else kid
        signed = delivery.signed_bytes(kid, nonce, code, sender, handle)
        sig = self.private.sign(signed if message is None else message)
        return {
            "t": delivery.TYPE,
            "code": code,
            "kid": kid,
            "from": sender,
            "handle": handle,
            "sig": _b64(sig),
        }

    def body(self, **kwargs: object) -> str:
        return json.dumps(self.fields(**kwargs))  # type: ignore[arg-type]


@pytest.fixture
def signer() -> Signer:
    return Signer()


def _refused(body: object, signer: Signer, nonce: bytes = NONCE) -> str:
    with pytest.raises(delivery.Refused) as caught:
        delivery.verify(body, nonce, keys=signer.keys)
    return caught.value.reason


# --- A delivery that passes ------------------------------------------------------------------


def test_a_signed_delivery_passes(signer: Signer) -> None:
    code = _code()
    got = delivery.verify(signer.body(code=code), NONCE, keys=signer.keys)
    assert got == delivery.Delivery(
        code=code, kid=signer.kid, sender="telegram:123456789", handle="@ada"
    )


def test_a_delivery_keeps_its_code_and_handle_out_of_its_repr(signer: Signer) -> None:
    code = _code()
    got = delivery.verify(signer.body(code=code, handle="@ada_lovelace"), NONCE, keys=signer.keys)
    assert code not in repr(got)
    assert "ada_lovelace" not in repr(got)
    assert "telegram:123456789" in repr(got)


def test_an_empty_handle_passes(signer: Signer) -> None:
    assert delivery.verify(signer.body(handle=""), NONCE, keys=signer.keys).handle == ""


def test_a_delivery_of_exactly_the_most_bytes_passes(signer: Signer) -> None:
    body = signer.body()
    padded = body[:-1] + " " * (delivery.MOST_BYTES - len(body.encode())) + "}"
    assert len(padded.encode()) == delivery.MOST_BYTES
    delivery.verify(padded, NONCE, keys=signer.keys)


# --- RFC 8032 -------------------------------------------------------------------------------

# RFC 8032, section 7.1, TEST 1: the public key, an empty message and its signature.
RFC_KEY = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b4"
    "6bd25bf5f0595bbe24655141438e7a100b"
)


def test_an_rfc_8032_signature_verifies() -> None:
    assert _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG)


def test_an_rfc_8032_signature_fails_on_any_other_message_or_bit() -> None:
    assert not _switchboard.signature_valid(RFC_KEY, b"\x00", RFC_SIG)
    flipped = bytes([RFC_SIG[0] ^ 1]) + RFC_SIG[1:]
    assert not _switchboard.signature_valid(RFC_KEY, b"", flipped)


def test_a_non_canonical_signature_is_refused() -> None:
    # S + L, the group order, is the same scalar mod L, but RFC 8032 refuses S >= L.
    order = 2**252 + 27742317777372353535851937790883648493
    s = int.from_bytes(RFC_SIG[32:], "little") + order
    assert s < 2**256
    assert not _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG[:32] + s.to_bytes(32, "little"))


def test_a_key_or_signature_of_the_wrong_length_is_false() -> None:
    assert not _switchboard.signature_valid(RFC_KEY[:31], b"", RFC_SIG)
    assert not _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG[:63])
    assert not _switchboard.signature_valid(bytes(32), b"", RFC_SIG)


# --- The signed bytes -----------------------------------------------------------------------


def test_the_signed_bytes_are_laid_out_as_the_design_says() -> None:
    kid = "491e2cd6f9c3555d"
    got = delivery.signed_bytes(kid, NONCE, "swbp1_x", "telegram:1", "@ab")
    assert got == (
        b"lucille-host/connect-sig/v1"
        + b"\x00"
        + bytes.fromhex(kid)
        + NONCE
        + hashlib.sha256(b"swbp1_x").digest()
        + b"\x00\x0atelegram:1"
        + b"\x00\x03@ab"
    )


def test_the_signed_bytes_split_one_way_only() -> None:
    kid = "491e2cd6f9c3555d"
    one = delivery.signed_bytes(kid, NONCE, "c", "telegram:1", "@ab")
    other = delivery.signed_bytes(kid, NONCE, "c", "telegram:1@", "ab")
    assert one != other


def test_the_signed_bytes_refuse_a_bad_key_id_or_nonce() -> None:
    with pytest.raises(ValueError):
        delivery.signed_bytes("491E2CD6F9C3555D", NONCE, "c", "telegram:1", "")
    with pytest.raises(ValueError):
        delivery.signed_bytes("491e2cd6f9c3555d", NONCE[:15], "c", "telegram:1", "")


def test_verify_refuses_a_nonce_that_is_not_16_bytes(signer: Signer) -> None:
    with pytest.raises(ValueError):
        delivery.verify(signer.body(), NONCE + b"\x00", keys=signer.keys)


# --- The body ---------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, b"{}", {"t": delivery.TYPE}, 7])
def test_a_body_that_is_not_a_string_is_refused(signer: Signer, body: object) -> None:
    assert _refused(body, signer) == delivery.NOT_TEXT


def test_a_body_one_byte_over_the_most_is_refused(signer: Signer) -> None:
    body = signer.body()
    padded = body[:-1] + " " * (delivery.MOST_BYTES + 1 - len(body.encode())) + "}"
    assert _refused(padded, signer) == delivery.TOO_LARGE


def test_size_is_counted_in_utf8_bytes_not_characters(signer: Signer) -> None:
    body = "é" * (delivery.MOST_BYTES // 2 + 1)
    assert len(body) < delivery.MOST_BYTES
    assert _refused(body, signer) == delivery.TOO_LARGE


def test_a_body_that_is_not_utf8_is_refused(signer: Signer) -> None:
    assert _refused('{"t": "\ud800"}', signer) == delivery.NOT_UTF8


def test_a_body_that_nests_is_refused(signer: Signer) -> None:
    fields: dict[str, object] = dict(signer.fields())
    fields["code"] = [fields["code"]]
    assert _refused(json.dumps(fields), signer) == delivery.TOO_DEEP
    assert _refused("[" * 3000, signer) == delivery.TOO_DEEP


@pytest.mark.parametrize("body", ["", "{", "{'t': 1}", "nope", '{"t": "x",}'])
def test_a_body_that_is_not_json_is_refused(signer: Signer, body: str) -> None:
    assert _refused(body, signer) == delivery.NOT_JSON


def test_a_key_named_twice_is_refused_even_with_the_same_value(signer: Signer) -> None:
    body = signer.body()
    twice = body[:-1] + ', "t": "' + delivery.TYPE + '"}'
    assert _refused(twice, signer) == delivery.KEY_TWICE


@pytest.mark.parametrize("body", ['"text"', "[]", "7", "null"])
def test_a_body_that_is_not_an_object_is_refused(signer: Signer, body: str) -> None:
    assert _refused(body, signer) == delivery.NOT_OBJECT


def test_a_missing_or_extra_key_is_refused(signer: Signer) -> None:
    fields = signer.fields()
    del fields["handle"]
    assert _refused(json.dumps(fields), signer) == delivery.WRONG_KEYS
    fields = signer.fields()
    fields["note"] = ""
    assert _refused(json.dumps(fields), signer) == delivery.WRONG_KEYS


@pytest.mark.parametrize("value", [None, 1, 1.5, True])
def test_a_value_that_is_not_a_string_is_refused(signer: Signer, value: object) -> None:
    fields: dict[str, object] = dict(signer.fields())
    fields["handle"] = value
    assert _refused(json.dumps(fields), signer) == delivery.NOT_STRING


def test_a_nan_value_is_refused(signer: Signer) -> None:
    body = signer.body().replace('"handle": "@ada"', '"handle": NaN')
    assert _refused(body, signer) == delivery.NOT_STRING


def test_another_type_is_refused(signer: Signer) -> None:
    fields = signer.fields()
    fields["t"] = "lucille-host/delivery/2"
    assert _refused(json.dumps(fields), signer) == delivery.WRONG_TYPE
    fields["t"] = "lucille-host/hello/1"
    assert _refused(json.dumps(fields), signer) == delivery.WRONG_TYPE


# --- The fields' forms --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kid",
    [
        "",
        "491e2cd6f9c3555",
        "491e2cd6f9c3555d0",
        "491E2CD6F9C3555D",
        "491e2cd6f9c3555g",
        "491e2cd6f9c3555d\n",
        " 491e2cd6f9c3555d",
        "４91e2cd6f9c3555d",  # a fullwidth digit four
    ],
)
def test_a_key_id_out_of_its_form_is_refused(signer: Signer, kid: str) -> None:
    fields = signer.fields()
    fields["kid"] = kid
    assert _refused(json.dumps(fields), signer) == delivery.BAD_KID


@pytest.mark.parametrize(
    "sender",
    [
        "",
        "telegram",
        "telegram:",
        ":123",
        "Telegram:123",
        "a" * 17 + ":123",
        "telegram:" + "1" * 65,
        "telegram:12.3",
        "telegram:12 3",
        "telegram:123\n",
        "telegram:١٢٣",
        "tele gram:123",
        "telegram:123:4",
    ],
)
def test_a_signed_account_out_of_its_form_is_refused(signer: Signer, sender: str) -> None:
    fields = signer.fields()
    fields["from"] = sender
    assert _refused(json.dumps(fields), signer) == delivery.BAD_FROM


@pytest.mark.parametrize(
    "sender", ["a:1", "a" * 16 + ":" + "Z" * 64, "telegram:A_b-9", "slack:U024BE7LH"]
)
def test_a_signed_account_in_its_form_passes(signer: Signer, sender: str) -> None:
    assert delivery.verify(signer.body(sender=sender), NONCE, keys=signer.keys).sender == sender


@pytest.mark.parametrize(
    "handle",
    [
        "@",
        "ada",
        "@1ada",
        "@_ada",
        "@ada-lovelace",
        "@ada.l",
        "@" + "a" * 65,
        "@ada\n",
        " @ada",
        "@adа",  # a Cyrillic а
        "@@ada",
    ],
)
def test_a_handle_out_of_its_form_is_refused(signer: Signer, handle: str) -> None:
    fields = signer.fields()
    fields["handle"] = handle
    assert _refused(json.dumps(fields), signer) == delivery.BAD_HANDLE


@pytest.mark.parametrize("handle", ["@a", "@Ada_99", "@" + "a" * 64])
def test_a_handle_in_its_form_passes(signer: Signer, handle: str) -> None:
    assert delivery.verify(signer.body(handle=handle), NONCE, keys=signer.keys).handle == handle


def test_a_signature_out_of_its_form_is_refused(signer: Signer) -> None:
    fields = signer.fields()
    sig = fields["sig"]
    for bad in (sig[:-1], sig + "A", sig + "==", sig[:-1] + "+", sig[:-1] + "=", sig[:-1] + "/"):
        assert _refused(json.dumps({**fields, "sig": bad}), signer) == delivery.BAD_SIG


def test_a_signature_whose_last_character_is_not_canonical_is_refused(signer: Signer) -> None:
    # 86 characters carry 516 bits; the last character's low 4 bits are not part of the 64 bytes,
    # so 16 characters there decode to the same signature. Only the one with those bits clear is
    # taken.
    fields = signer.fields()
    sig = fields["sig"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(sig[-1])
    other = sig[:-1] + alphabet[last | 1]
    assert base64.urlsafe_b64decode(other + "==") == base64.urlsafe_b64decode(sig + "==")
    assert _refused(json.dumps({**fields, "sig": other}), signer) == delivery.BAD_SIG


# --- The code ---------------------------------------------------------------------------------


def _recoded(code: str, **changes: object) -> str:
    """The same code's payload written again, with ``changes``."""
    payload = json.loads(base64.urlsafe_b64decode(code[6:] + "=" * (-len(code[6:]) % 4)))
    payload.update(changes)
    return "swbp1_" + _b64(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "variant",
    [
        lambda code: " " + code,
        lambda code: code + "\n",
        lambda code: code + "=",
        lambda code: code[:20] + "!" + code[20:],
        lambda code: _recoded(code),  # spaces after the separators
        lambda code: _recoded(code, n=""),
        lambda code: _recoded(code, p=True),
        lambda code: "swbp1_",
        lambda code: "not a code",
        lambda code: "",
    ],
)
def test_a_code_not_in_its_one_encoding_is_refused(signer: Signer, variant: object) -> None:
    code = variant(_code())  # type: ignore[operator]
    assert not _switchboard.code_in_one_encoding(code)
    assert _refused(signer.body(code=code), signer) == delivery.BAD_CODE


def test_a_code_in_its_one_encoding_passes_the_code_check() -> None:
    assert _switchboard.code_in_one_encoding(_code())


def test_a_code_whose_payload_nests_too_deep_is_refused(signer: Signer) -> None:
    deep = "swbp1_" + _b64(("[" * (_switchboard.NEST_MOST + 1)).encode())
    assert not _switchboard.code_in_one_encoding(deep)


# --- The key and the signature ------------------------------------------------------------------


def test_a_key_id_this_release_does_not_pin_is_unknown(signer: Signer) -> None:
    with pytest.raises(delivery.Refused) as caught:
        delivery.verify(signer.body(), NONCE)
    assert caught.value.reason == delivery.UNKNOWN_KEY
    assert caught.value.unknown_key


def test_a_revoked_key_id_is_refused_before_it_is_looked_up(signer: Signer) -> None:
    with pytest.raises(delivery.Refused) as caught:
        delivery.verify(signer.body(), NONCE, keys=signer.keys, revoked=frozenset({signer.kid}))
    assert caught.value.reason == delivery.REVOKED_KEY
    assert not caught.value.unknown_key


def test_a_signature_by_another_key_under_this_key_id_is_forged(signer: Signer) -> None:
    other = Signer()
    body = other.body(kid=signer.kid)
    assert _refused(body, signer) == delivery.FORGED


def test_a_signature_for_another_request_is_forged(signer: Signer) -> None:
    body = signer.body(nonce=bytes(16))
    assert _refused(body, signer) == delivery.FORGED


def test_a_signature_lifted_onto_another_code_is_forged(signer: Signer) -> None:
    fields = signer.fields()
    fields["code"] = _code()
    assert _refused(json.dumps(fields), signer) == delivery.FORGED


@pytest.mark.parametrize(
    ("field", "value"), [("from", "telegram:987654321"), ("handle", "@eve"), ("handle", "")]
)
def test_a_signature_lifted_onto_another_account_is_forged(
    signer: Signer, field: str, value: str
) -> None:
    fields = signer.fields()
    fields[field] = value
    assert _refused(json.dumps(fields), signer) == delivery.FORGED


def test_a_signature_without_the_label_is_forged(signer: Signer) -> None:
    code = _code()
    signed = delivery.signed_bytes(signer.kid, NONCE, code, "telegram:1", "@ada")
    unlabelled = signed[len(delivery.LABEL) + 1 :]
    body = signer.body(code=code, sender="telegram:1", message=unlabelled)
    assert _refused(body, signer) == delivery.FORGED


def test_a_refusal_carries_nothing_from_the_post(signer: Signer) -> None:
    fields = signer.fields(handle="@secret_handle", sender="telegram:424242")
    fields["sig"] = "A" * 86
    with pytest.raises(delivery.Refused) as caught:
        delivery.verify(json.dumps(fields), NONCE, keys=signer.keys)
    for part in ("secret_handle", "424242", fields["code"], signer.kid):
        assert part not in str(caught.value)
        assert part not in repr(caught.value)


# --- The keys a release pins, checked when the host loads ---------------------------------------


def _id(raw: bytes) -> str:
    return hashlib.sha256(raw).digest()[:8].hex()


def test_the_release_keys_pass_the_load_time_checks() -> None:
    pinned = _switchboard._pinned(
        dict(_switchboard.LUCILLE_SIGNING_KEYS), _switchboard.LUCILLE_REVOKED_KEY_IDS
    )
    assert dict(pinned) == dict(_switchboard.LUCILLE_SIGNING_KEYS)
    assert isinstance(_switchboard.LUCILLE_SIGNING_KEYS, type(pinned))


@pytest.mark.parametrize("count", [0, 3])
def test_a_release_pins_one_or_two_keys(count: int) -> None:
    keys = {_id(raw): raw for raw in (os.urandom(32) for _ in range(count))}
    with pytest.raises(ValueError, match="one or two"):
        _switchboard._pinned(keys, frozenset())


def test_two_keys_may_be_pinned() -> None:
    keys = {_id(raw): raw for raw in (os.urandom(32), os.urandom(32))}
    assert len(_switchboard._pinned(keys, frozenset())) == 2


@pytest.mark.parametrize("raw", [os.urandom(31), os.urandom(33), b""])
def test_a_key_that_is_not_32_bytes_is_refused(raw: bytes) -> None:
    with pytest.raises(ValueError, match="32 raw bytes"):
        _switchboard._pinned({_id(raw): raw}, frozenset())


def test_a_key_given_as_a_bytearray_is_refused() -> None:
    raw = os.urandom(32)
    with pytest.raises(ValueError, match="32 raw bytes"):
        _switchboard._pinned({_id(raw): bytearray(raw)}, frozenset())  # type: ignore[dict-item]


def test_a_key_under_an_id_its_bytes_do_not_give_is_refused() -> None:
    raw = os.urandom(32)
    with pytest.raises(ValueError, match="id its bytes do not give"):
        _switchboard._pinned({_id(os.urandom(32)): raw}, frozenset())
    # An id with a letter in it, so that its upper case is another id.
    while _id(raw) == _id(raw).upper():
        raw = os.urandom(32)
    with pytest.raises(ValueError, match="id its bytes do not give"):
        _switchboard._pinned({_id(raw).upper(): raw}, frozenset())


def test_a_key_both_pinned_and_revoked_is_refused() -> None:
    raw = os.urandom(32)
    with pytest.raises(ValueError, match="pinned and revoked"):
        _switchboard._pinned({_id(raw): raw}, frozenset({_id(raw)}))


@pytest.mark.parametrize("revoked", ["491E2CD6F9C3555D", "491e2cd6", "", "x" * 16])
def test_a_revoked_id_out_of_its_form_is_refused(revoked: str) -> None:
    raw = os.urandom(32)
    with pytest.raises(ValueError, match="revoked key id"):
        _switchboard._pinned({_id(raw): raw}, frozenset({revoked}))


def test_the_release_revokes_no_key_yet() -> None:
    assert frozenset() == _switchboard.LUCILLE_REVOKED_KEY_IDS


# --- The order of the checks (decisions/0002 §11.5) ---------------------------------------------

# The checks on a parsed body, in the order verify makes them. A post that fails two of them is
# refused for the earlier one: the key checks come after every form check, and the revoked list
# before the pinned keys, so that an unknown key id (which the app reads as Lucille perhaps
# needing a newer Capsule) is reported only for a post that is otherwise in form and not revoked.
ORDER = (
    delivery.WRONG_KEYS,
    delivery.NOT_STRING,
    delivery.WRONG_TYPE,
    delivery.BAD_KID,
    delivery.BAD_FROM,
    delivery.BAD_HANDLE,
    delivery.BAD_SIG,
    delivery.BAD_CODE,
    delivery.REVOKED_KEY,
    delivery.UNKNOWN_KEY,
    delivery.FORGED,
)


class Case:
    """One post, and the keys and revoked ids it is checked against."""

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self.fields: dict[str, object] = dict(signer.fields())
        self.keys: dict[str, bytes] = signer.keys
        self.revoke = False

    def breaks(self, reason: str) -> None:
        """Make the post fail the check ``reason`` names, and only that one by itself."""
        fields = self.fields
        if reason == delivery.WRONG_KEYS:
            fields["extra"] = ""
        elif reason == delivery.NOT_STRING:
            fields["from"] = 1
        elif reason == delivery.WRONG_TYPE:
            fields["t"] = "lucille-host/hello/1"
        elif reason == delivery.BAD_KID:
            # Sixteen characters, but the last is no hex digit: upper case alone would change
            # nothing in an id of digits only.
            fields["kid"] = str(fields["kid"])[:-1] + "g"
        elif reason == delivery.BAD_FROM:
            fields["from"] = "Telegram:123456789"
        elif reason == delivery.BAD_HANDLE:
            fields["handle"] = "ada"
        elif reason == delivery.BAD_SIG:
            fields["sig"] = str(fields["sig"]) + "="
        elif reason == delivery.BAD_CODE:
            fields["code"] = str(fields["code"]) + "\n"
        elif reason == delivery.REVOKED_KEY:
            # Revoked whatever id the post ends up naming, so that a post whose id another breaker
            # changed still fails this check too.
            self.revoke = True
        elif reason == delivery.UNKNOWN_KEY:
            self.keys = {}
        elif reason == delivery.FORGED:
            code = str(fields["code"])
            fields["sig"] = self.signer.fields(code=code, nonce=bytes(16))["sig"]
        else:  # pragma: no cover - a reason the table does not know
            raise AssertionError(reason)

    @property
    def revoked(self) -> frozenset[str]:
        if not self.revoke:
            return frozenset()
        return frozenset({self.signer.kid, str(self.fields["kid"])})

    def refusal(self) -> delivery.Refused:
        with pytest.raises(delivery.Refused) as caught:
            delivery.verify(json.dumps(self.fields), NONCE, keys=self.keys, revoked=self.revoked)
        return caught.value


@pytest.mark.parametrize("reason", ORDER)
def test_each_breaker_fails_only_its_own_check(signer: Signer, reason: str) -> None:
    case = Case(signer)
    case.breaks(reason)
    assert case.refusal().reason == reason


PAIRS = [(ORDER[i], ORDER[j]) for i in range(len(ORDER)) for j in range(i + 1, len(ORDER))]


@pytest.mark.parametrize(("earlier", "later"), PAIRS)
def test_a_post_failing_two_checks_is_refused_for_the_earlier(
    signer: Signer, earlier: str, later: str
) -> None:
    case = Case(signer)
    case.breaks(later)
    case.breaks(earlier)
    refusal = case.refusal()
    assert refusal.reason == earlier
    assert refusal.unknown_key is (earlier == delivery.UNKNOWN_KEY)


def test_a_malformed_key_id_that_is_also_revoked_fails_both_and_is_reported_malformed(
    signer: Signer,
) -> None:
    case = Case(signer)
    case.breaks(delivery.REVOKED_KEY)
    case.breaks(delivery.BAD_KID)
    assert str(case.fields["kid"]) in case.revoked
    assert case.refusal().reason == delivery.BAD_KID


def test_a_revoked_and_unknown_key_id_is_reported_revoked_not_unknown(signer: Signer) -> None:
    case = Case(signer)
    case.breaks(delivery.UNKNOWN_KEY)
    case.breaks(delivery.REVOKED_KEY)
    refusal = case.refusal()
    assert refusal.reason == delivery.REVOKED_KEY
    assert not refusal.unknown_key


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"\xff" * (delivery.MOST_BYTES + 1), delivery.NOT_TEXT),
        ("\ud800" + "[" * delivery.MOST_BYTES, delivery.NOT_UTF8),
        ("[" * (delivery.MOST_BYTES + 1), delivery.TOO_LARGE),
        ('{"a": [', delivery.TOO_DEEP),
        ('{"t": 1, "t": 2', delivery.NOT_JSON),
        ('{"t": 1, "t": 2}', delivery.KEY_TWICE),
        ('["t"]', delivery.NOT_OBJECT),
        ('{"t": 1}', delivery.WRONG_KEYS),
    ],
)
def test_the_body_checks_come_in_order(signer: Signer, body: object, reason: str) -> None:
    # Each body fails the check named and the ones after it that it can reach.
    assert _refused(body, signer) == reason


# --- What a refusal keeps -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('{"secret": "\ud800"}', delivery.NOT_UTF8),
        ('{"secret": "x"', delivery.NOT_JSON),
        ('{"secret": 1, "secret": 2}', delivery.KEY_TWICE),
    ],
)
def test_a_refusal_has_no_context_that_holds_the_body(
    signer: Signer, body: str, reason: str
) -> None:
    with pytest.raises(delivery.Refused) as caught:
        delivery.verify(body, NONCE, keys=signer.keys)
    assert caught.value.reason == reason
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


# --- The pinned keys cannot change after load ---------------------------------------------------


def test_the_pinned_keys_are_a_read_only_copy() -> None:
    raw = os.urandom(32)
    given = {_id(raw): raw}
    pinned = _switchboard._pinned(given, frozenset())
    with pytest.raises(TypeError):
        pinned[_id(raw)] = bytes(32)  # type: ignore[index]
    other = os.urandom(32)
    given[_id(other)] = other
    assert _id(other) not in pinned
    assert len(pinned) == 1


def test_the_release_keys_and_revoked_ids_cannot_be_changed() -> None:
    with pytest.raises(TypeError):
        _switchboard.LUCILLE_SIGNING_KEYS["0" * 16] = bytes(32)  # type: ignore[index]
    assert isinstance(_switchboard.LUCILLE_REVOKED_KEY_IDS, frozenset)
    assert not hasattr(_switchboard.LUCILLE_REVOKED_KEY_IDS, "add")


def test_a_key_id_matching_only_a_prefix_of_its_bytes_id_is_refused() -> None:
    raw = os.urandom(32)
    real = _id(raw)
    for kept in (8, 15):
        wrong = real[:kept] + "".join("0" if c != "0" else "1" for c in real[kept:])
        with pytest.raises(ValueError, match="id its bytes do not give"):
            _switchboard._pinned({wrong: raw}, frozenset())


# --- The length guard in signature_valid --------------------------------------------------------


def test_signature_valid_refuses_wrong_lengths_before_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the library answering yes to everything, only the guard stands.
    monkeypatch.setattr("lucille_host._switchboard.signing.verify", lambda *args: True)
    assert _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG)
    assert not _switchboard.signature_valid(RFC_KEY[:31], b"", RFC_SIG)
    assert not _switchboard.signature_valid(RFC_KEY + b"\x00", b"", RFC_SIG)
    assert not _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG[:63])
    assert not _switchboard.signature_valid(RFC_KEY, b"", RFC_SIG + b"\x00")
