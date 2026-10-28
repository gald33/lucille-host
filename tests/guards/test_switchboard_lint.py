"""tools/guards/switchboard_lint.py reports each planted violation, honours its exemptions, and
passes the repo."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from switchboard import client, config, crypto, pairing, sharing, signing

from tools.guards import switchboard_lint
from tools.guards._common import import_aliases, package_of, references

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "guards" / "switchboard_lint.py"

RUNTIME = "src/lucille_host/example.py"
REAL_WRAPPER = (REPO_ROOT / "src" / "lucille_host" / "_switchboard.py").read_bytes()
WRAPPER = "src/lucille_host/_switchboard.py"
CONFORMANCE = "src/lucille_host/conformance/fake_lucille.py"
TEST = "tests/test_example.py"
TOOL = "tools/example.py"
EVERY_ZONE = (RUNTIME, WRAPPER, CONFORMANCE, TEST, TOOL)

# Built from two literals, so this file does not itself hold an invite-code prefix.
PREFIX = "swb1" + "_"


def rules(source: str, rel: str, bindings: dict[str, str] | None = None) -> set[str]:
    return {violation.rule for violation in switchboard_lint.check_source(source, rel, bindings)}


@pytest.mark.parametrize("rel", EVERY_ZONE)
def test_a_file_that_does_not_parse_is_reported(rel: str) -> None:
    assert rules("def broken(:\n", rel) == {"parse"}


# --- One import site in the runtime package --------------------------------------------------

SWITCHBOARD_IMPORTS = [
    "import switchboard\n",
    "import switchboard.client\n",
    "import switchboard.pairing as pairing\n",
    "from switchboard import client\n",
    "from switchboard.client import Client\n",
    # Whatever the wrapper imports from switchboard, re-exported, is still switchboard.
    "from lucille_host._switchboard import sharing\n",
    "from lucille_host._switchboard import client\n",
    "from lucille_host._switchboard import pairing\n",
    "from lucille_host._switchboard import switchboard\n",
]


@pytest.mark.parametrize("source", SWITCHBOARD_IMPORTS)
def test_runtime_import_outside_the_wrapper_is_reported(source: str) -> None:
    assert "switchboard-import" in rules(source, RUNTIME)


# The wrapper reached by a relative import, from the package and from a subpackage.
RELATIVE_WRAPPER_IMPORTS = [
    ("src/lucille_host/example.py", "from ._switchboard import client\n"),
    ("src/lucille_host/example.py", "from ._switchboard import pairing, sharing\n"),
    (
        "src/lucille_host/example.py",
        "from . import _switchboard\n_switchboard.client.Client(None)\n",
    ),
    ("src/lucille_host/ops/catalog.py", "from .._switchboard import client\n"),
    ("src/lucille_host/ops/catalog.py", "from .. import _switchboard as w\nw.sharing.touch()\n"),
]


@pytest.mark.parametrize(("rel", "source"), RELATIVE_WRAPPER_IMPORTS)
def test_runtime_relative_import_of_the_wrapper_is_reported(rel: str, source: str) -> None:
    assert "switchboard-import" in rules(source, rel)


SWITCHBOARD_USES_THROUGH_THE_WRAPPER = [
    "from lucille_host import _switchboard\n_switchboard.client.Client(None)\n",
    "from lucille_host import _switchboard as w\nw.pairing.start(None, store=None)\n",
    "import lucille_host._switchboard\nlucille_host._switchboard.sharing.touch(store=s)\n",
    "from . import _switchboard\n_switchboard.pairing.start(None)\n",
]


@pytest.mark.parametrize("source", SWITCHBOARD_USES_THROUGH_THE_WRAPPER)
def test_runtime_use_through_the_wrapper_is_reported(source: str) -> None:
    assert "switchboard-import" in rules(source, RUNTIME)


@pytest.mark.parametrize(
    "rel",
    [
        "src/lucille_host/_exec.py",
        "src/lucille_host/ops/_switchboard.py",
        "src/lucille_host/_switchboard_helpers.py",
    ],
)
def test_the_wrapper_exemption_is_for_that_exact_path(rel: str) -> None:
    assert "switchboard-import" in rules("from switchboard import pairing\n", rel)


def test_names_the_wrapper_binds_are_followed() -> None:
    bindings = switchboard_lint.wrapper_bindings(
        "from switchboard.client import Client as HubClient\nfrom typing import Any\n"
    )
    assert bindings["HubClient"] == "switchboard.client.Client"
    assert bindings["Any"] == "typing.Any"
    source = "from lucille_host._switchboard import HubClient\nHubClient(None)\n"
    assert "switchboard-import" in rules(source, RUNTIME, bindings)
    assert rules("from lucille_host._switchboard import Any\n", RUNTIME, bindings) == set()


def test_the_tree_check_reads_the_wrapper(tmp_path: Path) -> None:
    package = tmp_path / "src" / "lucille_host"
    package.mkdir(parents=True)
    (package / "_switchboard.py").write_text("from switchboard.client import Client as HubClient\n")
    (package / "cli.py").write_text("from lucille_host._switchboard import HubClient\n")
    found = switchboard_lint.check(tmp_path)
    assert [(v.path, v.rule) for v in found] == [("src/lucille_host/cli.py", "switchboard-import")]


@pytest.mark.parametrize("rel", [WRAPPER, CONFORMANCE, TEST, TOOL])
@pytest.mark.parametrize("source", SWITCHBOARD_IMPORTS)
def test_the_wrapper_conformance_tests_and_tools_may_import(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


def test_runtime_may_use_the_wrapper() -> None:
    bindings = switchboard_lint.wrapper_bindings(REAL_WRAPPER)
    source = "from lucille_host import _switchboard\nhub = _switchboard.LUCILLE_HUB\n"
    assert rules(source, RUNTIME, bindings) == set()
    assert rules("from lucille_host._switchboard import LUCILLE_HUB\n", RUNTIME, bindings) == set()


# The library objects the real wrapper names: none may leave it.
REAL_WRAPPER_LIBRARY_OBJECTS = [
    "from lucille_host._switchboard import CLIENT_CLASS\nCLIENT_CLASS(cfg).post('x', {})\n",
    "from ._switchboard import CLIENT_CLASS\n",
    "from ._switchboard import SHARING_CALLS as calls\ncalls[0](store=s)\n",
    "from lucille_host._switchboard import PAIRING_CALLS\nPAIRING_CALLS[1](blob, store=s)\n",
    "from lucille_host import _switchboard\n_switchboard.SHARING_CALLS[0](store=s)\n",
    # The wrapper module itself as a value, from which a string could pick any name.
    "from lucille_host import _switchboard\ngetattr(_switchboard, name)\n",
    "from lucille_host import _switchboard\nhand_over(_switchboard)\n",
]


@pytest.mark.parametrize("source", REAL_WRAPPER_LIBRARY_OBJECTS)
def test_library_objects_the_wrapper_names_are_switchboard(source: str) -> None:
    bindings = switchboard_lint.wrapper_bindings(REAL_WRAPPER)
    assert "switchboard-import" in rules(source, RUNTIME, bindings)


def test_a_name_the_wrapper_does_not_show_is_its_own_is_presumed_switchboard() -> None:
    source = "from lucille_host._switchboard import anything_at_all\n"
    assert "switchboard-import" in rules(source, RUNTIME)  # the wrapper not read
    bindings = switchboard_lint.wrapper_bindings("LUCILLE_HUB = 'x'\n")
    assert "switchboard-import" in rules(source, RUNTIME, bindings)  # not bound by the wrapper


WRAPPER_FOR_BINDINGS = """\
import functools
from typing import Any

from switchboard import client, pairing

HUB = "https://hub.example"
TIMEOUT: float = 40.0
LIMIT = TIMEOUT * 2
CLIENT = client.Client
CALLS = (pairing.accept,)
ALIAS = CLIENT
LATER = HUB
LATER = client


def post(hub: Any) -> None:
    return client.Client(hub).post("x", {})


@functools.cache
def cached() -> None:
    pass


@client.wrap
def decorated() -> None:
    pass


class Hub:
    pass


class SubClient(client.Client):
    pass


try:
    GUARDED = 1
except ImportError:
    GUARDED = 2

WALRUS = (HIDDEN := client.Client)


def setter() -> None:
    global SET_LATER
    SET_LATER = client.Client


SET_LATER = None
"""


@pytest.mark.parametrize(
    ("name", "stands_for"),
    [
        ("HUB", "lucille_host._switchboard.HUB"),
        ("TIMEOUT", "lucille_host._switchboard.TIMEOUT"),
        ("LIMIT", "lucille_host._switchboard.LIMIT"),
        ("post", "lucille_host._switchboard.post"),
        ("cached", "lucille_host._switchboard.cached"),
        ("Hub", "lucille_host._switchboard.Hub"),
        ("Any", "typing.Any"),
        ("functools", "functools"),
        ("client", "switchboard.client"),
        ("pairing", "switchboard.pairing"),
        # Assigned from switchboard, directly or through another such name.
        ("CLIENT", "switchboard"),
        ("CALLS", "switchboard"),
        ("ALIAS", "switchboard"),
        # Bound twice, once from switchboard.
        ("LATER", "switchboard"),
        # Decorated or derived from switchboard.
        ("decorated", "switchboard"),
        ("SubClient", "switchboard"),
        # Bound where this lint does not classify the binding.
        ("GUARDED", "switchboard"),
        ("WALRUS", "switchboard"),
        ("HIDDEN", "switchboard"),
        ("SET_LATER", "switchboard"),
    ],
)
def test_wrapper_bindings_classify_each_name(name: str, stands_for: str) -> None:
    assert switchboard_lint.wrapper_bindings(WRAPPER_FOR_BINDINGS)[name] == stands_for


def test_a_star_import_in_the_wrapper_taints_every_name_it_could_bind() -> None:
    bindings = switchboard_lint.wrapper_bindings("from switchboard.client import *\nC = Client\n")
    assert bindings["C"] == "switchboard"


def test_the_tree_check_follows_an_assigned_library_object(tmp_path: Path) -> None:
    package = tmp_path / "src" / "lucille_host"
    package.mkdir(parents=True)
    (package / "_switchboard.py").write_text(
        "from switchboard import client\nHUB = 'x'\nCLIENT = client.Client\n"
    )
    (package / "cli.py").write_text(
        "from lucille_host._switchboard import HUB, CLIENT\nCLIENT(HUB).post('x', {})\n"
    )
    found = switchboard_lint.check(tmp_path)
    assert {(v.path, v.line, v.rule) for v in found} >= {
        ("src/lucille_host/cli.py", 1, "switchboard-import"),
        ("src/lucille_host/cli.py", 2, "switchboard-import"),
    }
    assert all(v.path == "src/lucille_host/cli.py" for v in found)


# --- No register, no invites, anywhere -------------------------------------------------------

REGISTER = [
    "import atexit\natexit.register(print)\n",
    "from atexit import register\n",
    "client.register(name='host')\n",
    "register()\n",
    "import codecs\nhook = codecs.register\n",
    "@dispatch.register\ndef handle(value):\n    pass\n",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("source", REGISTER)
def test_register_is_reported_everywhere(source: str, rel: str) -> None:
    assert "register" in rules(source, rel)


REGISTER_BY_STRING = [
    "getattr(client.Client(None), 'register')(name='host')\n",
    "hasattr(c, 'register')\n",
    "import operator\noperator.methodcaller('register', name='host')(c)\n",
    "from operator import attrgetter\nattrgetter('client.register')(c)\n",
    "c.call(**{'register': True})\n",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("source", REGISTER_BY_STRING)
def test_register_by_string_is_reported_everywhere(source: str, rel: str) -> None:
    assert "register" in rules(source, rel)


INVITATION_FORMS = [
    "from switchboard.invite import Invite\n",
    "import switchboard.invite\n",
    "from switchboard import Invite\n",
    "code = room.invite_code\n",
    "invite = 1\n",
    "client.join(invite_code='x')\n",
    f'code = "{PREFIX}abc"\n',
    f'code = b"{PREFIX}abc"\n',
    f'code = f"{PREFIX}{{suffix}}"\n',
    # The same names spelled as strings where a string becomes an attribute or a keyword.
    "client.Client(None).join(**{'invite_code': 'x'})\n",
    "getattr(client.Client, 'from_invite')(blob)\n",
    "setattr(c, 'invite', None)\n",
    # A parameter and an exception name are names too.
    "def accept(invite_code):\n    return invite_code\n",
    "def accept(*, invite=None):\n    pass\n",
    "try:\n    pass\nexcept Exception as invite:\n    pass\n",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("source", INVITATION_FORMS)
def test_invites_are_reported_everywhere(source: str, rel: str) -> None:
    assert "invite" in rules(source, rel)


def test_a_test_may_be_named_for_the_invite_it_forbids() -> None:
    source = "def test_host_never_invites():\n    assert 'no invite was sent'\n"
    assert rules(source, TEST) == set()


def test_a_test_may_patch_an_invite_entry_point_by_its_dotted_path() -> None:
    source = (
        "def test_host_never_invites(monkeypatch):\n"
        "    monkeypatch.setattr('switchboard.invite.from_env', fail)\n"
    )
    assert rules(source, TEST) == set()


# --- share_new and the sharing members -------------------------------------------------------

SHARE_NEW = [
    "from switchboard import sharing\nsharing.share_new('room', None)\n",
    "from switchboard.sharing import share_new\n",
    "import switchboard.sharing as shares\nshares.share_new\n",
    "room = factory.share_new()\n",
    "getattr(factory, 'share_new')('room', None)\n",
]


@pytest.mark.parametrize("rel", [RUNTIME, WRAPPER, TOOL])
@pytest.mark.parametrize("source", SHARE_NEW)
def test_share_new_is_reported_outside_conformance_and_tests(source: str, rel: str) -> None:
    assert "share-new" in rules(source, rel)


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST])
@pytest.mark.parametrize("source", SHARE_NEW)
def test_conformance_and_tests_may_mint_rooms(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


OTHER_SHARING_MEMBERS = [
    "from switchboard import sharing\nsharing.status(store=None)\n",
    "from switchboard.sharing import recent_rooms\n",
    "import switchboard.sharing as shares\nshares.notes(store=None)\n",
    "import switchboard.sharing\nswitchboard.sharing.active_pairs(store=None)\n",
    "import switchboard\nswitchboard.sharing.Shared\n",
    "from switchboard import sharing\ngetattr(sharing, 'status')\n",
    "from switchboard import sharing\nhand_over(sharing)\n",
    "from switchboard.sharing import *\n",
    "from switchboard import *\n",
    "from lucille_host import _switchboard\n_switchboard.sharing.status(store=None)\n",
    # A chain with a sharing component is taken to be the module, whatever it is reached through.
    "import helpers\nhelpers.sharing.notes(store=None)\n",
]


@pytest.mark.parametrize("rel", [WRAPPER, TOOL])
@pytest.mark.parametrize("source", OTHER_SHARING_MEMBERS)
def test_other_sharing_members_are_reported(source: str, rel: str) -> None:
    assert "sharing-member" in rules(source, rel)


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST])
@pytest.mark.parametrize("source", OTHER_SHARING_MEMBERS)
def test_conformance_and_tests_may_use_any_sharing_member(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


ALLOWED_SHARING = [
    "from switchboard import sharing\n"
    "sharing.touch(pairs=['lucille'], store=shares)\n"
    "sharing.accept('offer', store=shares)\n"
    "sharing.resolve_room('room', store=shares)\n"
    "sharing.leave('room', store=shares)\n",
    "from switchboard.sharing import accept, leave, resolve_room, touch\n",
    "import switchboard.sharing as sh\nsh.touch(store=shares)\n",
    # The types those calls take and return make no hub call.
    "from switchboard import sharing\n"
    "shares = sharing.ShareStore(state / 'pairs', env={})\n"
    "try:\n    pass\nexcept sharing.SharingError:\n    pass\n",
    "from switchboard.sharing import Kept, Left, SharedRoom, Store, Touched\n",
]


@pytest.mark.parametrize("source", ALLOWED_SHARING)
def test_the_wrapper_may_take_offered_rooms(source: str) -> None:
    assert rules(source, WRAPPER) == set()


# --- The rest of the Switchboard surface -----------------------------------------------------

SURFACE_DENIED = [
    "from switchboard import cli\ncli.main(['share', 'new', 'room', '--pair', 'lucille'])\n",
    "import switchboard.cli\nswitchboard.cli.main([])\n",
    "import switchboard.mcp_server\n",
    "from switchboard import rendezvous\n",
    "from switchboard import testing\n",
    "from switchboard.server import create_app\n",
    "from switchboard import pairing\npairing.start(None, store=None)\n",
    "from switchboard import pairing\npairing.collect('lucille', store=None)\n",
    "from switchboard import pairing\npairing.export(None, for_peer=True)\n",
    "from switchboard import pairing\npairing.heal('lucille', store=None)\n",
    "from switchboard.pairing import from_secret\n",
    "import switchboard\ngetattr(switchboard, 'cli')\n",
    "from switchboard import pairing\nhand_over(pairing)\n",
    "from switchboard import client\nhand_over(client)\n",
]


@pytest.mark.parametrize("rel", [WRAPPER, TOOL])
@pytest.mark.parametrize("source", SURFACE_DENIED)
def test_parts_of_switchboard_outside_the_surface_are_reported(source: str, rel: str) -> None:
    assert "switchboard-surface" in rules(source, rel)


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST])
@pytest.mark.parametrize("source", SURFACE_DENIED)
def test_conformance_and_tests_may_use_any_part_of_switchboard(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


SURFACE_ALLOWED = [
    "from switchboard import pairing\n"
    "pairing.PairingCode.decode(code)\n"
    "pairing.accept(blob, store=s)\n"
    "pairing.resolve('lucille', store=s)\n"
    "pairing.forget('lucille', store=s)\n"
    "pairing.status(store=s)\n",
    "from switchboard.pairing import AlreadyRedeemed, PairStore, Resolution\n",
    "from switchboard.signing import SigningIdentity\nSigningIdentity.generate()\n",
    "from switchboard.crypto import DecryptionError\n",
    "from switchboard.config import ClientConfig\n",
    "from switchboard import client\nclient.Client(config).post('lucille.exec', body)\n",
    "import switchboard\n",
    # A store of the host's own names the handshake record it keeps, and the verdict window.
    "from switchboard import pairing\n"
    "class KeyringStore(pairing.Store):\n"
    "    def pending(self, name: str) -> pairing.PendingHandshake | None:\n"
    "        return pairing.PendingHandshake.from_json(self.read(name), origin=name)\n"
    "    def keep(self) -> float:\n"
    "        return pairing.VERDICT_KEEP + 1\n",
    # Telling the library's notes and the hub's errors apart makes no hub call.
    "from switchboard import pairing\n"
    "def classify(message):\n"
    "    tag = pairing.message_tag(message)\n"
    "    notes = (pairing.TAG_PAIRED, pairing.TAG_MOVED, pairing.TAG_ENDED, pairing.TAG_SHARED)\n"
    "    return tag in notes\n"
    "def hub_failed(error):\n"
    "    return isinstance(error, pairing.hub_errors())\n",
    # The read loop reads the pair room's channel the library posts its notes on.
    "from switchboard import pairing\n"
    "def read(hub, cursor):\n"
    "    return hub.inbox(channels=[pairing.CHANNEL], since=cursor)\n",
    "from switchboard.pairing import CHANNEL\n",
]


@pytest.mark.parametrize("source", SURFACE_ALLOWED)
def test_the_wrapper_may_use_the_surface(source: str) -> None:
    assert rules(source, WRAPPER) == set()


# The surface, written out here so that dropping or adding a member fails a test.
PINNED_SURFACE = {
    "pairing": {
        "PairingCode",
        "accept",
        "resolve",
        "forget",
        "status",
        "PairStore",
        "Store",
        "PairRecord",
        "PendingHandshake",
        "VERDICT_KEEP",
        "Resolution",
        "Accepted",
        "Forgotten",
        "PairingError",
        "AlreadyRedeemed",
        "message_tag",
        "CHANNEL",
        "TAG_PAIRED",
        "TAG_MOVED",
        "TAG_ENDED",
        "TAG_SHARED",
        "hub_errors",
    },
    "client": {"Client", "SwitchboardError", "ReadOnlyRoom"},
    "sharing": {
        "touch",
        "accept",
        "resolve_room",
        "leave",
        "ShareStore",
        "Store",
        "SharingError",
        "Touched",
        "Kept",
        "Left",
        "SharedRoom",
    },
    "signing": {"SigningIdentity"},
    "crypto": {"CryptoError", "DecryptionError"},
    "config": {"ClientConfig"},
}


def test_the_surface_is_pinned() -> None:
    assert {sub: set(members) for sub, members in switchboard_lint.SURFACE.items()} == (
        PINNED_SURFACE
    )


SURFACE_MEMBERS = sorted(
    (sub, member) for sub, members in PINNED_SURFACE.items() for member in members
)


@pytest.mark.parametrize(("sub", "member"), SURFACE_MEMBERS)
def test_each_surface_member_is_allowed_in_the_wrapper(sub: str, member: str) -> None:
    assert rules(f"from switchboard.{sub} import {member}\n", WRAPPER) == set()


LIBRARY_SUBMODULES = {
    "pairing": pairing,
    "client": client,
    "sharing": sharing,
    "signing": signing,
    "crypto": crypto,
    "config": config,
}


@pytest.mark.parametrize(("sub", "member"), SURFACE_MEMBERS)
def test_each_surface_member_exists_in_the_pinned_library(sub: str, member: str) -> None:
    assert member in dir(LIBRARY_SUBMODULES[sub])


# --- Private members in the runtime ----------------------------------------------------------

PRIVATE_MEMBERS = [
    (WRAPPER, "from switchboard import client\nclient.Client(None)._call('POST', '/x', json={})\n"),
    (WRAPPER, "def post(hub):\n    return hub._call('POST', '/x')\n"),
    (RUNTIME, "import os\nos._exit(1)\n"),
    (RUNTIME, "from lucille_host import _switchboard\n_switchboard.sharing._touch(None)\n"),
    # Only an attribute directly on self or cls, or directly on a module of this package, is own.
    (WRAPPER, "class Host:\n    def post(self):\n        return self.hub._call('POST', '/x')\n"),
    (
        RUNTIME,
        "class Host:\n    def post(self):\n        return self._client._call('POST', '/x')\n",
    ),
    (
        RUNTIME,
        "class Host:\n    @classmethod\n    def post(cls):\n        return cls.hub._call('POST')\n",
    ),
    (
        RUNTIME,
        "from lucille_host import _switchboard\n_switchboard.CLIENT_CLASS._call(c, 'POST')\n",
    ),
    (RUNTIME, "import lucille_host.cli\nlucille_host.cli.sys._getframe()\n"),
    (RUNTIME, "from lucille_host import cli\ncli.sys._getframe()\n"),
    # A private name imported from a module outside this package.
    (RUNTIME, "from tempfile import _os\n"),
    (RUNTIME, "from argparse import _os as o\n"),
    (RUNTIME, "from switchboard.client import _call\n"),
    ("src/lucille_host/ops/catalog.py", "from collections import _sys\n"),
]


@pytest.mark.parametrize(("rel", "source"), PRIVATE_MEMBERS)
def test_private_members_of_other_objects_are_reported_in_the_runtime(
    rel: str, source: str
) -> None:
    assert "private-member" in rules(source, rel)


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST, TOOL])
def test_private_members_are_not_checked_outside_the_runtime(rel: str) -> None:
    assert rules("def post(hub):\n    return hub._call('POST', '/x')\n", rel) == set()


OWN_PRIVATE_MEMBERS = [
    "from . import _exec\n_exec.run(['x'])\n",
    "from lucille_host._exec import run\nrun(['x'])\n",
    "class Store:\n    def get(self):\n        return self._data\n",
    "class Store:\n    @classmethod\n    def make(cls):\n        return cls._cache\n",
    "from lucille_host import _exec\n_exec.run(['x'])\n",
    "import lucille_host._exec\nlucille_host._exec.run(['x'])\n",
    "from lucille_host import _exec\n_exec._helper(['x'])\n",
    "import lucille_host._exec\nlucille_host._exec._helper(['x'])\n",
    "from lucille_host import ops\nops._table\n",
]


@pytest.mark.parametrize("source", OWN_PRIVATE_MEMBERS)
def test_the_runtime_may_use_its_own_private_members(source: str) -> None:
    assert rules(source, RUNTIME) == set()


def test_own_modules_are_this_packages_modules() -> None:
    modules = switchboard_lint.own_modules(REPO_ROOT)
    assert {"lucille_host", "lucille_host._exec", "lucille_host.ops", "lucille_host.cli"} <= modules
    assert "lucille_host._switchboard.CLIENT_CLASS" not in modules


def test_conformance_and_tests_are_not_exempt_from_register_or_invites() -> None:
    source = f"import atexit\natexit.register(print)\ncode = '{PREFIX}x'\n"
    for rel in (CONFORMANCE, TEST):
        assert {"register", "invite"} <= rules(source, rel)


# --- The real repository and the script ------------------------------------------------------


def test_repository_is_clean() -> None:
    files = switchboard_lint.python_files(REPO_ROOT)
    assert "src/lucille_host/_switchboard.py" in files
    assert switchboard_lint.check(REPO_ROOT) == []


def _sharing_members(tree: ast.Module) -> set[str]:
    """The members of switchboard.sharing a module imports or uses, however it imports them."""
    prefix = switchboard_lint.SHARING + "."
    return {
        ref.qualified[len(prefix) :].split(".")[0]
        for ref in references(tree, WRAPPER)
        if ref.qualified.startswith(prefix)
    }


class _ToOtherSharingMember(ast.NodeTransformer):
    """Rename every allowed sharing member the wrapper names to one outside the surface."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        base = node.value
        if isinstance(base, ast.Name) and self.aliases.get(base.id) == switchboard_lint.SHARING:
            node.attr = "notes"
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.module == switchboard_lint.SHARING:
            for alias in node.names:
                alias.name = "notes"
        return node


def test_the_wrapper_really_imports_the_sharing_module() -> None:
    """The repository-clean test above means something only if the wrapper uses sharing."""
    tree = ast.parse(REAL_WRAPPER)
    members = _sharing_members(tree)
    assert members, "the wrapper names no member of switchboard.sharing"
    assert members <= set(switchboard_lint.SURFACE["sharing"])
    assert rules(REAL_WRAPPER.decode(), WRAPPER) == set()
    # The same wrapper, naming a sharing member outside the surface, is refused.
    aliases = import_aliases(tree, package_of(WRAPPER))
    mutated = ast.unparse(_ToOtherSharingMember(aliases).visit(tree))
    assert "sharing-member" in rules(mutated, WRAPPER)


# --- A * import of the wrapper ----------------------------------------------------------------
# The wrapper has no __all__ of names, so ``import *`` would bind every public name it has.

STAR_OF_THE_WRAPPER = [
    ("src/lucille_host/sneaky.py", "from ._switchboard import *\npairing.start(None)\n"),
    (
        "src/lucille_host/sneaky.py",
        "from lucille_host._switchboard import *\nclient.Client(None)\n",
    ),
    ("src/lucille_host/ops/x.py", "from .._switchboard import *\npairing.heal(None)\n"),
]


@pytest.mark.parametrize(("rel", "source"), STAR_OF_THE_WRAPPER)
def test_a_star_import_of_the_wrapper_is_a_switchboard_import(rel: str, source: str) -> None:
    bindings = switchboard_lint.wrapper_bindings(REAL_WRAPPER)
    found = rules(source, rel, bindings)
    assert {"switchboard-import", "sharing-member"} <= found


def test_the_wrapper_exports_no_name_to_a_star_import() -> None:
    tree = ast.parse(REAL_WRAPPER)
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    assert len(assigned) == 1
    assert isinstance(assigned[0], ast.List | ast.Tuple) and assigned[0].elts == []


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_script_exit_codes(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run_script(str(empty)).returncode == 2
    assert _run_script("--bad-flag").returncode == 2

    module = tmp_path / "tree" / "src" / "lucille_host" / "helper.py"
    module.parent.mkdir(parents=True)
    module.write_text("from lucille_host import _switchboard\n")
    assert _run_script(str(tmp_path / "tree")).returncode == 0

    module.write_text("import json\nfrom switchboard import pairing\n")
    result = _run_script(str(tmp_path / "tree"))
    assert result.returncode == 1
    assert "src/lucille_host/helper.py:2: [switchboard-import]" in result.stdout
