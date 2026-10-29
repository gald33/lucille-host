"""The local policy file, its digest, and the CLI verbs that change it."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import MappingProxyType

import pytest

from lucille_host import mounts, paths, policy
from lucille_host.cli import main
from lucille_host.ops import OPERATIONS
from tests._host import lock_held


def _policy_file() -> Path:
    return paths.dirs().policy_file


def test_fresh_policy(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not _policy_file().exists()
    loaded = policy.load(_policy_file())
    assert dict(loaded.enabled) == {"host.describe": OPERATIONS["host.describe"].definition_hash}
    assert dict(loaded.roots) == {}
    assert policy.effective(loaded, paths.os_family()).enabled_ops == ("host.describe",)
    assert main(["ops", "list"]) == 0
    listed = dict(line.split("  ", 1) for line in capsys.readouterr().out.splitlines())
    assert listed["host.describe"] == "enabled"
    assert listed["host.sleep"].startswith("disabled")
    assert listed["files.sha256"].startswith("disabled")


def test_enable_disable_and_roots_round_trip(home: Path, tmp_path: Path) -> None:
    docs = tmp_path / 'my "docs" \\ café \U0001f600'
    docs.mkdir()
    assert main(["ops", "enable", "files.sha256"]) == 0
    assert main(["roots", "add", "docs", str(docs)]) == 0
    loaded = policy.load(_policy_file())
    assert loaded.enabled["files.sha256"] == OPERATIONS["files.sha256"].definition_hash
    assert loaded.roots["docs"] == policy.Root(path=str(docs), broad=False)
    assert _policy_file().read_text(encoding="utf-8").isascii()
    assert stat.S_IMODE(_policy_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(_policy_file().parent.stat().st_mode) == 0o700
    assert main(["ops", "disable", "files.sha256"]) == 0
    assert main(["roots", "remove", "docs"]) == 0
    loaded = policy.load(_policy_file())
    assert "files.sha256" not in loaded.enabled and "docs" not in loaded.roots


def test_cli_refusals(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ops", "enable", "host.shell"]) == 1
    assert main(["ops", "disable", "host.sleep"]) == 1
    assert main(["roots", "add", "Bad Alias", str(tmp_path)]) == 1
    assert main(["roots", "add", "docs", str(tmp_path / "missing")]) == 1
    assert main(["roots", "remove", "docs"]) == 1
    assert main(["--profile", "../x", "ops", "list"]) == 1
    err = capsys.readouterr().err
    assert err.count("lucille-host: ") == 6


def test_home_or_root_needs_the_flag(home: Path) -> None:
    assert main(["roots", "add", "home", str(home)]) == 1
    assert main(["roots", "add", "everything", "/"]) == 1
    assert "home" not in policy.load(_policy_file()).roots
    assert main(["roots", "add", "home", str(home), "--allow-home-or-root"]) == 0
    assert policy.load(_policy_file()).roots["home"].broad
    # A hand-written entry for $HOME without the flag makes the file invalid.
    text = _policy_file().read_text().replace("broad = true", "broad = false")
    _policy_file().write_text(text)
    with pytest.raises(policy.PolicyError):
        policy.load(_policy_file())


@pytest.mark.parametrize(
    "text",
    [
        "version = 1\nunknown = 1\n",
        "version = 2\n",
        "log_results = false\n",
        "version = 1\nlog_results = 1\n",
        'version = 1\n[ops."files.sha256"]\ndefinition = "abc"\n',
        'version = 1\n[ops."files.sha256"]\ndefinition = "' + "a" * 64 + '"\nextra = 1\n',
        'version = 1\n[ops."not an op"]\ndefinition = "' + "a" * 64 + '"\n',
        'version = 1\n[roots."docs"]\npath = "relative/path"\n',
        'version = 1\n[roots."Docs"]\npath = "/tmp"\n',
        'version = 1\n[roots."docs"]\npath = "/tmp"\nbroad = "no"\n',
        # The same table without "extra" loads (test_a_valid_root_table_loads), so this tests it.
        'version = 1\n[roots."docs"]\npath = "/no/such/folder"\nbroad = false\nextra = 1\n',
        'version = 1\n[roots."docs"]\n',
        "version = 1\n[[ops]]\n",
        "this is not toml",
    ],
)
def test_invalid_policy_fails_closed(home: Path, text: str) -> None:
    path = _policy_file()
    paths.ensure_dir(path.parent)
    path.write_text(text)
    with pytest.raises(policy.PolicyError):
        policy.load(path)
    # No verb works on an invalid file, and none rewrites it.
    assert main(["ops", "enable", "host.sleep"]) == 1
    assert main(["ops", "list"]) == 1
    assert path.read_text() == text


def test_a_valid_root_table_loads(home: Path) -> None:
    path = _policy_file()
    paths.ensure_dir(path.parent)
    path.write_text('version = 1\n[roots."docs"]\npath = "/no/such/folder"\nbroad = false\n')
    assert policy.load(path).roots["docs"] == policy.Root(path="/no/such/folder")


def test_upgrade_disables_changed_entry(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The file says files.sha256 was enabled with a definition this release no longer has, as
    # after an upgrade that changed the entry.
    stale = hashlib.sha256(b"the definition an older release had").hexdigest()
    old = policy.Policy(
        enabled=MappingProxyType(
            {"host.describe": OPERATIONS["host.describe"].definition_hash, "files.sha256": stale}
        )
    )
    paths.ensure_dir(_policy_file().parent)
    policy.save(_policy_file(), old)
    eff = policy.effective(policy.load(_policy_file()), paths.os_family())
    assert eff.enabled_ops == ("host.describe",)
    assert eff.changed_by_upgrade == ("files.sha256",)
    assert main(["ops", "list"]) == 0
    assert "files.sha256  disabled: an upgrade changed it" in capsys.readouterr().out
    # The file is left as it was, so the user can see why.
    assert policy.load(_policy_file()).enabled["files.sha256"] == stale
    # Enabling it again accepts the new definition.
    assert main(["ops", "enable", "files.sha256"]) == 0
    eff = policy.effective(policy.load(_policy_file()), paths.os_family())
    assert "files.sha256" in eff.enabled_ops and eff.changed_by_upgrade == ()


def test_operations_this_computer_cannot_run_are_not_enabled(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["ops", "enable", "files.sha256"]) == 0
    monkeypatch.setattr("lucille_host.ops.Operation.available", lambda self, family: False)
    eff = policy.effective(policy.load(_policy_file()), paths.os_family())
    assert "files.sha256" in eff.unavailable and "files.sha256" not in eff.enabled_ops
    assert main(["ops", "enable", "host.sleep"]) == 1


def test_policy_digest_not_plain_hash(state_dir: Path, tmp_path: Path) -> None:
    eff = policy.effective(
        policy.Policy(
            enabled=MappingProxyType(
                {"host.describe": OPERATIONS["host.describe"].definition_hash}
            ),
            roots=MappingProxyType({"docs": policy.Root(path="/home/someone/Documents")}),
        ),
        "linux",
    )
    key = policy.digest_key(state_dir)
    assert len(key) == 32
    assert stat.S_IMODE((state_dir / policy.KEY_FILE).stat().st_mode) == 0o600
    assert policy.digest_key(state_dir) == key
    digest = policy.digest(eff, key)
    assert len(digest) == 64 and int(digest, 16) >= 0
    # Not a plain hash of what it covers: anyone holding one could confirm a guessed path.
    assert digest != hashlib.sha256(policy.canonical(eff)).hexdigest()
    other_dir = paths.ensure_dir(tmp_path / "other-state")
    assert policy.digest(eff, policy.digest_key(other_dir)) != digest
    # It changes when the policy does.
    changed = policy.effective(
        policy.Policy(
            enabled=MappingProxyType(
                {"host.describe": OPERATIONS["host.describe"].definition_hash}
            ),
            roots=MappingProxyType({"docs": policy.Root(path="/home/someone/Other")}),
        ),
        "linux",
    )
    assert policy.digest(changed, key) != digest


def test_a_damaged_digest_key_is_refused(state_dir: Path) -> None:
    (state_dir / policy.KEY_FILE).write_bytes(b"short")
    with pytest.raises(policy.PolicyError):
        policy.digest_key(state_dir)


def test_profiles_are_separate(home: Path) -> None:
    assert main(["--profile", "second", "ops", "enable", "host.sleep"]) == 0
    assert "host.sleep" not in policy.load(_policy_file()).enabled
    assert "host.sleep" in policy.load(paths.dirs("second").policy_file).enabled


def test_home_or_root_is_recognised_by_any_spelling(home: Path, tmp_path: Path) -> None:
    link = tmp_path / "link-to-home"
    link.symlink_to(home)
    for spelling in (str(home), f"{home}/", f"{home}/.", str(link), "/", "//", "/./"):
        assert policy.is_broad(spelling), spelling
    inner = home / "Documents"
    inner.mkdir()
    assert not policy.is_broad(str(inner))
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()
    assert not policy.is_broad(str(sibling))


def test_a_folder_holding_home_needs_the_flag(home: Path, tmp_path: Path) -> None:
    # tmp_path holds $HOME (tmp_path / "home"), as /home or /Users would.
    assert policy.is_broad(str(tmp_path))
    assert policy.is_broad(str(tmp_path.parent))
    link = tmp_path / "link-to-parent"
    link.symlink_to(tmp_path)
    assert policy.is_broad(str(link))
    assert main(["roots", "add", "everyone", str(tmp_path)]) == 1
    assert main(["roots", "add", "everyone", str(tmp_path), "--allow-home-or-root"]) == 0
    assert policy.load(_policy_file()).roots["everyone"].broad


def test_every_directory_the_host_creates_is_0700(home: Path) -> None:
    assert main(["pause"]) == 0
    assert main(["ops", "enable", "host.sleep"]) == 0
    # Every directory from $HOME down to the config and state directories, in this system's
    # layout: none of them existed before.
    created = set()
    for directory in (paths.dirs().config, paths.dirs().state):
        while directory != home:
            created.add(directory)
            directory = directory.parent
    assert len(created) >= 4
    for directory in sorted(created):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_a_policy_file_that_cannot_be_read_fails_closed_without_a_traceback(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.ensure_dir(_policy_file())  # a directory where the file should be
    assert main(["ops", "list"]) == 1
    assert main(["ops", "enable", "host.sleep"]) == 1
    err = capsys.readouterr().err
    assert err.count("lucille-host: ") == 2 and "Traceback" not in err


def test_the_digest_key_is_made_once_and_reused(state_dir: Path) -> None:
    first = policy.digest_key(state_dir)
    assert policy.digest_key(state_dir) == first
    assert [p.name for p in state_dir.iterdir()] == [policy.KEY_FILE]


def _blind_realpath(path: str, *, strict: bool = False) -> str:
    """realpath as it treats a firmlink or a second mount: it leaves a link where it is."""
    return os.path.abspath(path)


def test_another_spelling_of_home_that_realpath_keeps_is_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another case on a case-insensitive disk, a firmlink or a second mount: a spelling of home
    realpath does not unify, so only device and inode can tell. A link stands in for one."""
    spelling = tmp_path / "second-spelling-of-home"
    spelling.symlink_to(home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with monkeypatch.context() as patched:
        patched.setattr("os.path.realpath", _blind_realpath)
        assert policy.is_broad(str(spelling), home=str(home))
        assert not policy.is_broad(str(elsewhere), home=str(home))


def _second_mount_of_home(tmp_path: Path) -> tuple[Path, str]:
    """Home is data/home/me, seen as home/me through a second mount of data/home at home (a bind
    mount on Linux; macOS's Data volume has this shape too). data holds it, and is no folder above
    home/me by name. A link stands in for the mount: realpath is made blind to it, as to a mount,
    and no mount table lists it."""
    real_home = tmp_path / "data" / "home" / "me"
    (real_home / "docs").mkdir(parents=True)
    (tmp_path / "data" / "other").mkdir()
    (tmp_path / "home").symlink_to(tmp_path / "data" / "home")
    return tmp_path / "data", str(tmp_path / "home" / "me")


def test_a_folder_where_home_is_mounted_again_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, home = _second_mount_of_home(tmp_path)
    with monkeypatch.context() as patched:
        patched.setattr("os.path.realpath", _blind_realpath)
        for folder in (data, data / "home", data / "home" / "me"):
            assert policy.is_broad(str(folder), home=home, look_below=True), folder
        for folder in (data / "other", data / "home" / "me" / "docs"):
            assert not policy.is_broad(str(folder), home=home, look_below=True), folder
        # Only adding a folder looks below it: loading the policy does not.
        assert not policy.is_broad(str(data), home=home)


def test_only_adding_a_folder_looks_below_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled network mount below a folder would hang a load that looked there, and with it
    every verb, `roots remove` included. `roots add` looks, with the user there to interrupt it."""
    (tmp_path / "mounts").mkdir()
    data, other_home = _second_mount_of_home(tmp_path / "mounts")
    # The config directory stays under the fixture's home; $HOME is the mounted one.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("HOME", other_home)
    with monkeypatch.context() as patched:
        patched.setattr("os.path.realpath", _blind_realpath)
        assert main(["roots", "add", "data", str(data)]) == 1
        path = _policy_file()
        paths.ensure_dir(path.parent)
        path.write_text(f'version = 1\n[roots."data"]\npath = "{data}"\nbroad = false\n')
        assert policy.load(path).roots["data"].broad is False
        assert main(["roots", "remove", "data"]) == 0


def test_a_folder_holding_another_mount_of_home_by_the_mount_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/mnt/storage/homes mounted again at /home, so home is /home/me: /mnt/storage holds it under
    another name, and only the mount table says so. A link stands in for the mount, realpath is
    blind to it, and the mount table is written out."""
    real_home = tmp_path / "storage" / "homes" / "me"
    real_home.mkdir(parents=True)
    (tmp_path / "storage" / "other").mkdir()
    (tmp_path / "home").symlink_to(tmp_path / "storage" / "homes")
    home = str(tmp_path / "home" / "me")
    storage = str(tmp_path / "storage").replace(" ", "\\040")
    table = tmp_path / "mountinfo"
    table.write_text(
        "21 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        f"30 21 8:2 / {storage} rw - ext4 /dev/sdb1 rw\n"
        f"31 21 8:2 /homes {tmp_path / 'home'} rw - ext4 /dev/sdb1 rw\n"
    )
    monkeypatch.setattr(mounts, "MOUNTINFO", str(table))
    with monkeypatch.context() as patched:
        patched.setattr("os.path.realpath", _blind_realpath)
        for folder in (tmp_path / "storage", tmp_path / "storage" / "homes"):
            assert policy.is_broad(str(folder), home=home), folder
        assert not policy.is_broad(str(tmp_path / "storage" / "other"), home=home)
        # Without a mount table to read, it is not seen.
        patched.setattr(mounts, "MOUNTINFO", str(tmp_path / "no-such-table"))
        assert not policy.is_broad(str(tmp_path / "storage"), home=home)


def test_mount_table_lines_are_read_as_the_kernel_writes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mounts.unescape(b"/mnt/my\\040disk/a\\134b") == "/mnt/my disk/a\\b"
    table = tmp_path / "mountinfo"
    table.write_bytes(
        b"23 28 0:22 / /proc rw,relatime - proc proc rw\n"
        b"36 35 98:0 /mnt1 /mnt\\0402 rw,noatime master:1 shared:7 - ext3 /dev/root rw\n"
        b"not a line of the table\n"
    )
    monkeypatch.setattr(mounts, "MOUNTINFO", str(table))
    assert mounts.table() == [
        mounts.Mount(device=os.makedev(0, 22), root="/", point="/proc", fstype="proc"),
        mounts.Mount(device=os.makedev(98, 0), root="/mnt1", point="/mnt 2", fstype="ext3"),
    ]


def test_home_spelled_past_path_max_is_still_home(home: Path) -> None:
    padded = str(home) + "/." * 2100
    assert len(os.fsencode(padded)) > 4096
    assert policy.is_broad(padded)


def test_a_link_to_home_inside_a_folder_does_not_make_it_hold_home(
    home: Path, tmp_path: Path
) -> None:
    # Containment refuses a link out of a folder when it is followed, so the folder is not broad.
    project = tmp_path / "project"
    project.mkdir()
    (project / "home").symlink_to(home)
    assert not policy.is_broad(str(project))


def test_the_macos_data_volume_and_what_holds_it_need_the_flag(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS, /System/Volumes/Data holds every home folder through the /Users firmlink. A
    folder stands for it here, holding this test's home under the same names (by a link)."""
    volume = tmp_path / "System" / "Volumes" / "Data"
    mirror = volume / os.path.realpath(home).lstrip("/")
    mirror.parent.mkdir(parents=True)
    mirror.symlink_to(home)
    (volume / "elsewhere").mkdir()
    monkeypatch.setattr(policy, "_DATA_VOLUME", str(volume))
    for folder in (volume, volume.parent, volume.parent.parent):
        assert policy.is_broad(str(folder)), folder
    assert not policy.is_broad(str(volume / "elsewhere"))


def test_the_hosts_own_folders_cannot_be_added(home: Path) -> None:
    dirs = paths.dirs()
    for folder in (*dirs.own, dirs.config, dirs.state):
        paths.ensure_dir(folder)
        assert main(["roots", "add", "own", str(folder)]) == 1, folder
    # A folder holding them can be: no path in it reaches them (tests/test_containment.py).
    assert main(["roots", "add", "above", str(dirs.own[0].parent)]) == 0


def test_a_policy_change_waits_for_the_policy_lock(home: Path) -> None:
    lock = paths.ensure_dir(paths.dirs().config) / ".policy.lock"
    results: list[int] = []
    change = threading.Thread(target=lambda: results.append(main(["ops", "enable", "host.sleep"])))
    with lock_held(lock):
        change.start()
        change.join(0.5)
        assert change.is_alive() and "host.sleep" not in policy.load(_policy_file()).enabled
    change.join(10)
    assert results == [0] and "host.sleep" in policy.load(_policy_file()).enabled


def test_policy_changes_run_at_once_all_land(home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    aliases = [f"a{index}" for index in range(12)]
    runs = [
        subprocess.Popen(
            [sys.executable, "-m", "lucille_host", "roots", "add", alias, str(folder)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for alias in aliases
    ]
    for run in runs:
        assert run.wait(60) == 0
    assert sorted(policy.load(_policy_file()).roots) == sorted(aliases)
