"""``lucille-host uninstall-data --yes`` (decisions/0002 section 17, spec/CLI.md section 7): it
deletes the profile's keyring items, then its folders, and only that profile's; it refuses while
``run`` or ``pair`` of the profile runs, and while the profile holds a pair or work rooms over one,
deleting nothing then; and it can be run again on a profile with nothing left."""

from __future__ import annotations

import contextlib
import io
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from keyring.backends.fail import Keyring as FailKeyring
from keyring.errors import PasswordDeleteError

from lucille_host import cli, credentials, loop, paths, rooms, store
from lucille_host import state as host_state
from tests._host import HostEnv, lock_held
from tests._keyring import MemoryKeyring

SECRET = "correct-horse-battery-staple"


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch, stdin: str = "") -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv)


def _objects(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines()]


def _fill(profile: str, monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """A profile with a credential, a folder, a pause and a pair item in the keyring."""
    folder.mkdir(exist_ok=True)
    argv = ["--profile", profile, "credential", "set", "gmail", "default"]
    assert _main([*argv, "--host", "imap.gmail.com", "--value-stdin"], monkeypatch, SECRET) == 0
    assert _main(["--profile", profile, "roots", "add", "docs", str(folder)], monkeypatch) == 0
    assert _main(["--profile", profile, "pause"], monkeypatch) == 0
    dirs = paths.dirs(profile)
    store.Item(dirs.state, profile).change(lambda doc: doc["shared"].update({"x": 1}))


def test_it_deletes_the_profiles_items_and_folders_and_no_other_profiles(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("mac-app", monkeypatch, tmp_path / "docs")
    _fill("default", monkeypatch, tmp_path / "docs")
    mine = paths.dirs("mac-app")
    other = paths.dirs("default")
    for service in store.OWNED_SERVICES:
        assert (service, "mac-app") in memory_keyring.items
    capsys.readouterr()

    assert _main(["--profile", "mac-app", "--json", "uninstall-data", "--yes"], monkeypatch) == 0
    said = capsys.readouterr()
    assert _objects(said.out) == [{"v": 1, "type": "uninstalled", "profile": "mac-app"}]
    assert said.err == ""
    assert not mine.config.exists() and not mine.state.exists()
    assert sorted(memory_keyring.items) == sorted((s, "default") for s in store.OWNED_SERVICES)
    # The other profile is as it was.
    assert other.policy_file.exists() and other.state.exists()
    assert credentials.CredentialStore(other.state, "default").value("gmail", "default") == SECRET

    # Again, on a profile with nothing left: done, and it says so again.
    assert _main(["--profile", "mac-app", "uninstall-data", "--yes"], monkeypatch) == 0
    said = capsys.readouterr()
    assert said.out == (
        "uninstalled: profile mac-app's keyring items and folders are removed from this computer\n"
    )
    assert not mine.config.exists() and not mine.state.exists()


def test_the_store_names_every_keyring_service_the_host_keeps_an_item_under() -> None:
    """What uninstall-data deletes is every item the host's stores keep: the pair's and the
    credentials'."""
    assert store.OWNED_SERVICES == (store.SERVICE, credentials.SERVICE)


def test_yes_is_required(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.ensure_dir(host_env.dirs.state)
    with pytest.raises(SystemExit) as info:
        _main(["--json", "uninstall-data"], monkeypatch)
    assert info.value.code == 2
    said = capsys.readouterr()
    assert [o["code"] for o in _objects(said.out)] == ["usage"]
    assert "--yes" in said.err
    assert host_env.dirs.state.exists()


@pytest.mark.parametrize(
    ("lock", "said"),
    [(loop.RUN_LOCK, cli.UNINSTALL_RUNNING), (cli.PAIR_LOCK, cli.UNINSTALL_PAIRING)],
)
def test_it_refuses_while_run_or_pair_holds_its_lock(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    lock: str,
    said: str,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    before = dict(memory_keyring.items)
    capsys.readouterr()
    with lock_held(host_env.dirs.state / lock):
        assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    out = capsys.readouterr()
    assert _objects(out.out) == [{"v": 1, "type": "error", "code": "busy", "message": said}]
    assert memory_keyring.items == before
    assert host_env.dirs.policy_file.exists()


def test_it_refuses_while_a_pair_is_held(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    monkeypatch.setattr("lucille_host.link.Session.held", lambda _self: ["pair-abcdef"])
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    out = capsys.readouterr()
    message = cli.UNINSTALL_HELD.format(name="pair-abcdef")
    assert _objects(out.out) == [{"v": 1, "type": "error", "code": "held", "message": message}]
    assert memory_keyring.items == before
    assert host_env.dirs.policy_file.exists()


def test_it_refuses_while_work_rooms_over_a_pair_gone_are_held(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    left_over = rooms.Record(pair="pair-abcdef", held={"room-1": object()})  # type: ignore[dict-item]
    monkeypatch.setattr("lucille_host.rooms.load", lambda _path: left_over)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    out = capsys.readouterr()
    message = cli.UNINSTALL_ROOMS.format(name="pair-abcdef")
    assert _objects(out.out) == [{"v": 1, "type": "error", "code": "held", "message": message}]
    assert host_env.dirs.policy_file.exists()


def test_an_item_that_cannot_be_deleted_stops_it_before_any_folder(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """keyring says an item it could not delete is not there, as its Keychain backend does for
    every error: the read after it finds the item, and nothing more is deleted."""
    _fill("default", monkeypatch, tmp_path / "docs")

    def refused(_service: str, _username: str) -> None:
        raise PasswordDeleteError("Can't delete password in keychain")

    monkeypatch.setattr(memory_keyring, "delete_password", refused)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    out = capsys.readouterr()
    found = _objects(out.out)
    assert [o["code"] for o in found] == ["store_error"]
    assert found[0]["message"] == "the keyring item lucille-host/default could not be deleted"
    assert SECRET not in out.out + out.err
    assert host_env.dirs.policy_file.exists() and host_env.dirs.state.exists()


def test_a_keyring_error_names_its_class_only(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")

    def failing(_service: str, _username: str) -> None:
        raise OSError(f"the item holds {SECRET}")

    monkeypatch.setattr(memory_keyring, "delete_password", failing)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    out = capsys.readouterr()
    assert _objects(out.out) == [
        {
            "v": 1,
            "type": "error",
            "code": "store_error",
            "message": "the keyring could not be deleted (OSError)",
        }
    ]
    assert SECRET not in out.out + out.err
    assert host_env.dirs.policy_file.exists()


def test_it_refuses_where_the_host_does_not_start(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    before = dict(memory_keyring.items)
    monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example")
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    assert [o["code"] for o in _objects(capsys.readouterr().out)] == ["startup_refused"]
    assert memory_keyring.items == before
    assert host_env.dirs.policy_file.exists()


def _mounted_at(
    monkeypatch: pytest.MonkeyPatch, name: str, when: Callable[[], bool] = lambda: True
) -> None:
    """Make the entry ``name`` of any folder read as on another device than its folder, as a mount
    point is, while ``when()`` holds: a mount with no root and no disk image. Only a read of an
    entry within an open folder (``dir_fd``) is changed, which is how the verb reads each one."""
    real = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        found = real(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and os.fsdecode(path) == name and when():
            values = list(found)
            values[2] = found.st_dev + 1  # st_dev
            return os.stat_result(values)
        return found

    monkeypatch.setattr("lucille_host.cli.os.stat", stat)


@pytest.mark.parametrize(("where", "inside"), [("state", ("mnt",)), ("config", ("sub", "mnt"))])
def test_a_mount_inside_a_folder_is_refused_before_anything_is_deleted(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    where: str,
    inside: tuple[str, ...],
) -> None:
    """A folder mounted inside the profile's is outside it: the verb reads both folders through
    first, and refuses ``other_device`` with the keyring items and every file where they were."""
    _fill("default", monkeypatch, tmp_path / "docs")
    mount = getattr(host_env.dirs, where).joinpath(*inside)
    mount.mkdir(parents=True)
    (mount / "sentinel").write_text("outside the profile")
    before = dict(memory_keyring.items)
    _mounted_at(monkeypatch, inside[-1])
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    found = _objects(capsys.readouterr().out)
    assert [o["code"] for o in found] == ["other_device"]
    message = cli.UNINSTALL_OTHER_DEVICE.format(path=mount, folder=getattr(host_env.dirs, where))
    assert found[0]["message"] == message
    assert memory_keyring.items == before
    assert (mount / "sentinel").read_text() == "outside the profile"
    assert host_env.dirs.policy_file.exists()
    assert (host_env.dirs.state / loop.RUN_LOCK).exists()


def test_a_mount_made_after_the_check_stops_the_deletion_there(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A mount made between the check and the deletion: the deletion crosses no device either, so
    nothing on it is deleted, and the message says what may be gone."""
    _fill("default", monkeypatch, tmp_path / "docs")
    mount = host_env.dirs.state / "mnt"
    mount.mkdir()
    (mount / "sentinel").write_text("outside the profile")
    deleted: list[str] = []
    real_delete = store.delete_items

    def delete_items(username: str) -> None:
        real_delete(username)
        deleted.append(username)

    monkeypatch.setattr(store, "delete_items", delete_items)
    _mounted_at(monkeypatch, "mnt", when=lambda: bool(deleted))
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    found = _objects(capsys.readouterr().out)
    message = cli.UNINSTALL_OTHER_DEVICE_LATE.format(path=mount, folder=host_env.dirs.state)
    assert found == [{"v": 1, "type": "error", "code": "other_device", "message": message}]
    assert deleted == ["default"]
    assert (mount / "sentinel").read_text() == "outside the profile"


@pytest.mark.parametrize("where", ["state", "config"])
def test_a_folder_that_is_a_link_is_refused_before_anything_is_deleted(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    where: str,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    folder = getattr(host_env.dirs, where)
    target = tmp_path / "elsewhere"
    folder.rename(target)
    folder.symlink_to(target)
    (target / "sentinel").write_text("outside the profile")
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    found = _objects(capsys.readouterr().out)
    message = cli.UNINSTALL_NOT_A_FOLDER.format(folder=folder)
    assert found == [{"v": 1, "type": "error", "code": "state_invalid", "message": message}]
    assert memory_keyring.items == before
    assert (target / "sentinel").read_text() == "outside the profile"
    assert folder.is_symlink()


def test_links_inside_are_deleted_and_what_they_name_is_not(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "sentinel").write_text("kept")
    (host_env.dirs.state / "to-folder").symlink_to(outside)
    (host_env.dirs.config / "to-file").symlink_to(outside / "sub" / "sentinel")
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 0
    assert not host_env.dirs.state.exists() and not host_env.dirs.config.exists()
    assert (outside / "sub" / "sentinel").read_text() == "kept"


def test_lock_files_go_last(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A command of the profile started while the folders are deleted finds the lock still there,
    and held, until nothing else of its folder is left."""
    _fill("default", monkeypatch, tmp_path / "docs")
    (host_env.dirs.state / "zzz").write_text("after the locks by name")
    unlinked: list[str] = []
    real = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        unlinked.append(os.fsdecode(path))
        real(path, *args, **kwargs)

    monkeypatch.setattr("lucille_host.cli.os.unlink", unlink)
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 0
    locks = {loop.RUN_LOCK, cli.PAIR_LOCK, store.CREDENTIALS_LOCK}
    assert locks <= set(unlinked)
    # "zzz" comes after every lock file by name, and is deleted before any of them.
    assert unlinked.index("zzz") < min(unlinked.index(name) for name in locks)


def test_it_holds_the_credential_stores_lock_while_it_deletes(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No ``credential`` verb changes the credentials' item while it is deleted: they take the
    same lock."""
    _fill("default", monkeypatch, tmp_path / "docs")
    held: list[Path] = []
    real_lock = store.file_lock

    @contextlib.contextmanager
    def file_lock(path: Path) -> Iterator[None]:
        with real_lock(path):
            held.append(path)
            try:
                yield
            finally:
                held.remove(path)

    seen: list[list[Path]] = []
    real_delete = store.delete_items
    monkeypatch.setattr(store, "file_lock", file_lock)
    monkeypatch.setattr(
        store, "delete_items", lambda username: (seen.append(list(held)), real_delete(username))
    )
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 0
    assert seen == [[host_env.dirs.state / credentials.LOCK]]
    assert credentials.LOCK == store.CREDENTIALS_LOCK


def test_a_waiting_connect_request_goes_with_the_state_folder(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``pair cancel`` only deletes the request this computer keeps, so the folder's deletion
    drops it as that does."""
    _fill("default", monkeypatch, tmp_path / "docs")
    host_env.dirs.connect_file.write_text("{}")
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 0
    assert [o["type"] for o in _objects(capsys.readouterr().out)] == ["uninstalled"]
    assert not host_env.dirs.connect_file.exists()


def test_it_refuses_while_a_room_is_being_accepted_over_a_pair_gone(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    accepting = object()
    left_over = rooms.Record(pair="pair-abcdef", accepting=accepting)  # type: ignore[arg-type]
    monkeypatch.setattr("lucille_host.rooms.load", lambda _path: left_over)
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    message = cli.UNINSTALL_ROOMS.format(name="pair-abcdef")
    assert _objects(capsys.readouterr().out) == [
        {"v": 1, "type": "error", "code": "held", "message": message}
    ]
    assert memory_keyring.items == before
    assert host_env.dirs.policy_file.exists()


def test_a_keyring_it_cannot_use_is_refused_in_words_of_its_own(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A profile that keeps its pair in a file (``pair --store-file``), where no keyring can be
    used: the refusal does not point to ``--store-file``, which deletes nothing; and nothing is
    deleted."""
    _fill("default", monkeypatch, tmp_path / "docs")
    host_state.save_host_config(host_env.dirs, host_state.HostConfig(store="file"))
    monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    found = _objects(capsys.readouterr().out)
    assert [o["code"] for o in found] == ["insecure_keyring"]
    assert found[0]["message"].endswith(store.UNINSTALL_REFUSAL)
    assert "--store-file" not in found[0]["message"]
    assert host_env.dirs.policy_file.exists() and host_env.dirs.state.exists()


def _bind_mount_table(tmp_path: Path, points: list[Path], device: int) -> Path:
    """A table of mounts (``/proc/self/mountinfo``'s form) that lists a bind mount of another
    folder of the same filesystem at each of ``points``: the same device as the folder around it,
    so that only the table tells it from the profile's own."""
    lines = [f"22 1 {os.major(device)}:{os.minor(device)} / / rw - ext4 /dev/sda1 rw"]
    for number, point in enumerate(points, start=40):
        escaped = os.path.realpath(point).replace("\\", "\\134").replace(" ", "\\040")
        lines.append(
            f"{number} 22 {os.major(device)}:{os.minor(device)} /outside/dir {escaped} "
            "rw,relatime shared:1 - ext4 /dev/sda1 rw"
        )
    tmp_path.mkdir(exist_ok=True)
    table = tmp_path / "mountinfo"
    table.write_text("\n".join(lines) + "\n")
    return table


@pytest.mark.parametrize(
    ("where", "inside"), [("state", ("bm",)), ("config", ("a", "b", "bm")), ("state", ())]
)
def test_a_bind_mount_of_the_same_filesystem_is_refused_before_anything_is_deleted(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    where: str,
    inside: tuple[str, ...],
) -> None:
    """A bind mount shows another folder of the same filesystem: its device is the folder's own,
    so the device check cannot see it, and the table of mounts does. A mount point at a folder
    itself is refused too."""
    _fill("default", monkeypatch, tmp_path / "docs")
    folder = getattr(host_env.dirs, where)
    point = folder.joinpath(*inside)
    point.mkdir(parents=True, exist_ok=True)
    (point / "sentinel").write_text("outside the profile")
    table = _bind_mount_table(tmp_path, [point], os.stat(folder).st_dev)
    monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(table))
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    message = cli.UNINSTALL_MOUNTED.format(point=os.path.realpath(point), folder=folder)
    assert _objects(capsys.readouterr().out) == [
        {"v": 1, "type": "error", "code": "other_device", "message": message}
    ]
    assert memory_keyring.items == before
    assert (point / "sentinel").read_text() == "outside the profile"
    assert host_env.dirs.policy_file.exists()


def test_a_bind_mount_made_after_the_check_is_refused_before_any_folder(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    point = host_env.dirs.state / "bm"
    point.mkdir()
    (point / "sentinel").write_text("outside the profile")
    table = _bind_mount_table(tmp_path, [point], os.stat(host_env.dirs.state).st_dev)
    empty = _bind_mount_table(tmp_path / "empty", [], os.stat(host_env.dirs.state).st_dev)
    monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(empty))
    real_delete = store.delete_items

    def delete_items(username: str) -> None:
        real_delete(username)
        monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(table))

    monkeypatch.setattr(store, "delete_items", delete_items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    message = cli.UNINSTALL_MOUNTED_LATE.format(
        point=os.path.realpath(point), folder=host_env.dirs.state
    )
    assert _objects(capsys.readouterr().out) == [
        {"v": 1, "type": "error", "code": "other_device", "message": message}
    ]
    assert (point / "sentinel").read_text() == "outside the profile"
    assert host_env.dirs.policy_file.exists()


def test_where_the_table_must_be_read_and_cannot_be_nothing_is_removed(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """On Linux, a table that cannot be read hides any bind mount: refused, not skipped."""
    _fill("default", monkeypatch, tmp_path / "docs")
    monkeypatch.setattr("lucille_host.mounts.MOUNTINFO", str(tmp_path / "no-such-table"))
    monkeypatch.setattr(cli, "MOUNT_TABLE_SYSTEMS", frozenset({paths.os_family()}))
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    assert _objects(capsys.readouterr().out) == [
        {"v": 1, "type": "error", "code": "other_device", "message": cli.UNINSTALL_NO_TABLE}
    ]
    assert memory_keyring.items == before
    assert host_env.dirs.policy_file.exists()


def test_the_table_is_read_on_linux_only_where_none_can_be_read() -> None:
    assert frozenset({"linux"}) == cli.MOUNT_TABLE_SYSTEMS


def test_a_folder_that_opens_on_another_device_is_refused(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A folder swapped for a mount between its ``stat`` and its ``open``: the descriptor's own
    device is checked too."""
    _fill("default", monkeypatch, tmp_path / "docs")
    inner = host_env.dirs.state / "inner"
    inner.mkdir()
    swapped = os.stat(inner).st_ino
    real = os.fstat

    def fstat(descriptor: int) -> os.stat_result:
        found = real(descriptor)
        if found.st_ino == swapped:
            values = list(found)
            values[2] = found.st_dev + 1  # st_dev
            return os.stat_result(values)
        return found

    monkeypatch.setattr("lucille_host.cli.os.fstat", fstat)
    before = dict(memory_keyring.items)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    found = _objects(capsys.readouterr().out)
    assert [o["code"] for o in found] == ["other_device"]
    assert found[0]["message"] == cli.UNINSTALL_OTHER_DEVICE.format(
        path=inner, folder=host_env.dirs.state
    )
    assert memory_keyring.items == before


def test_a_failure_after_the_keyring_is_cleared_says_what_may_be_gone(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _fill("default", monkeypatch, tmp_path / "docs")
    stuck = host_env.dirs.state / "deep"
    stuck.mkdir()
    (stuck / "f").write_text("x")
    real = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.fsdecode(path) == "f":
            raise PermissionError(1, "Operation not permitted")
        real(path, *args, **kwargs)

    monkeypatch.setattr("lucille_host.cli.os.unlink", unlink)
    capsys.readouterr()
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 1
    message = cli.UNINSTALL_FAILED_LATE.format(reason="Operation not permitted", path=stuck / "f")
    assert _objects(capsys.readouterr().out) == [
        {"v": 1, "type": "error", "code": "os_error", "message": message}
    ]


def test_every_lock_file_of_both_folders_goes_after_everything_else(
    host_env: HostEnv,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The state folder, with ``run``'s lock, is not deleted while the config folder still holds
    anything but its own lock: a command started then finds every lock still there."""
    _fill("default", monkeypatch, tmp_path / "docs")
    unlinked: list[str] = []
    real = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        unlinked.append(os.fsdecode(path))
        real(path, *args, **kwargs)

    monkeypatch.setattr("lucille_host.cli.os.unlink", unlink)
    assert _main(["--json", "uninstall-data", "--yes"], monkeypatch) == 0
    locks = [i for i, name in enumerate(unlinked) if name.endswith(".lock")]
    others = [i for i, name in enumerate(unlinked) if not name.endswith(".lock")]
    assert host_env.dirs.policy_file.name in unlinked
    assert locks and others and max(others) < min(locks)
