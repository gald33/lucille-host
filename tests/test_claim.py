"""The claim ships with the program, and is the text PRIVACY.md and the README give.

``lucille-host status`` prints it (``tests/test_status.py``). PRIVACY.md and the README are not in
the wheel, so the text the program prints lives in ``src/lucille_host/claim.txt``. These tests hold
the three to the same text, so that none of them can change alone.
"""

from __future__ import annotations

from pathlib import Path

from lucille_host import report

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVACY = REPO_ROOT / "PRIVACY.md"
README = REPO_ROOT / "README.md"


def claim_in(path: Path, heading: str) -> str:
    """The one quoted block (``> `` lines) of the section ``heading`` of a Markdown file, with its
    quoting taken off: each line ended by a newline, a ``>`` alone an empty line."""
    text = path.read_text(encoding="utf-8")
    assert text.count(f"\n{heading}\n") == 1, f"{path.name} has one section {heading!r}"
    lines = text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0].splitlines()
    quoted = [index for index, line in enumerate(lines) if line.startswith(">")]
    assert quoted, f"{path.name}: no quoted block under {heading!r}"
    assert quoted == list(range(quoted[0], quoted[-1] + 1)), (
        f"{path.name}: more than one quoted block under {heading!r}"
    )
    unquoted = []
    for index in quoted:
        line = lines[index]
        assert line == ">" or line.startswith("> "), f"{path.name}: {line!r}"
        unquoted.append(line[2:] + "\n")
    return "".join(unquoted)


def test_the_shipped_claim_is_privacy_md_s() -> None:
    assert report.claim() == claim_in(PRIVACY, "## The claim")


def test_the_shipped_claim_is_the_readme_s() -> None:
    assert report.claim() == claim_in(README, "## What lucille-host claims")


def test_the_shipped_claim_is_plain_text_in_the_package() -> None:
    # Beside report.py, so that the wheel carries it (tests/spec/test_packaged.py builds one).
    # Wherever the package is installed: the Mac app's job runs this suite on a copy of its bundled
    # runtime, where lucille_host is in site-packages, not in this checkout's src.
    assert report.CLAIM_FILE == Path(report.__file__).with_name("claim.txt")
    raw = report.CLAIM_FILE.read_bytes()
    assert raw.decode("ascii") == report.claim()
    lines = report.claim().split("\n")
    # Lines ended by a newline, none with trailing space; paragraphs split by one empty line.
    assert lines[-1] == "" and lines[0] and lines[-2]
    assert all(line == line.rstrip() for line in lines)
    assert "\n\n\n" not in report.claim() and "\r" not in report.claim()
    assert report.claim().startswith("lucille-host does only what you switch on, on your computer.")
