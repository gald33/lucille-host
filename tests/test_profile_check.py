"""``lucille-host profile check`` (decisions/0004-running-as-root.md, 4.3; spec/CLI.md section 17):
the folders and files of a profile that another user owns, found by the user, with the repair.

Only root can make a file root owns, and the suite never runs as root, so another user's folder or
file is planted: :func:`owners._owner` and :func:`owners._own_owner` are made to say uid 0 for the
paths a test names, as root would leave them (each folder root made, from the highest down), and,
for the rest, the uid every test is given (tests/conftest.py) where the suite's own uid owns it.
A folder the user cannot look inside is real: a folder of the user's with no permissions at all,
which refuses ``stat`` below it as a root-owned 0700 folder does. Every case runs in conftest's
private home, with the layouts 4.3 lists: the default one, a custom and a relative
``XDG_*_HOME``, a relative ``HOME``, symbolic links, macOS, and a folder outside the home folder.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from lucille_host import owners, paths
from lucille_host.cli import PROFILE_DENIED, main
from tests._host import HostEnv
from tests.conftest import TEST_UID
from tests.spec import _support

# The uid that owns the files the suite makes.
MINE = os.getuid()


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The real paths that root owns, as far as the check can tell; empty to begin with."""
    found: set[str] = set()

    def planting(real: Callable[[Path], int | None]) -> Callable[[Path], int | None]:
        def owner(path: Path) -> int | None:
            if os.path.realpath(path) in found:
                return 0
            uid = real(path)
            return TEST_UID if uid == MINE else uid

        return owner

    monkeypatch.setattr(owners, "_owner", planting(owners._owner))
    monkeypatch.setattr(owners, "_own_owner", planting(owners._own_owner))
    return found


@pytest.fixture
def home(home: Path) -> Path:
    """conftest's private home, which ``HOME`` names: no test here reaches the real one."""
    assert os.environ["HOME"] == str(home)
    return home


def _plant(planted: set[str], *where: Path) -> None:
    planted.update(os.path.realpath(path) for path in where)


def _plant_down(planted: set[str], top: Path, folder: Path) -> None:
    """``top`` and every folder from it down to ``folder``, as root makes them."""
    _plant(planted, folder, *[above for above in folder.parents if above.is_relative_to(top)])


def _linux(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def _made(environ: dict[str, str], family: str = "linux") -> paths.Dirs:
    found = paths.dirs(family=family, environ=environ)
    paths.ensure_dir(found.config)
    paths.ensure_dir(found.state)
    return found


def _check(environ: dict[str, str], family: str = "linux") -> list[owners.Found]:
    return owners.check(family=family, environ=environ)


def _states(found: list[owners.Found]) -> list[tuple[str, Path]]:
    return [(each.state, each.path) for each in found]


@pytest.fixture
def unreadable_folders() -> Iterator[list[Path]]:
    """Folders a test takes every permission from, given back before ``tmp_path`` is removed."""
    taken: list[Path] = []
    yield taken
    for folder in taken:
        folder.chmod(0o700)


def test_a_profile_the_user_owns_passes(home: Path, planted: set[str]) -> None:
    environ = _linux(home)
    found = _made(environ)
    found.policy_file.write_text("")
    (found.state / "paused").write_text("")
    assert _check(environ) == []


def test_nothing_made_yet_passes(home: Path, planted: set[str]) -> None:
    assert _check(_linux(home)) == []


def test_a_root_owned_file_in_the_users_profile(home: Path, planted: set[str]) -> None:
    environ = _linux(home)
    found = _made(environ)
    found.policy_file.write_text("")
    _plant(planted, found.policy_file)
    policy_file = Path(os.path.realpath(found.policy_file))
    assert _check(environ) == [
        owners.Found(
            "not_yours",
            policy_file,
            "file",
            0,
            f"ls -l '{policy_file}'",
            f"chown {TEST_UID} '{policy_file}'",
        )
    ]


def test_a_root_owned_profile_folder(home: Path, planted: set[str]) -> None:
    environ = _linux(home)
    found = _made(environ)
    (found.state / "paused").write_text("")
    _plant(planted, found.state, found.state / "paused")
    state = Path(os.path.realpath(found.state))
    # The folder, and nothing inside it: `chown -R` of it repairs that too.
    assert _check(environ) == [
        owners.Found(
            "not_yours", state, "folder", 0, f"ls -lR '{state}'", f"chown -R {TEST_UID} '{state}'"
        )
    ]


def test_the_highest_root_owned_ancestor_is_the_one_to_repair(
    home: Path, planted: set[str], unreadable_folders: list[Path]
) -> None:
    # As after a root `pause` in a home with no .local: every folder it made is root's, and the
    # user cannot look below the highest one.
    environ = _linux(home)
    found = _made(environ)
    local = home / ".local"
    _plant(planted, local, local / "state", local / "state" / "lucille-host", found.state)
    local.chmod(0)
    unreadable_folders.append(local)
    result = _check(environ)
    real = Path(os.path.realpath(local))
    assert _states(result) == [("not_yours", real)]
    assert result[0].repair == f"chown -R {TEST_UID} '{real}'"


def test_the_config_and_state_sides_are_repaired_apart(home: Path, planted: set[str]) -> None:
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, home / ".config", found.config)
    _plant_down(planted, home / ".local" / "state", found.state)
    assert _states(_check(environ)) == [
        ("not_yours", Path(os.path.realpath(home / ".config"))),
        ("not_yours", Path(os.path.realpath(home / ".local" / "state"))),
    ]
    assert found.config.is_dir()


def test_the_home_folder_and_above_are_never_reported(home: Path, planted: set[str]) -> None:
    environ = _linux(home)
    _made(environ)
    _plant(planted, home, home.parent)
    assert _check(environ) == []


def test_a_folder_the_user_cannot_look_inside_with_no_other_owner(
    home: Path, planted: set[str], unreadable_folders: list[Path]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    lucille = found.state.parent
    lucille.chmod(0)
    unreadable_folders.append(lucille)
    assert _states(_check(environ)) == [("unreadable", Path(os.path.realpath(found.state)))]


def test_a_folder_inside_the_profile_that_cannot_be_listed(
    home: Path, planted: set[str], unreadable_folders: list[Path]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    pairs = found.store_files
    pairs.mkdir()
    pairs.chmod(0)
    unreadable_folders.append(pairs)
    assert _states(_check(environ)) == [("unreadable", Path(os.path.realpath(pairs)))]


def test_a_custom_xdg_folder_inside_home(home: Path, planted: set[str]) -> None:
    environ = _linux(home, XDG_STATE_HOME=str(home / "data" / "state"))
    found = _made(environ)
    assert found.state == home / "data" / "state" / "lucille-host" / "default"
    _plant_down(planted, home / "data", found.state)
    real = Path(os.path.realpath(home / "data"))
    result = _check(environ)
    assert _states(result) == [("not_yours", real)]
    assert result[0].repair == f"chown -R {TEST_UID} '{real}'"


def test_a_custom_xdg_folder_outside_home_gets_no_repair(
    home: Path, tmp_path: Path, planted: set[str]
) -> None:
    elsewhere = tmp_path / "srv" / "state"
    environ = _linux(home, XDG_STATE_HOME=str(elsewhere))
    found = _made(environ)
    _plant(planted, found.state)
    result = _check(environ)
    assert result == [
        owners.Found("outside_home", Path(os.path.realpath(found.state)), "folder", 0)
    ]


def test_a_relative_xdg_folder_is_ignored_as_every_verb_ignores_it(
    home: Path, planted: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    environ = _linux(home, XDG_CONFIG_HOME="relative/config")
    found = _made(environ)
    assert found.config == home / ".config" / "lucille-host" / "default"
    _plant(planted, found.config)
    assert _states(_check(environ)) == [("not_yours", Path(os.path.realpath(found.config)))]
    assert not (home / "relative").exists()


def test_a_relative_home_resolves_as_the_paths_do(
    tmp_path: Path, planted: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    (tmp_path / "home").mkdir()
    environ = {"HOME": "home"}
    found = _made(environ)
    _plant(planted, found.config)
    result = _check(environ)
    assert _states(result) == [("not_yours", Path(os.path.realpath(found.config)))]


def test_a_symlinked_folder_inside_home_is_repaired_where_it_points(
    home: Path, planted: set[str]
) -> None:
    dotfiles = home / "dotfiles" / "config"
    dotfiles.mkdir(parents=True)
    (home / ".config").symlink_to(dotfiles)
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, dotfiles, Path(os.path.realpath(found.config)))
    real = Path(os.path.realpath(dotfiles))
    result = _check(environ)
    assert _states(result) == [("not_yours", real)]
    assert result[0].repair == f"chown -R {TEST_UID} '{real}'"


def test_a_symlinked_folder_pointing_outside_home(
    home: Path, tmp_path: Path, planted: set[str]
) -> None:
    target = tmp_path / "disk" / "local"
    target.mkdir(parents=True)
    (home / ".local").symlink_to(target)
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, target / "state", Path(os.path.realpath(found.state)))
    assert _states(_check(environ)) == [("outside_home", Path(os.path.realpath(target / "state")))]


def test_a_symlink_inside_the_profile_is_not_followed(
    home: Path, tmp_path: Path, planted: set[str]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "root-file").write_text("")
    _plant(planted, outside / "root-file")
    (found.state / "link").symlink_to(outside)
    assert _check(environ) == []


def test_the_macos_layout(home: Path, planted: set[str]) -> None:
    environ = {"HOME": str(home)}
    found = _made(environ, family="macos")
    assert found.state == found.config / "state"
    found.policy_file.write_text("")
    _plant(planted, found.state)
    real = Path(os.path.realpath(found.state))
    # The state folder is inside the config folder: it is found once, from inside.
    assert _states(_check(environ, family="macos")) == [("not_yours", real)]


def test_a_path_with_a_quote_is_quoted_for_a_shell(home: Path, planted: set[str]) -> None:
    environ = _linux(home, XDG_STATE_HOME=str(home / "it's here"))
    found = _made(environ)
    _plant_down(planted, home / "it's here", found.state)
    real = os.path.realpath(home / "it's here")
    result = _check(environ)
    assert result[0].repair == f"chown -R {TEST_UID} '" + real.replace("'", "'\"'\"'") + "'"
    assert found.state.is_dir()


# --- the verb -------------------------------------------------------------------------------------


def test_the_verb_writes_nothing_but_its_run_record(
    host_env: HostEnv, planted: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    paths.ensure_dir(host_env.dirs.config)
    paths.ensure_dir(host_env.dirs.state)
    before = sorted(p for p in host_env.home.rglob("*") if p.name != "runs.jsonl")
    assert main(["profile", "check"]) == 0
    assert capsys.readouterr().out == "every folder and file of this profile is yours\n"
    assert sorted(p for p in host_env.home.rglob("*") if p.name != "runs.jsonl") == before


def test_the_verb_in_text(
    host_env: HostEnv, planted: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    config = paths.ensure_dir(host_env.dirs.config)
    _plant(planted, config)
    real = os.path.realpath(config)
    assert main(["profile", "check"]) == 0
    said = capsys.readouterr()
    assert said.err == ""
    assert said.out.startswith(
        f"{real}: a folder owned by uid 0, not by you\n"
        f"  look first, as root: ls -lR '{real}'\n"
        f"  then repair, as root: chown -R {TEST_UID} '{real}'\n"
    )


def test_the_verb_in_json_validates(
    host_env: HostEnv,
    planted: set[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unreadable_folders: list[Path],
) -> None:
    # The Linux layout, on any system, so that the state folder can be put outside the home.
    monkeypatch.setattr(paths, "os_family", lambda platform=None: "linux")
    elsewhere = tmp_path / "srv"
    assert not elsewhere.is_relative_to(host_env.home)
    monkeypatch.setenv("XDG_STATE_HOME", str(elsewhere))
    found = paths.dirs()
    config = paths.ensure_dir(found.config)
    (config / "policy.toml").write_text("")
    hidden = config / "keep"
    hidden.mkdir()
    hidden.chmod(0)
    unreadable_folders.append(hidden)
    _plant(planted, config / "policy.toml")
    _plant_down(planted, elsewhere, paths.ensure_dir(found.state))
    assert main(["--json", "profile", "check"]) == 0
    # Each one validated against its schema.
    objects = _support.cli_objects(capsys.readouterr().out)
    assert [(each["state"], each["path"]) for each in objects] == [
        ("unreadable", os.path.realpath(hidden)),
        ("not_yours", os.path.realpath(config / "policy.toml")),
        ("outside_home", os.path.realpath(elsewhere)),
    ]


def test_a_denied_profile_folder_points_to_the_verb(
    host_env: HostEnv, capsys: pytest.CaptureFixture[str], unreadable_folders: list[Path]
) -> None:
    config = paths.ensure_dir(host_env.dirs.config)
    config.parent.chmod(0)
    unreadable_folders.append(config.parent)
    assert main(["roots", "add", "docs", str(host_env.home)]) == 1
    said = capsys.readouterr().err
    assert said.startswith("lucille-host: ") and said.endswith(f"; {PROFILE_DENIED}\n")
    assert main(["--json", "ops", "list"]) == 1
    error = _support.cli_objects(capsys.readouterr().out)[-1]
    assert error["message"].endswith(f"; {PROFILE_DENIED}")


def test_a_denied_path_outside_the_profile_is_said_as_it_was(
    host_env: HostEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    from lucille_host import cli

    exc = PermissionError(13, "Permission denied", "/a/file")
    assert not cli._profile_denied(exc, host_env.dirs)
    inside = PermissionError(13, "Permission denied", str(host_env.dirs.state / "x"))
    assert cli._profile_denied(inside, host_env.dirs)
