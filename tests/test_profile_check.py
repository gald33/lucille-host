"""``lucille-host profile check`` (decisions/0004-running-as-root.md, 4.3; spec/CLI.md section 17):
the folders and files of a profile that another user owns, found by the user, with the repair.

Only root can make a file root owns, and the suite never runs as root, so another user's folder or
file is planted: :func:`owners._uid`, which reads the owner from what ``stat`` or ``lstat`` gave, is
made to say uid 0 for the folders and files a test names, by device and inode, as root would leave
them (each folder root made, from the highest down), and, for the rest, the uid every test is given
(tests/conftest.py) where the suite's own uid owns it. Every ``stat`` and ``lstat`` the check makes
is a real one, so that a symbolic link and what it points to are told apart as they are outside the
suite. A folder the user cannot look inside is real too: a folder with no permissions at all, which
refuses ``stat`` below it as a root-owned 0700 folder does. Every case runs in conftest's private
home, with the layouts 4.3 lists: the default one, a custom and a relative ``XDG_*_HOME``, a
relative ``HOME``, symbolic links, macOS, and a folder outside the home folder.
"""

from __future__ import annotations

import os
import subprocess
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


Planted = set[tuple[int, int]]


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch) -> Planted:
    """The folders and files root owns, as far as the check can tell, by device and inode; empty to
    begin with."""
    found: Planted = set()

    def uid(info: os.stat_result) -> int:
        if (info.st_dev, info.st_ino) in found:
            return 0
        return TEST_UID if info.st_uid == MINE else info.st_uid

    monkeypatch.setattr(owners, "_uid", uid)
    return found


@pytest.fixture
def home(home: Path) -> Path:
    """conftest's private home, which ``HOME`` names: no test here reaches the real one."""
    assert os.environ["HOME"] == str(home)
    return home


def _plant(planted: Planted, *where: Path) -> None:
    """What each of ``where`` is, a symbolic link followed, made root's. Before any folder above it
    loses its permissions."""
    planted.update((info.st_dev, info.st_ino) for info in map(os.stat, where))


def _plant_down(planted: Planted, top: Path, folder: Path) -> None:
    """``top`` and every folder from it down to ``folder``, as root makes them."""
    _plant(planted, folder, *[above for above in folder.parents if above.is_relative_to(top)])


def _in(path: Path | str, command: str) -> str:
    """``command`` on ``path`` as the check prints it: in a subshell, in the folder that holds it
    once that folder is checked to be the one named, with no symbolic link, on its name alone."""
    path = Path(path)
    folder = "'" + str(path.parent).replace("'", "'\"'\"'") + "'"
    name = "'./" + path.name.replace("'", "'\"'\"'") + "'"
    return f'(cd -P {folder} && [ "$(pwd -P)" = {folder} ] && {command} {name})'


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
    """Folders a test takes every permission from, given back before ``tmp_path`` is removed, the
    last taken first."""
    taken: list[Path] = []
    yield taken
    for folder in reversed(taken):
        folder.chmod(0o700)


def test_a_profile_the_user_owns_passes(home: Path, planted: Planted) -> None:
    environ = _linux(home)
    found = _made(environ)
    found.policy_file.write_text("")
    (found.state / "paused").write_text("")
    assert _check(environ) == []


def test_nothing_made_yet_passes(home: Path, planted: Planted) -> None:
    assert _check(_linux(home)) == []


def test_a_root_owned_file_in_the_users_profile(home: Path, planted: Planted) -> None:
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
            _in(policy_file, "ls -l"),
            _in(policy_file, f"chown {TEST_UID}"),
        )
    ]


def test_a_root_owned_profile_folder(home: Path, planted: Planted) -> None:
    environ = _linux(home)
    found = _made(environ)
    (found.state / "paused").write_text("")
    _plant(planted, found.state, found.state / "paused")
    state = Path(os.path.realpath(found.state))
    # The folder, and nothing inside it: `chown -R` of it repairs that too.
    assert _check(environ) == [
        owners.Found(
            "not_yours",
            state,
            "folder",
            0,
            _in(state, "ls -lR"),
            _in(state, f"chown -R {TEST_UID}"),
        )
    ]


def test_the_highest_root_owned_ancestor_is_the_one_to_repair(
    home: Path, planted: Planted, unreadable_folders: list[Path]
) -> None:
    # As after a root `pause` in a home with no .local: every folder it made is root's, mode 0700,
    # so the user's `stat` of each folder below .local is refused, for real. The refusals are not
    # findings, and the walk goes on past them up to .local, which `chown -R` repairs.
    environ = _linux(home)
    found = _made(environ)
    local = home / ".local"
    chain = [found.state, found.state.parent, local / "state", local]
    _plant(planted, *chain)
    real = Path(os.path.realpath(local))
    for folder in chain:
        folder.chmod(0)
        unreadable_folders.append(folder)
    with pytest.raises(PermissionError):
        found.state.stat()
    result = _check(environ)
    assert _states(result) == [("not_yours", real)]
    assert result[0].repair == _in(real, f"chown -R {TEST_UID}")


def test_the_config_and_state_sides_are_repaired_apart(home: Path, planted: Planted) -> None:
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, home / ".config", found.config)
    _plant_down(planted, home / ".local" / "state", found.state)
    assert _states(_check(environ)) == [
        ("not_yours", Path(os.path.realpath(home / ".config"))),
        ("not_yours", Path(os.path.realpath(home / ".local" / "state"))),
    ]
    assert found.config.is_dir()


def test_the_home_folder_and_above_are_never_reported(home: Path, planted: Planted) -> None:
    # Every folder from each profile folder up to the home folder is root's, and so are the home
    # folder and the one above it: the walk reaches the home folder, and stops there.
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, home / ".config", found.config)
    _plant_down(planted, home / ".local", found.state)
    _plant(planted, home, home.parent)
    assert _states(_check(environ)) == [
        ("not_yours", Path(os.path.realpath(home / ".config"))),
        ("not_yours", Path(os.path.realpath(home / ".local"))),
    ]


def test_a_folder_the_user_cannot_look_inside_with_no_other_owner(
    home: Path, planted: Planted, unreadable_folders: list[Path]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    lucille = found.state.parent
    lucille.chmod(0)
    unreadable_folders.append(lucille)
    assert _states(_check(environ)) == [("unreadable", Path(os.path.realpath(found.state)))]


def test_a_folder_inside_the_profile_that_cannot_be_listed(
    home: Path, planted: Planted, unreadable_folders: list[Path]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    pairs = found.store_files
    pairs.mkdir()
    pairs.chmod(0)
    unreadable_folders.append(pairs)
    assert _states(_check(environ)) == [("unreadable", Path(os.path.realpath(pairs)))]


def test_a_custom_xdg_folder_inside_home(home: Path, planted: Planted) -> None:
    environ = _linux(home, XDG_STATE_HOME=str(home / "data" / "state"))
    found = _made(environ)
    assert found.state == home / "data" / "state" / "lucille-host" / "default"
    _plant_down(planted, home / "data", found.state)
    real = Path(os.path.realpath(home / "data"))
    result = _check(environ)
    assert _states(result) == [("not_yours", real)]
    assert result[0].repair == _in(real, f"chown -R {TEST_UID}")


def test_a_custom_xdg_folder_outside_home_gets_no_repair(
    home: Path, tmp_path: Path, planted: Planted
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
    home: Path, planted: Planted, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    environ = _linux(home, XDG_CONFIG_HOME="relative/config")
    found = _made(environ)
    assert found.config == home / ".config" / "lucille-host" / "default"
    _plant(planted, found.config)
    assert _states(_check(environ)) == [("not_yours", Path(os.path.realpath(found.config)))]
    assert not (home / "relative").exists()


def test_a_relative_home_resolves_as_the_paths_do(
    tmp_path: Path, planted: Planted, monkeypatch: pytest.MonkeyPatch
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
    home: Path, planted: Planted
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
    assert result[0].repair == _in(real, f"chown -R {TEST_UID}")


def test_a_symlinked_folder_pointing_outside_home(
    home: Path, tmp_path: Path, planted: Planted
) -> None:
    target = tmp_path / "disk" / "local"
    target.mkdir(parents=True)
    (home / ".local").symlink_to(target)
    environ = _linux(home)
    found = _made(environ)
    _plant_down(planted, target / "state", Path(os.path.realpath(found.state)))
    assert _states(_check(environ)) == [("outside_home", Path(os.path.realpath(target / "state")))]


def test_a_symlink_inside_the_profile_is_not_followed_into(
    home: Path, tmp_path: Path, planted: Planted
) -> None:
    environ = _linux(home)
    found = _made(environ)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "root-file").write_text("")
    _plant(planted, outside / "root-file")
    (found.state / "link").symlink_to(outside)
    (found.state / "nowhere").symlink_to(tmp_path / "missing")
    (found.state / "itself").symlink_to(found.state / "itself")
    assert _check(environ) == []


def test_what_a_symlink_inside_the_profile_points_to_is_looked_at(
    home: Path, tmp_path: Path, planted: Planted
) -> None:
    # The link is the user's; the folder it points to is root's, and is reported by its own path,
    # outside the home folder, with nothing inside it.
    environ = _linux(home)
    found = _made(environ)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "root-file").write_text("")
    _plant(planted, outside, outside / "root-file")
    (found.state / "link").symlink_to(outside)
    assert _check(environ) == [
        owners.Found("outside_home", Path(os.path.realpath(outside)), "folder", 0)
    ]


def test_a_policy_file_linked_to_a_root_owned_file(home: Path, planted: Planted) -> None:
    # The host reads the policy through the link, so the file it points to is the one to repair.
    environ = _linux(home)
    found = _made(environ)
    target = home / "dotfiles" / "policy.toml"
    target.parent.mkdir()
    target.write_text("")
    found.policy_file.symlink_to(target)
    _plant(planted, target)
    real = Path(os.path.realpath(target))
    assert _check(environ) == [
        owners.Found(
            "not_yours", real, "file", 0, _in(real, "ls -l"), _in(real, f"chown {TEST_UID}")
        )
    ]


def test_a_symlink_to_a_file_that_cannot_be_looked_at(
    home: Path, planted: Planted, unreadable_folders: list[Path]
) -> None:
    environ = _linux(home)
    found = _made(environ)
    kept = home / "kept"
    kept.mkdir()
    (kept / "policy.toml").write_text("")
    found.policy_file.symlink_to(kept / "policy.toml")
    kept.chmod(0)
    unreadable_folders.append(kept)
    assert _states(_check(environ)) == [
        ("unreadable", Path(os.path.realpath(kept)) / "policy.toml")
    ]


def test_the_macos_layout(home: Path, planted: Planted) -> None:
    environ = {"HOME": str(home)}
    found = _made(environ, family="macos")
    assert found.state == found.config / "state"
    found.policy_file.write_text("")
    _plant(planted, found.state)
    real = Path(os.path.realpath(found.state))
    # The state folder is inside the config folder: it is found once, from inside.
    assert _states(_check(environ, family="macos")) == [("not_yours", real)]


Shell = Callable[[str], tuple[int, str, list[str]]]


@pytest.fixture
def root_shell(tmp_path: Path) -> Shell:
    """Runs a printed command with ``sh``, as root would paste it, with a ``chown`` that changes
    nothing and writes down the folder it ran in, with no symbolic link, and what it was given."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    log = tmp_path / "chown.log"
    stand_in = bin_folder / "chown"
    stand_in.write_text('#!/bin/sh\nprintf \'%s\\n\' "$(pwd -P)" "$@" >> "$CHOWN_LOG"\n')
    stand_in.chmod(0o755)

    def run(command: str) -> tuple[int, str, list[str]]:
        environ = {
            **os.environ,
            "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}",
            "CHOWN_LOG": str(log),
        }
        done = subprocess.run(
            ["/bin/sh", "-c", command], env=environ, capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout, log.read_text().splitlines() if log.exists() else []

    return run


def _swap_for_a_link(folder: Path, elsewhere: Path, *inside: str) -> None:
    """What the user can do before root runs a command: move ``folder``, which is theirs, aside,
    and put in its place a symbolic link to ``elsewhere``, another account's folder that holds
    ``inside``, the names the check reported, as another account's profile does."""
    folder.rename(folder.with_name(folder.name + ".moved"))
    for name in inside:
        (elsewhere / name).parent.mkdir(parents=True, exist_ok=True)
        (elsewhere / name).write_text("another account's")
    folder.symlink_to(elsewhere)


def test_the_printed_repair_names_the_file_it_found(
    home: Path, planted: Planted, root_shell: Shell
) -> None:
    environ = _linux(home)
    found = _made(environ)
    found.policy_file.write_text("")
    _plant(planted, found.policy_file)
    (finding,) = _check(environ)
    assert finding.repair is not None
    returned, _, chown = root_shell(finding.repair)
    assert returned == 0
    ran_in, *given = chown
    assert ran_in == os.path.realpath(found.config)
    assert given == [str(TEST_UID), "./policy.toml"]
    assert os.stat(Path(ran_in, given[-1])) == os.stat(found.policy_file)


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_a_folder_above_a_finding_swapped_for_a_link_stops_the_printed_commands(
    home: Path, tmp_path: Path, planted: Planted, root_shell: Shell, kind: str
) -> None:
    # The folder that holds a finding is the user's (the walk up stops at the first one that is),
    # so the user can swap it, or one above it, for a link to another account's folder holding a
    # file or folder of the same name. A command that named the whole path would then give root's
    # `chown` that account's file. The printed commands stop instead, and change nothing.
    environ = _linux(home)
    found = _made(environ)
    elsewhere = tmp_path / "another-account"
    if kind == "file":
        found.policy_file.write_text("")
        _plant(planted, found.policy_file)
        swapped, inside = found.config, ["policy.toml"]
    else:
        (found.state / "pairs").mkdir()
        _plant(planted, found.state, found.state / "pairs")
        swapped, inside = found.state.parent, ["default/pairs/record"]
    (finding,) = _check(environ)
    assert finding.look is not None and finding.repair is not None
    _swap_for_a_link(swapped, elsewhere, *inside)
    assert Path(finding.path).exists()  # the same path now names another account's
    for command in (finding.look, finding.repair):
        returned, out, chown = root_shell(command)
        assert returned != 0
        assert out == ""
        assert chown == []


def test_a_path_with_a_quote_is_quoted_for_a_shell(home: Path, planted: Planted) -> None:
    environ = _linux(home, XDG_STATE_HOME=str(home / "it's here"))
    found = _made(environ)
    _plant_down(planted, home / "it's here", found.state)
    folder = os.path.realpath(home)
    result = _check(environ)
    assert result[0].repair == (
        f"(cd -P '{folder}' && [ \"$(pwd -P)\" = '{folder}' ] && "
        f"chown -R {TEST_UID} './it'\"'\"'s here')"
    )
    assert found.state.is_dir()


# --- the verb -------------------------------------------------------------------------------------


def test_the_verb_writes_nothing_but_its_run_record(
    host_env: HostEnv, planted: Planted, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.ensure_dir(host_env.dirs.config)
    paths.ensure_dir(host_env.dirs.state)
    before = sorted(p for p in host_env.home.rglob("*") if p.name != "runs.jsonl")
    assert main(["profile", "check"]) == 0
    assert capsys.readouterr().out == "every folder and file of this profile is yours\n"
    assert sorted(p for p in host_env.home.rglob("*") if p.name != "runs.jsonl") == before


def test_the_verb_in_text(
    host_env: HostEnv, planted: Planted, capsys: pytest.CaptureFixture[str]
) -> None:
    config = paths.ensure_dir(host_env.dirs.config)
    _plant(planted, config)
    real = os.path.realpath(config)
    assert main(["profile", "check"]) == 0
    said = capsys.readouterr()
    assert said.err == ""
    assert said.out.startswith(
        f"{real}: a folder owned by uid 0, not by you\n"
        f"  look first, as root: {_in(real, 'ls -lR')}\n"
        f"  then repair, as root: {_in(real, f'chown -R {TEST_UID}')}\n"
    )


def test_the_verb_in_json_validates(
    host_env: HostEnv,
    planted: Planted,
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

    def denied(path: Path | str) -> bool:
        refused = PermissionError(13, "Permission denied", str(path))
        return cli._profile_denied(refused, host_env.dirs)

    assert not denied("/a/file")
    assert not denied(host_env.home / "Documents")
    assert denied(host_env.dirs.state / "x")
    assert denied(host_env.dirs.config)
    # A folder above one of the profile's folders.
    assert denied(host_env.dirs.state.parent)
    assert denied(host_env.home)
