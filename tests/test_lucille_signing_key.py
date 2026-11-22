"""Lucille's pinned signing keys (decisions/0002 §11.5).

The host checks a connect delivery's signature against a key the release pins, named by its key
id. These tests hold the pinned set to its rules: at most two keys, each 32 raw bytes that are a
point of Ed25519's prime-order group, each under the id its bytes give. The key itself is written
here a second time, in the base64url form it was handed over in, so that changing it takes two
edits that agree.
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lucille_host import _switchboard

# Lucille's key as it was handed over: the 32 raw bytes, in base64url.
HANDED_OVER = "MTJWcXysbK4_T38MXiDqSuPwww0meyI9Uv_NQNQi4yY="

# Ed25519's field prime, curve constant and group order (RFC 8032, section 5.1).
P = 2**255 - 19
D = -121665 * pow(121666, -1, P) % P
L = 2**252 + 27742317777372353535851937790883648493


def _point(raw: bytes) -> tuple[int, int] | None:
    """Decode a 32-byte encoded point as RFC 8032 section 5.1.3 does, or None if it is none."""
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        return None
    u = (y * y - 1) % P
    v = (D * y * y + 1) % P
    x = u * pow(v, 3, P) * pow(u * pow(v, 7, P), (P - 5) // 8, P) % P
    if (v * x * x - u) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
        if (v * x * x - u) % P:
            return None
    if x == 0 and sign:
        return None
    if x % 2 != sign:
        x = P - x
    return x, y


def _add(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    """Add two points of the curve, in affine coordinates."""
    (x1, y1), (x2, y2) = a, b
    t = D * x1 * x2 * y1 * y2 % P
    return (
        (x1 * y2 + x2 * y1) * pow(1 + t, -1, P) % P,
        (y1 * y2 + x1 * x2) * pow(1 - t, -1, P) % P,
    )


def _times(k: int, point: tuple[int, int]) -> tuple[int, int]:
    """Multiply a point by a scalar, by doubling and adding."""
    total = (0, 1)
    while k:
        if k & 1:
            total = _add(total, point)
        point = _add(point, point)
        k >>= 1
    return total


def test_at_least_one_and_at_most_two_keys_are_pinned() -> None:
    assert 1 <= len(_switchboard.LUCILLE_SIGNING_KEYS) <= 2


def test_the_pinned_set_cannot_be_changed_at_run_time() -> None:
    keys = _switchboard.LUCILLE_SIGNING_KEYS
    with pytest.raises(TypeError):
        keys["0000000000000000"] = bytes(32)  # type: ignore[index]
    assert "0000000000000000" not in keys


def test_each_key_id_is_derived_from_its_key_bytes() -> None:
    for kid, raw in _switchboard.LUCILLE_SIGNING_KEYS.items():
        assert isinstance(raw, bytes)
        assert len(raw) == 32
        assert re.fullmatch(r"[0-9a-f]{16}", kid)
        assert kid == hashlib.sha256(raw).digest()[:8].hex()


def test_the_pinned_key_is_the_one_handed_over() -> None:
    raw = base64.urlsafe_b64decode(HANDED_OVER)
    assert len(raw) == 32
    assert base64.urlsafe_b64encode(raw).decode("ascii") == HANDED_OVER
    assert hashlib.sha256(raw).digest()[:8].hex() == "491e2cd6f9c3555d"
    assert _switchboard.LUCILLE_SIGNING_KEYS["491e2cd6f9c3555d"] == raw


def test_each_key_is_a_point_of_the_prime_order_group() -> None:
    for raw in _switchboard.LUCILLE_SIGNING_KEYS.values():
        point = _point(raw)
        assert point is not None, "not the encoding of a curve point"
        assert _times(8, point) != (0, 1), "a point of small order"
        assert _times(L, point) == (0, 1), "not in the prime-order group"
        Ed25519PublicKey.from_public_bytes(raw)


def test_the_point_check_refuses_what_it_should() -> None:
    # The identity, a point of order 8 and a y past the field prime are each refused.
    identity = (1).to_bytes(32, "little")
    assert _point(identity) == (0, 1)
    assert _times(8, (0, 1)) == (0, 1)
    order_8 = bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a")
    point = _point(order_8)
    assert point is not None
    assert _times(8, point) == (0, 1)
    assert _point((P).to_bytes(32, "little")) is None
    # The RFC 8032 test 1 public key is in the prime-order group.
    rfc = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    rfc_point = _point(rfc)
    assert rfc_point is not None
    assert _times(L, rfc_point) == (0, 1)
