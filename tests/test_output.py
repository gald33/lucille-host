"""Decoding, redaction and capping of a child's output (spec/PROTOCOL.md section 13)."""

from __future__ import annotations

import base64
import json
import random

import pytest

from lucille_host import _output, spec


@pytest.mark.parametrize("path", spec.fixture_paths("decode"), ids=lambda p: p.stem)
def test_decode_holds_to_the_fixtures(path) -> None:  # type: ignore[no-untyped-def]
    fixture = spec.load_fixture(path)
    assert _output.decode(base64.b64decode(fixture["raw_base64"])) == fixture["text"]


def test_char_size_is_the_json_encoded_size_of_every_character() -> None:
    for code in range(0x110000):
        char = chr(code)
        assert _output._char_size(char) == _output.json_size(char) - 2, hex(code)


def test_cap_drops_whole_characters_from_the_end() -> None:
    text = "ab\U0001f600cd"
    assert _output.cap(text, 2) == ("", True)
    assert _output.cap(text, 4) == ("ab", True)
    # The emoji measures 12 bytes ("😀"): it goes whole or not at all.
    assert _output.cap(text, 15) == ("ab", True)
    assert _output.cap(text, 16) == ("ab\U0001f600", True)
    assert _output.cap(text, 18) == (text, False)
    with pytest.raises(ValueError):
        _output.cap(text, 1)


def test_capped_text_always_fits_and_is_the_longest_prefix_that_does() -> None:
    rng = random.Random(1)
    alphabet = ['"', "\\", "\n", "\x01", "a", "\x7f", "é", "東", "\U0001f680"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 60)))
        limit = rng.randrange(2, 200)
        capped, truncated = _output.cap(text, limit)
        assert text.startswith(capped)
        assert _output.json_size(capped) <= limit
        assert truncated is (capped != text)
        if truncated:
            assert _output.json_size(text[: len(capped) + 1]) > limit


def _fake_key_block(label: str, terminated: bool) -> str:
    block = f"-----BEGIN {label}-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    return block + (f"-----END {label}-----\n" if terminated else "")


def test_redact_replaces_private_key_blocks() -> None:
    for label in ("PRIVATE KEY", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
        text = "before\n" + _fake_key_block(label, terminated=True) + "after\n"
        redacted = _output.redact(text)
        assert redacted == f"before\n{_output.PRIVATE_KEY_MARK}\nafter\n", label
    # A certificate is public and stays.
    cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    assert _output.redact(cert) == cert


def test_redact_runs_an_unterminated_block_to_the_end() -> None:
    text = "head\n" + _fake_key_block("EC PRIVATE KEY", terminated=False) + "more\nand more\n"
    assert _output.redact(text) == "head\n" + _output.PRIVATE_KEY_MARK


def test_redact_replaces_common_key_formats() -> None:
    # Built at run time, so no token-shaped literal sits in the repository.
    samples = [
        "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "gh" + "p_" + "a1" * 18,
        "github" + "_pat_" + "x" * 30,
        "sk" + "-" + "proj_" + "z" * 30,
        "xo" + "xb-" + "1234567890-abc",
    ]
    for token in samples:
        redacted = _output.redact(f"token={token} end")
        assert redacted == f"token={_output.TOKEN_MARK} end", token
    # Not inside a longer word.
    word = "mask-" + "a" * 30
    assert _output.redact(word) == word


def test_render_order_removes_nul_and_redacts_before_capping() -> None:
    # A megabyte of NUL is removed before the cap is measured, so what follows still fits.
    raw = b"\x00" * (1024 * 1024) + b"tail"
    assert _output.render(raw, 64) == ("tail", False)
    # A redacted block shrinks before the cap is measured.
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n" * 4000
    block = f"-----BEGIN PRIVATE KEY-----\n{body}-----END PRIVATE KEY-----\n"
    text, truncated = _output.render((block + "tail").encode(), 200)
    assert (text, truncated) == (f"{_output.PRIVATE_KEY_MARK}\ntail", False)


def test_render_flags_a_stream_the_host_stopped_keeping() -> None:
    assert _output.render(b"short", 100, dropped=True) == ("short", True)


def test_a_full_stream_measures_at_most_the_cap() -> None:
    text, truncated = _output.render(("é" * 20000).encode(), _output.STREAM_CAP)
    assert truncated
    assert len(json.dumps(text)) <= _output.STREAM_CAP
