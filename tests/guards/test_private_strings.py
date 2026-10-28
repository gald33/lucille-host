"""tools/guards/private_strings.py reports hits without publishing them, and fails closed.

Every list entry here is generated at run time, so no entry appears in any file of the repository,
this one included. Each is random throughout, so that a check for any short piece of it in the
output means something: a report that printed part of an entry would publish part of the string.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from tools.guards import private_strings
from tools.guards.private_strings import Entry, parse_entries

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "guards" / "private_strings.py"


# The shortest piece of an entry the output may not hold.
WINDOW = 6


def synthetic_entry() -> str:
    """A harmless random string that occurs in no file: it does not exist until the test runs.

    32 characters from a 64-character alphabet, random throughout, with no fixed prefix. A given
    six-character piece of it matches a given place in the output by chance once in 64**6, about
    7 * 10**10, so a few hundred characters of output cannot hold one by accident.
    """
    return secrets.token_urlsafe(24)


def assert_not_leaked(entry: str, output: str) -> None:
    """No piece of ``entry`` WINDOW characters long appears in ``output``."""
    assert len(entry) >= WINDOW
    for start in range(len(entry) - WINDOW + 1):
        piece = entry[start : start + WINDOW]
        assert piece not in output, f"characters {start}..{start + WINDOW} of the entry leaked"


def run_script(
    root: Path, raw_list: str | None, fork: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (private_strings.LIST_VARIABLE, private_strings.FORK_VARIABLE)
    }
    if raw_list is not None:
        env[private_strings.LIST_VARIABLE] = raw_list
    if fork is not None:
        env[private_strings.FORK_VARIABLE] = fork
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_hit_is_reported_without_the_string_or_the_line(tmp_path: Path) -> None:
    entry = synthetic_entry()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text(f"first line\nsecond line\nthe {entry} sits here\n")
    other = synthetic_entry()
    result = run_script(tmp_path, f"# a comment\n{other}\n{entry}\n")
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "docs/notes.md:3: [private-string] matches list entry 3" in output
    assert_not_leaked(entry, output)
    assert_not_leaked(other, output)
    assert "sits here" not in output


# Files a careless walk might skip: a dot-directory, configuration and lock files, a file with no
# extension, and a large file.
HIDDEN_PLACES = [
    ".github/workflows/x.yml",
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    "src/lucille_host/py.typed",
]


@pytest.mark.parametrize("rel", HIDDEN_PLACES)
def test_hit_is_found_wherever_the_file_is(tmp_path: Path, rel: str) -> None:
    entry = synthetic_entry()
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"plain\n{entry}\n")
    result = run_script(tmp_path, entry)
    assert result.returncode == 1
    assert f"{rel}:2: [private-string] matches list entry 1" in result.stdout
    assert_not_leaked(entry, result.stdout + result.stderr)


def test_hit_deep_in_a_large_file_is_found(tmp_path: Path) -> None:
    entry = synthetic_entry()
    filler = "x" * 99 + "\n"
    (tmp_path / "big.txt").write_text(filler * 2000 + f"{entry}\n")  # about 200 KiB
    found = private_strings.check(tmp_path, parse_entries(entry))
    assert [(v.path, v.line) for v in found] == [("big.txt", 2001)]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_in_a_git_tree_tracked_files_are_read(tmp_path: Path) -> None:
    entry = synthetic_entry()
    _git(tmp_path, "init", "-q")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(f"name: ci\n# {entry}\n")
    (tmp_path / "README.md").write_text("plain\n")
    (tmp_path / "notes.txt").write_text(f"{entry}\n")  # never added: not part of the repository
    _git(tmp_path, "add", ".github/workflows/ci.yml", "README.md")
    assert private_strings.tracked_files(tmp_path) == [".github/workflows/ci.yml", "README.md"]
    result = run_script(tmp_path, entry)
    assert result.returncode == 1
    assert ".github/workflows/ci.yml:2: [private-string] matches list entry 1" in result.stdout
    assert "notes.txt" not in result.stdout


def test_hit_in_a_file_path_withholds_the_path(tmp_path: Path) -> None:
    entry = synthetic_entry()
    (tmp_path / f"{entry}.txt").write_text("nothing private inside\n")
    (tmp_path / "other.txt").write_text("plain\n")
    result = run_script(tmp_path, entry)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "path withheld" in output
    assert "list entry 1" in output
    assert_not_leaked(entry, output)


def test_comment_and_blank_lines_are_not_entries(tmp_path: Path) -> None:
    word = synthetic_entry()
    entry = synthetic_entry()
    raw = f"# {word}\n\n   \n#{word}\n  {entry}  \n"
    assert parse_entries(raw) == [Entry(5, entry)]
    (tmp_path / "file.txt").write_text(f"# {word}\n#{word}\n")
    assert run_script(tmp_path, raw).returncode == 0


def test_hit_on_a_non_ascii_line_is_found(tmp_path: Path) -> None:
    entry = synthetic_entry()
    (tmp_path / "utf8.txt").write_text(f"plain\ncaf\u00e9 {entry}\n", encoding="utf-8")
    (tmp_path / "latin1.txt").write_bytes(b"plain\ncaf\xe9 " + entry.encode() + b"\n")
    found = private_strings.check(tmp_path, parse_entries(entry))
    assert [(v.path, v.line) for v in found] == [("latin1.txt", 2), ("utf8.txt", 2)]


def test_an_entry_that_is_not_ascii_is_found(tmp_path: Path) -> None:
    entry = "caf\u00e9-" + synthetic_entry()
    (tmp_path / "file.txt").write_text(f"plain\nthe {entry} here\n", encoding="utf-8")
    found = private_strings.check(tmp_path, parse_entries(entry))
    assert [(v.path, v.line) for v in found] == [("file.txt", 2)]


def test_hit_on_a_last_line_without_a_newline_is_found(tmp_path: Path) -> None:
    entry = synthetic_entry()
    (tmp_path / "file.txt").write_text(f"plain\nends with {entry}")
    found = private_strings.check(tmp_path, parse_entries(entry))
    assert [(v.path, v.line) for v in found] == [("file.txt", 2)]


def test_a_symbolic_link_is_checked_by_the_path_it_holds(tmp_path: Path) -> None:
    entry = synthetic_entry()
    _git(tmp_path, "init", "-q")
    (tmp_path / "plain.txt").write_text("plain\n")
    os.symlink(f"../{entry}/target", tmp_path / "dangling")
    os.symlink("plain.txt", tmp_path / "to-a-file")
    _git(tmp_path, "add", "plain.txt", "dangling", "to-a-file")
    assert private_strings.tracked_files(tmp_path) == ["dangling", "plain.txt", "to-a-file"]
    result = run_script(tmp_path, entry)
    assert result.returncode == 1
    assert "dangling:1: [private-string] matches list entry 1" in result.stdout
    assert "3 file(s) checked" in result.stdout
    assert_not_leaked(entry, result.stdout + result.stderr)


def test_a_symbolic_link_outside_git_is_checked_too(tmp_path: Path) -> None:
    entry = synthetic_entry()
    root = tmp_path / "tree"
    root.mkdir()
    (tmp_path / entry).mkdir()  # a real directory, outside the tree
    (root / "plain.txt").write_text("plain\n")
    os.symlink(f"/nowhere/{entry}", root / "dangling")
    os.symlink(f"../{entry}", root / "dir-link")  # listed by the walk, not followed
    found = private_strings.check(root, parse_entries(entry))
    assert [(v.path, v.line) for v in found] == [("dangling", 1), ("dir-link", 1)]


def test_binary_file_contents_are_skipped(tmp_path: Path) -> None:
    entry = synthetic_entry()
    (tmp_path / "blob.bin").write_bytes(b"\0\1" + entry.encode())
    (tmp_path / "text.txt").write_text("plain\n")
    assert private_strings.check(tmp_path, parse_entries(entry)) == []


def test_fork_pull_request_without_the_list_is_skipped_and_says_so(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("plain\n")
    result = run_script(tmp_path, "", fork="true")
    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert "fork" in result.stdout
    # The skip is also an annotation, so it shows on the pull request's checks page.
    assert any(line.startswith("::warning ") for line in result.stdout.splitlines())


def test_the_leak_check_catches_a_partial_leak() -> None:
    entry = synthetic_entry()
    with pytest.raises(AssertionError):
        assert_not_leaked(entry, f"report: ...{entry[10:16]}...")


def test_missing_list_outside_a_fork_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("plain\n")
    for raw_list, fork in [(None, None), ("", None), ("", "false"), ("# only a comment\n\n", None)]:
        result = run_script(tmp_path, raw_list, fork)
        assert result.returncode == 1, (raw_list, fork)
        assert "not set or holds no entries" in result.stderr


def test_clean_tree_passes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("print('plain')\n")
    result = run_script(tmp_path, f"# list\n{synthetic_entry()}\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_usage_errors(tmp_path: Path) -> None:
    env_list = synthetic_entry()
    assert run_script(tmp_path / "missing", env_list).returncode == 2
    assert run_script(tmp_path, env_list).returncode == 2  # an empty tree: nothing to check


def test_repository_is_clean_against_a_synthetic_list() -> None:
    """Runs without the real secret, so it shows only that the check reads this repository.

    A synthetic list cannot show that no private string is committed. Only the CI step
    "Private-string check", run with the repository secret, checks that.
    """
    raw = f"# synthetic list\n\n{synthetic_entry()}\n{synthetic_entry()}\n"
    assert private_strings.tracked_files(REPO_ROOT)
    assert private_strings.check(REPO_ROOT, parse_entries(raw)) == []
    assert run_script(REPO_ROOT, raw).returncode == 0
