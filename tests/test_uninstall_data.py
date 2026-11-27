"""``lucille-host uninstall-data --yes`` (decisions/0002 section 17, spec/CLI.md section 7): it
deletes the profile's keyring items, then its folders, and only that profile's; it refuses while
``run`` or ``pair`` of the profile runs, and while the profile holds a pair or work rooms over one,
deleting nothing then; and it can be run again on a profile with nothing left."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from keyring.errors import PasswordDeleteError

from lucille_host import cli, credentials, loop, paths, rooms, store
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
