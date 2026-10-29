"""Containment: a request's path stays inside its folder, resolved as the kernel resolves it."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import random
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lucille_host import containment, mounts, paths
from lucille_host.cli import main
from lucille_host.containment import MAX_LINKS, contain
from lucille_host.jobs import Job, check, process
from tests._host import MakeContext


def _deep_link_out(tmp_path: Path) -> tuple[Path, str, bytes]:
    """A folder whose real path is about a quarter of PATH_MAX, holding a chain of folders with, at
    its bottom, a link to a folder outside it. The chain is long enough that the folder's real path
    plus the request's path passes PATH_MAX, while each alone stays under it: on Linux (PATH_MAX
    4096) the request's path is 13 components of 63 emoji, 844 characters and 3301 bytes; on macOS
    (1024) it is 3. Everything is made one name at a time from a descriptor, so that no call here
    is given a path longer than PATH_MAX either."""
    path_max = os.pathconf(tmp_path, "PC_PATH_MAX")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = b"outside the root\n"
    (outside / "secret.txt").write_bytes(secret)
    root = tmp_path
    while len(os.fsencode(root)) < path_max // 4:
        room = path_max // 4 - len(os.fsencode(root)) - 1
        root = root / ("r" + "x" * min(max(room - 1, 0), 239))
        root.mkdir()
    component = "\U0001f600" * 63
    suffix = "/L/secret.txt"
    count = (path_max - len(os.fsencode(root)) - len(suffix)) // (len(os.fsencode(component)) + 1)
    count += 1
    here = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(count):
            os.mkdir(component, dir_fd=here)
            below = os.open(component, os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
            os.close(here)
            here = below
        os.symlink(str(outside), "L", dir_fd=here)
    finally:
        os.close(here)
    path = "/".join([component] * count) + suffix
    assert len(os.fsencode(root)) + 1 + len(os.fsencode(path)) > path_max
    assert len(os.fsencode(path)) < path_max and len(path) <= 1024
    return root, path, secret


def test_a_link_past_path_max_is_still_followed(tmp_path: Path) -> None:
    root, path, _ = _deep_link_out(tmp_path)
    assert contain(str(root), path) is None
    # The same chain without the link out stays inside: the refusal is the link's.
    assert contain(str(root), path.replace("/L/secret.txt", "/missing")) == os.path.realpath(root)


def test_files_sha256_never_hashes_a_file_outside_the_root(
    tmp_path: Path, make_context: MakeContext
) -> None:
    root, path, secret = _deep_link_out(tmp_path)
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id="9" * 8, op="files.sha256", args={"root": "docs", "path": path})
    assert check(job, ctx) == "path_outside_root"
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")
    assert hashlib.sha256(secret).hexdigest() not in json.dumps(reply)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "a" / "b").mkdir(parents=True)
    outside.mkdir()
    (root / "a" / "b" / "f.txt").write_text("f")
    (root / "file.txt").write_text("x")
    (outside / "o.txt").write_text("o")
    return root, outside


def test_links_that_stay_inside(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    real = os.path.realpath(root)
    (root / "rel").symlink_to("a/b")
    (root / "a" / "up").symlink_to("../file.txt")
    (root / "abs-in").symlink_to(Path(real) / "a" / "b" / "f.txt")
    (root / "self").symlink_to(".")
    (root / "chain1").symlink_to("chain2")
    (root / "chain2").symlink_to("a/b/f.txt")
    for path in ("rel/f.txt", "a/up", "abs-in", "self/self/file.txt", "chain1", "a/./b/f.txt"):
        assert contain(str(root), path) == real, path


def test_links_that_lead_out(tree: tuple[Path, Path]) -> None:
    root, outside = tree
    (root / "abs-out").symlink_to(outside / "o.txt")
    (root / "slash").symlink_to("/")
    (root / "climb").symlink_to("../outside/o.txt")
    (root / "a" / "climb2").symlink_to("../../../outside")
    (root / "a" / "deep").symlink_to("b/../../../outside")
    (root / "loop1").symlink_to("loop2")
    (root / "loop2").symlink_to("loop1")
    for path in ("abs-out", "slash/etc", "climb", "a/climb2/o.txt", "a/deep", "loop1"):
        assert contain(str(root), path) is None, path


def test_too_many_links_is_an_escape(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    for index in range(MAX_LINKS + 1):
        (root / f"l{index}").symlink_to(f"l{index + 1}" if index < MAX_LINKS else "file.txt")
    assert contain(str(root), f"l{MAX_LINKS - 39}") is not None
    assert contain(str(root), "l0") is None


def test_what_does_not_exist_cannot_lead_out(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    real = os.path.realpath(root)
    for path in ("missing.txt", "missing/deeper/x", "file.txt/x", "a/b/f.txt/y", ".", "a" * 300):
        assert contain(str(root), path) == real, path


def test_requests_that_are_escapes_by_their_form(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    for path in ("/etc/passwd", "../outside", "a/../../outside", "a/b/../..", "\ud800"):
        assert contain(str(root), path) is None, path


def test_a_folder_the_host_cannot_open_contains_nothing(tmp_path: Path) -> None:
    assert contain(str(tmp_path / "gone"), "a.txt") is None
    (tmp_path / "file").write_text("not a folder")
    assert contain(str(tmp_path / "file"), "a.txt") is None


def test_agrees_with_realpath_wherever_the_path_resolves(tmp_path: Path) -> None:
    """Random trees of folders, files and links (relative and absolute, in and out, some
    climbing), and random paths through them: wherever the path fully resolves, contain() says
    inside exactly when its real path is inside the folder."""
    rng = random.Random(7)
    for trial in range(40):
        base = tmp_path / f"t{trial}"
        root = base / "root"
        outside = base / "outside"
        root.mkdir(parents=True)
        outside.mkdir()
        (outside / "o").write_text("o")
        real = os.path.realpath(root)
        folders = [Path(".")]
        names = ["a", "b", "c", "d"]
        for _ in range(6):
            parent = rng.choice(folders)
            folder = parent / rng.choice(names)
            (root / folder).mkdir(exist_ok=True)
            folders.append(folder)
            (root / folder / "f").write_text("f")
        targets = [
            "..",
            "../..",
            "../../outside",
            "a",
            "a/b",
            "f",
            ".",
            str(outside),
            str(Path(real) / "a"),
            "/",
        ]
        for index in range(8):
            folder = rng.choice(folders)
            link = root / folder / f"l{index}"
            if not os.path.lexists(link):
                link.symlink_to(rng.choice(targets))
        entries = ["a", "b", "c", "d", "f", "o", *(f"l{i}" for i in range(8))]
        for _ in range(60):
            path = "/".join(rng.choice(entries) for _ in range(rng.randrange(1, 5)))
            try:
                resolved = os.path.realpath(root / path, strict=True)
            except OSError:
                continue
            inside = resolved == real or resolved.startswith(real + "/")
            assert (contain(str(root), path) is not None) is inside, (trial, path, resolved)


def _deeper_than_path_max(tmp_path: Path) -> tuple[Path, Path, str]:
    """A folder holding 17 nested directories of 250-byte names, 4267 bytes deep, past PATH_MAX on
    Linux (4096) and on macOS (1024). Short links reach each level from the one above, so no
    lookup the child's kernel makes is long, while the joined path from the folder is."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    here = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for level in range(17):
            name = f"{level:02d}" + "d" * 248
            os.mkdir(name, dir_fd=here)
            os.symlink(name, f"s{level}", dir_fd=here)
            below = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
            os.close(here)
            here = below
        made = os.open("inside.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=here)
        os.write(made, b"inside")
        os.close(made)
        os.symlink(str(outside), "abs-out", dir_fd=here)
        os.symlink("../" * 18 + "outside", "rel-out", dir_fd=here)
    finally:
        os.close(here)
    chain = "/".join(f"s{level}" for level in range(17))
    return root, outside, chain


def test_a_tree_deeper_than_path_max_is_walked_one_name_at_a_time(tmp_path: Path) -> None:
    root, _, chain = _deeper_than_path_max(tmp_path)
    real = os.path.realpath(root)
    assert contain(str(root), f"{chain}/inside.txt") == real
    assert contain(str(root), f"{chain}/abs-out/secret.txt") is None
    assert contain(str(root), f"{chain}/rel-out/secret.txt") is None


def test_every_lookup_names_a_single_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes the walk agree with the kernel: each lookup it makes relative to a directory
    descriptor names one component, so a lookup fails only as the child's lookup of that name
    would. A lookup of a joined path could fail on its length alone. The one lookup of the whole
    path, following links, is the check made after the walk (the child's own lookup); it decides
    nothing alone, so it is left out here."""
    root, _, chain = _deeper_than_path_max(tmp_path)
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "up").symlink_to("../a/b")
    seen: list[str] = []
    real_stat, real_open, real_readlink = os.stat, os.open, os.readlink

    def record(real):  # type: ignore[no-untyped-def]
        def call(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            follows = real is real_stat and kwargs.get("follow_symlinks", True)
            if kwargs.get("dir_fd") is not None and not follows:
                seen.append(path)
            return real(path, *args, **kwargs)

        return call

    monkeypatch.setattr("os.stat", record(real_stat))
    monkeypatch.setattr("os.open", record(real_open))
    monkeypatch.setattr("os.readlink", record(real_readlink))
    for path in (f"{chain}/inside.txt", f"{chain}/rel-out/secret.txt", "a/up/x", "a/./b"):
        contain(str(root), path)
    assert seen and all("/" not in name and name not in ("", ".", "..") for name in seen)


def test_dot_dot_and_absolute_targets_restart_from_the_folder(tree: tuple[Path, Path]) -> None:
    """``..`` in a link climbs from the link's own directory, and an absolute target starts again
    at the folder: neither may resolve the next name in a deeper directory that happens to hold an
    entry of the same name."""
    root, outside = tree
    real = os.path.realpath(root)
    (root / "x").symlink_to(outside)
    (root / "a" / "x").write_text("a decoy of the same name, inside")
    (root / "a" / "rel").symlink_to("../x")
    (root / "a" / "abs").symlink_to(f"{real}/x")
    for path in ("a/rel", "a/rel/o.txt", "a/abs", "a/abs/o.txt"):
        assert contain(str(root), path) is None, path
    assert contain(str(root), "a/x") == real


def test_too_many_lookups_is_an_escape(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = tree
    assert contain(str(root), "a/b/f.txt") is not None
    monkeypatch.setattr(containment, "MAX_STEPS", 2)
    assert contain(str(root), "a/b/f.txt") is None


@pytest.mark.parametrize("code", [errno.EACCES, errno.ELOOP, errno.EIO])
def test_a_lookup_that_fails_otherwise_is_an_escape(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Only "nothing is there" ends the walk inside. Any other error, on looking a name up,
    reading a link or opening a directory, means the host could not check, and it runs nothing."""
    root, _ = tree
    (root / "link").symlink_to("a/b")
    real_stat, real_readlink, real_open = os.stat, os.readlink, os.open

    def failing(real, name):  # type: ignore[no-untyped-def]
        def call(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == name and kwargs.get("dir_fd") is not None:
                raise OSError(code, os.strerror(code))
            return real(path, *args, **kwargs)

        return call

    for function, real, name, path in (
        ("stat", real_stat, "b", "a/b/f.txt"),
        ("readlink", real_readlink, "link", "link/f.txt"),
        ("open", real_open, "b", "a/b/f.txt"),
    ):
        assert contain(str(root), path) is not None
        with monkeypatch.context() as patched:
            patched.setattr(f"os.{function}", failing(real, name))
            assert contain(str(root), path) is None, (function, code)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may open any directory")
def test_a_folder_the_host_may_search_but_not_read_is_an_escape(
    tree: tuple[Path, Path],
) -> None:
    """The child can pass through a directory it may only search, and follow a link out of it, so
    a directory the host cannot open is not checked as inside."""
    root, outside = tree
    search_only = root / "d"
    search_only.mkdir()
    (search_only / "out").symlink_to(outside)
    search_only.chmod(0o111)
    try:
        assert contain(str(root), "d/out/o.txt") is None
    finally:
        search_only.chmod(0o700)


def test_a_fifo_socket_or_device_is_refused(tree: tuple[Path, Path]) -> None:
    """Opening a FIFO blocks until the time limit or takes another program's data, and a device
    or socket is no file to hash: a path naming one is refused, directly or through a link."""
    root, _ = tree
    real = os.path.realpath(root)
    os.mkfifo(root / "pipe")
    (root / "to-pipe").symlink_to("pipe")
    for path in ("pipe", "to-pipe"):
        assert contain(str(root), path) is None, path
    # A name below a FIFO is no lookup the child can make: inside, as below a file.
    assert contain(str(root), "pipe/x") == real
    held = socket.socket(socket.AF_UNIX)
    try:
        here = os.getcwd()
        os.chdir(root)
        try:
            held.bind("sock")
        finally:
            os.chdir(here)
        assert stat.S_ISSOCK(os.lstat(root / "sock").st_mode)
        assert contain(str(root), "sock") is None
    finally:
        held.close()
    assert contain("/dev", "null") is None
    for path in ("file.txt", "a", "a/b", "missing"):
        assert contain(str(root), path) == real, path


def test_files_sha256_does_not_read_a_fifo(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "chan")
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id="8" * 8, op="files.sha256", args={"root": "docs", "path": "chan"})
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")


@pytest.fixture
def own(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A folder holding the host's own folder (its config and state), and a file in each."""
    above = tmp_path / "above"
    config = above / "lucille-host" / "default"
    config.mkdir(parents=True)
    (config / "policy.toml").write_text("version = 1\n")
    (above / "notes.txt").write_text("the user's")
    return above, above / "lucille-host", config


def test_no_path_reaches_the_hosts_own_folders(own: tuple[Path, Path, Path]) -> None:
    above, host_dir, config = own
    real = os.path.realpath(above)
    mine = (str(host_dir),)
    (above / "abs").symlink_to(config / "policy.toml")
    (above / "rel").symlink_to("lucille-host/default")
    (above / "sub").mkdir()
    (above / "sub" / "up").symlink_to("../lucille-host")
    for path in (
        "lucille-host",
        "lucille-host/default",
        "lucille-host/default/policy.toml",
        "lucille-host/missing",
        "abs",
        "rel/policy.toml",
        "sub/up/default/policy.toml",
    ):
        assert contain(str(above), path, own=mine) is None, path
        # Without them named, each of these is inside: the refusal is theirs.
        assert contain(str(above), path) == real, path
    assert contain(str(above), "notes.txt", own=mine) == real


def test_a_folder_inside_the_hosts_own_holds_nothing(own: tuple[Path, Path, Path]) -> None:
    above, host_dir, config = own
    for folder in (host_dir, config):
        assert contain(str(folder), "policy.toml", own=(str(host_dir),)) is None
    # Another spelling of the folder is the same folder.
    (above.parent / "alias").symlink_to(config)
    assert contain(str(above.parent / "alias"), "policy.toml", own=(str(host_dir),)) is None


def test_files_sha256_never_hashes_the_hosts_own_files(
    tmp_path: Path, state_dir: Path, make_context: MakeContext
) -> None:
    """A root holding the state directory (as a root holding ~/.local or ~/.config would) cannot
    reach the audit log or the digest key through it."""
    ctx = make_context(enabled=["files.sha256"], roots={"all": tmp_path})
    for name in ("audit.log", "policy-digest.key"):
        job = Job(job_id="7" * 8, op="files.sha256", args={"root": "all", "path": f"state/{name}"})
        assert check(job, ctx) == "path_outside_root"


def test_a_hard_link_to_one_of_the_hosts_files_is_refused(own: tuple[Path, Path, Path]) -> None:
    above, host_dir, config = own
    os.link(config / "policy.toml", above / "copy-of-policy")
    assert contain(str(above), "copy-of-policy", own=(str(host_dir),)) is None
    assert contain(str(above), "notes.txt", own=(str(host_dir),)) is not None


def test_no_path_reaches_the_folders_the_host_really_uses(
    home: Path, make_context: MakeContext
) -> None:
    """The host's own folders as paths.Dirs names them, under a root that holds ~/.config (or
    ~/Library/Application Support on macOS)."""
    assert main(["ops", "enable", "files.sha256"]) == 0
    assert main(["pause"]) == 0 and main(["resume"]) == 0
    dirs = paths.dirs()
    above = dirs.own[0].parent
    ctx = dataclasses.replace(
        make_context(enabled=["files.sha256"], roots={"cfg": above}),
        own_dirs=tuple(str(folder) for folder in dirs.own),
    )
    names = [p.relative_to(above) for p in dirs.own[0].rglob("*") if p.is_file()]
    assert any(name.name == "policy.toml" for name in names)
    for name in names:
        job = Job(job_id="6" * 8, op="files.sha256", args={"root": "cfg", "path": str(name)})
        assert check(job, ctx) == "path_outside_root", name


@pytest.fixture
def piped() -> object:
    """A process whose standard input is a pipe holding data it has not read yet."""
    reader = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; time.sleep(30)"], stdin=subprocess.PIPE
    )
    assert reader.stdin is not None
    reader.stdin.write(b"another program's data\n")
    reader.stdin.flush()
    try:
        yield reader
    finally:
        reader.kill()
        reader.wait()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="no /proc")
def test_no_path_goes_through_proc(piped: subprocess.Popen[bytes], tmp_path: Path) -> None:
    """Under /proc, <pid>/fd/0 takes the kernel to a pipe whose link text is 'pipe:[N]', and
    'self', '<pid>/cwd' and a process not yet started mean the host when the host looks and the
    child when it opens the path. No lookup the host makes can stand in for the child's, so no path
    goes through /proc, and a folder on /proc holds nothing."""
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    here = os.getcwd()
    os.chdir(tmp_path)
    try:
        for path in (
            f"proc/{piped.pid}/fd/0",
            f"proc/self/cwd{secret}",
            f"proc/self/cwd/{secret.name}",
            f"proc/{piped.pid}/root{secret}",
            f"proc/{2**22 - 1}/root{secret}",
            "proc/self/status",
            "dev/stdin",
            "proc",
        ):
            assert contain("/", path) is None, path
        for root in ("/proc", f"/proc/{piped.pid}"):
            assert contain(root, "status") is None, root
        # The same file by its own path is inside "/".
        assert contain("/", str(secret).lstrip("/")) == "/"
    finally:
        os.chdir(here)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="no /proc")
def test_a_folder_on_proc_cannot_be_added(home: Path) -> None:
    assert main(["roots", "add", "p", "/proc"]) == 1
    assert main(["roots", "add", "p", "/proc/self"]) == 1


def test_a_file_named_with_a_trailing_slash_names_nothing(tree: tuple[Path, Path]) -> None:
    """The child's lookup of 'file.txt/' fails with ENOTDIR, as below any file: inside, nothing.
    So does a link whose text names a file with a trailing slash."""
    root, _ = tree
    real = os.path.realpath(root)
    (root / "slash").symlink_to("file.txt/")
    (root / "slash-dot").symlink_to("file.txt/.")
    (root / "slash-abs").symlink_to(f"{real}/file.txt/")
    (root / "chain").symlink_to("slash/")
    (root / "dir-slash").symlink_to("a/")
    for path in ("file.txt/", "file.txt/.", "a/", "a/.", ".", "slash", "slash-dot", "slash-abs"):
        assert contain(str(root), path) == real, path
    for path in ("chain", "dir-slash", "dir-slash/b/f.txt", "slash/"):
        assert contain(str(root), path) == real, path


def test_the_hosts_files_behind_a_link_are_still_its_own(home: Path, tmp_path: Path) -> None:
    """A profile folder moved to another disk and linked back: the files the host really uses are
    where the link points, and they are still its own."""
    moved = tmp_path / "data" / "lh-state"
    moved.mkdir(parents=True)
    state = paths.dirs().state
    paths.ensure_dir(state.parent)
    state.symlink_to(moved)
    assert main(["pause"]) == 0
    assert (moved / "paused").exists()
    own = tuple(str(folder) for folder in paths.dirs().own)
    assert contain(str(tmp_path / "data"), "lh-state/paused", own=own) is None
    assert main(["roots", "add", "moved", str(moved)]) == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's mount table")
def test_without_a_mount_table_that_lists_proc_nothing_is_walked(
    tree: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/proc covered by another mount, or not mounted: the table cannot show which folders are
    /proc, so no path can be shown to stay off it."""
    root, _ = tree
    assert contain(str(root), "file.txt") is not None
    monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(tmp_path / "no-such-table"))
    assert contain(str(root), "file.txt") is None
    assert containment.on_proc(str(root))


def test_a_mount_point_with_a_carriage_return_is_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel escapes a newline in a mount point but not a carriage return."""
    table = tmp_path / "mountinfo"
    table.write_bytes(b"77 50 0:41 / /mnt/pr\rx rw,relatime - proc proc rw\n")
    monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(table))
    assert [(m.point, m.fstype) for m in mounts.table()] == [("/mnt/pr\rx", "proc")]
