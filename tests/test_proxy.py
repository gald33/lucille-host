"""github.read's child (``src/lucille_host/proxy.py``): decisions/0003, "First use".

The keyring, the resolver and the connection are given to :func:`proxy.main`, so nothing here
touches the network or a real keyring. The connection is a stand-in that records what was sent
and answers with the bytes a test gives it, as a server would. Tokens are built when the tests
run, never written whole in this file.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from lucille_host import proxy
from lucille_host._output import TOKEN_MARK

TOKEN = "ghp" + "_" + "Ab1" * 12
FINE_GRAINED = "github" + "_pat_" + "11AB" * 6 + "_" + "cd" * 20
ARGV = ["--state", "/tmp/s", "--profile", "default", "--", "personal", "/user"]
GITHUB_V4 = "140.82.112.6"
GITHUB_V6 = "2606:50c0:8000::154"
MARK = TOKEN_MARK.encode("ascii")


class FakeSocket:
    """A connection that records what is sent and answers with ``response``."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str) -> io.BufferedReader:
        assert mode == "rb"
        return io.BufferedReader(io.BytesIO(self.response))

    def close(self) -> None:
        self.closed = True


def _creds(
    value: str = TOKEN, hosts: tuple[str, ...] = ("api.github.com",)
) -> Callable[[str, str, str], tuple[tuple[str, ...], str] | None]:
    def read(state: str, profile: str, name: str) -> tuple[tuple[str, ...], str] | None:
        assert (state, profile, name) == ("/tmp/s", "default", "personal")
        return hosts, value

    return read


def _http(status: str, headers: list[str], body: bytes = b"") -> bytes:
    head = [f"HTTP/1.1 {status}", *headers]
    if not any(h.lower().startswith(("content-length", "transfer-encoding")) for h in headers):
        head.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def _main(
    response: bytes,
    *,
    argv: list[str] | None = None,
    read: Any = None,
    addresses: tuple[str, ...] = (GITHUB_V4,),
) -> tuple[proxy.Reply, list[FakeSocket], list[str]]:
    sockets: list[FakeSocket] = []
    connected: list[str] = []

    def connect(address: str) -> FakeSocket:
        connected.append(address)
        sock = FakeSocket(response)
        sockets.append(sock)
        return sock

    reply = proxy.main(
        ARGV if argv is None else argv,
        read_credential=read or _creds(),
        resolve=lambda host, port: list(addresses),
        connect=connect,
    )
    return reply, sockets, connected


# --- what is sent ---------------------------------------------------------------------------


def test_one_get_with_the_fixed_headers_and_the_token_only_in_authorization() -> None:
    reply, sockets, connected = _main(_http("200 OK", [], b'{"login":"someone"}'))
    assert reply == proxy.Reply(0, b'{"login":"someone"}', b"HTTP 200\n")
    assert connected == [GITHUB_V4]
    [sock] = sockets
    assert sock.closed
    head, _, body = sock.sent.partition(b"\r\n\r\n")
    assert body == b""
    lines = head.decode("ascii").split("\r\n")
    assert lines[0] == "GET /user HTTP/1.1"
    assert lines[1:] == [
        "Host: api.github.com",
        f"Authorization: Bearer {TOKEN}",
        "Accept: application/vnd.github+json",
        "Accept-Encoding: identity",
        "X-GitHub-Api-Version: 2022-11-28",
        f"User-Agent: lucille-host/{proxy.__version__}",
        "Connection: close",
    ]
    assert sock.sent.count(TOKEN.encode()) == 1


def test_a_fine_grained_token_is_taken() -> None:
    reply, sockets, _ = _main(_http("200 OK", [], b"[]"), read=_creds(FINE_GRAINED))
    assert reply.code == 0
    assert f"Authorization: Bearer {FINE_GRAINED}".encode() in sockets[0].sent


def test_the_path_and_query_go_as_given() -> None:
    argv = [*ARGV[:-1], "/repos/someone/thing/issues?state=open&per_page=5"]
    _, sockets, _ = _main(_http("200 OK", [], b"[]"), argv=argv)
    assert sockets[0].sent.startswith(
        b"GET /repos/someone/thing/issues?state=open&per_page=5 HTTP/1.1\r\n"
    )


# --- the arguments --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ARGV[:-1],
        [*ARGV, "extra"],
        ["--profile", "default", "--state", "/tmp/s", "--", "personal", "/user"],
        ["--state", "relative", *ARGV[2:]],
        ["--state", "/tmp/s", "--profile", "Not Valid", *ARGV[4:]],
        [*ARGV[:-1], "/repos/a/.."],
        [*ARGV[:-1], "/orgs/someone/members"],
        [*ARGV[:-1], "/user?token=1"],
        [*ARGV[:-2], "Personal", "/user"],
    ],
)
def test_a_malformed_argument_list_is_internal_and_reads_nothing(argv: list[str]) -> None:
    def read(*_: str) -> None:
        raise AssertionError("read a credential")

    reply, sockets, _ = _main(b"", argv=argv, read=read)
    assert reply == proxy.Reply(6, b"", b"lucille-host proxy: internal error\n")
    assert sockets == []


# --- the credential -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("read", "line"),
    [
        (lambda *_: None, "no such credential"),
        (_creds(hosts=("github.com",)), "the credential is not bound to api.github.com"),
        (_creds(hosts=()), "the credential is not bound to api.github.com"),
        (_creds("hunter2"), "the credential is not a GitHub token"),
        (_creds("ghp" + "_" + "A" * 35), "the credential is not a GitHub token"),
        (_creds("ghp" + "_" + "A" * 36 + "_"), "the credential is not a GitHub token"),
        (_creds("github" + "_pat_" + "A" * 21), "the credential is not a GitHub token"),
        (_creds(TOKEN + "\n"), "the credential is not a GitHub token"),
        (_creds(" " + TOKEN), "the credential is not a GitHub token"),
    ],
)
def test_the_credential_is_refused_before_anything_is_sent(read: Any, line: str) -> None:
    reply, sockets, _ = _main(b"", read=read)
    assert reply == proxy.Reply(2, b"", f"lucille-host proxy: {line}\n".encode())
    assert sockets == []


def test_a_keyrings_error_is_not_shown() -> None:
    def read(*_: str) -> None:
        raise OSError(f"the keyring said {TOKEN}")

    reply, sockets, _ = _main(b"", read=read)
    assert reply == proxy.Reply(
        2, b"", b"lucille-host proxy: the keyring did not give the credential\n"
    )
    assert sockets == []


def test_the_token_form() -> None:
    assert proxy.TOKEN.fullmatch(TOKEN) and proxy.TOKEN.fullmatch(FINE_GRAINED)
    assert proxy.TOKEN.fullmatch("ghp" + "_" + "a" * 251)
    assert not proxy.TOKEN.fullmatch("ghp" + "_" + "a" * 252)
    assert proxy.TOKEN.fullmatch("github" + "_pat_" + "_" * 255)
    assert not proxy.TOKEN.fullmatch("github" + "_pat_" + "a" * 256)
    assert not proxy.TOKEN.fullmatch("ghp" + "_" + "١" * 36)


# --- the address ----------------------------------------------------------------------------


def _v4(value: int) -> str:
    return str(ipaddress.IPv4Address(value))


# The shared address space for carrier-grade NAT, from its integer value.
CGNAT_FIRST = 0x64400000
CGNAT_LAST = CGNAT_FIRST + (1 << 22) - 1

REFUSED = [
    "0.0.0.0",
    "0.255.255.255",
    "10.0.0.1",
    _v4(CGNAT_FIRST),
    _v4(CGNAT_FIRST + 1),
    _v4(CGNAT_LAST),
    "127.0.0.1",
    "169.254.169.254",
    "172.16.0.1",
    "172.31.255.255",
    "192.0.0.1",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.19.255.255",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::ffff:" + GITHUB_V4,
    "::ffff:127.0.0.1",
    "64:ff9b::a00:1",
    "64:ff9b:1::1",
    "100::1",
    "2001::1",
    "2001:1ff:ffff::1",
    "2001:db8::1",
    "2002:a00:1::1",
    "3fff::1",
    "3fff:fff:ffff::1",
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "fe80::1%eth0",
    "ff02::1",
    "not an address",
    "",
]
ALLOWED = [
    GITHUB_V4,
    "1.1.1.1",
    "9.255.255.255",
    "11.0.0.0",
    _v4(CGNAT_FIRST - 1),
    _v4(CGNAT_LAST + 1),
    "172.15.255.255",
    "172.32.0.0",
    "192.0.1.0",
    "198.17.255.255",
    "198.20.0.0",
    "223.255.255.255",
    GITHUB_V6,
    "2001:200::1",
    "2001:db9::1",
    "2003::1",
    "3ffe::1",
    "3fff:1000::1",
]


def test_the_shared_address_space_is_built_from_its_integer() -> None:
    assert _v4(CGNAT_FIRST).startswith("100.64.")
    assert _v4(CGNAT_LAST).startswith("100.127.")


@pytest.mark.parametrize("address", REFUSED)
def test_refused_addresses(address: str) -> None:
    assert not proxy.address_allowed(address)


@pytest.mark.parametrize("address", ALLOWED)
def test_allowed_addresses(address: str) -> None:
    assert proxy.address_allowed(address)


def test_one_refused_address_refuses_the_whole_request() -> None:
    reply, sockets, _ = _main(b"", addresses=(GITHUB_V4, GITHUB_V6, "10.0.0.1"))
    assert reply == proxy.Reply(
        4, b"", b"lucille-host proxy: api.github.com resolved to an address that is not allowed\n"
    )
    assert sockets == []


@pytest.mark.parametrize("failure", ["empty", "gaierror"])
def test_a_name_that_does_not_resolve(failure: str) -> None:
    def resolve(host: str, port: int) -> list[str]:
        assert (host, port) == ("api.github.com", 443)
        if failure == "empty":
            return []
        raise OSError("no such name")

    reply = proxy.main(ARGV, read_credential=_creds(), resolve=resolve, connect=None)
    assert reply == proxy.Reply(4, b"", b"lucille-host proxy: api.github.com did not resolve\n")


def test_it_connects_to_vetted_addresses_only_in_order() -> None:
    tried: list[str] = []
    good = FakeSocket(_http("200 OK", [], b"{}"))

    def connect(address: str) -> FakeSocket:
        tried.append(address)
        if address == GITHUB_V6:
            raise OSError("unreachable")
        return good

    reply = proxy.main(
        ARGV,
        read_credential=_creds(),
        resolve=lambda *_: [GITHUB_V6, GITHUB_V4],
        connect=connect,
    )
    assert reply.code == 0 and tried == [GITHUB_V6, GITHUB_V4]


def test_no_connection_is_exit_4() -> None:
    def connect(address: str) -> FakeSocket:
        raise OSError("refused")

    reply = proxy.main(
        ARGV, read_credential=_creds(), resolve=lambda *_: [GITHUB_V4], connect=connect
    )
    assert reply == proxy.Reply(
        4, b"", b"lucille-host proxy: could not connect to api.github.com\n"
    )


# --- the response ---------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "code"), [("201 Created", 0), ("204 No Content", 0)])
def test_any_2xx_is_exit_0(status: str, code: int) -> None:
    reply, _, _ = _main(_http(status, []))
    assert reply.code == code and reply.stderr == f"HTTP {status[:3]}\n".encode()


@pytest.mark.parametrize("status", ["301 Moved Permanently", "302 Found", "304 Not Modified"])
def test_a_redirect_is_reported_never_followed(status: str) -> None:
    response = _http(status, ["Location: https://example.com/elsewhere"], b"moved")
    reply, sockets, connected = _main(response)
    assert reply.code == 3
    assert reply.stderr == f"HTTP {status[:3]}\n".encode()
    assert len(sockets) == 1 and len(connected) == 1
    assert sockets[0].sent.count(b"GET ") == 1


@pytest.mark.parametrize("status", ["401 Unauthorized", "404 Not Found", "500 Server Error"])
def test_another_status_is_exit_3_with_its_body(status: str) -> None:
    reply, _, _ = _main(_http(status, [], b'{"message":"Not Found"}'))
    assert reply == proxy.Reply(3, b'{"message":"Not Found"}', f"HTTP {status[:3]}\n".encode())


def test_the_link_header_goes_on_standard_error() -> None:
    link = '<https://api.github.com/user/repos?page=2>; rel="next"'
    reply, _, _ = _main(_http("200 OK", [f"Link: {link}"], b"[]"))
    assert reply.stderr == f"HTTP 200\nLink: {link}\n".encode()


def test_a_chunked_body_is_joined_before_the_scrub() -> None:
    # The token split across two chunks is one token once the chunks are joined.
    raw = TOKEN.encode()
    pieces = [b'{"echo":"' + raw[:10], raw[10:] + b'"}']
    chunked = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"
    reply, _, _ = _main(_http("200 OK", ["Transfer-Encoding: chunked"], chunked))
    assert reply == proxy.Reply(0, b'{"echo":"' + MARK + b'"}', b"HTTP 200\n")


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
def test_a_compressed_body_is_not_read(encoding: str) -> None:
    reply, _, _ = _main(_http("200 OK", [f"Content-Encoding: {encoding}"], TOKEN.encode()))
    assert reply == proxy.Reply(
        5, b"", b"HTTP 200\nlucille-host proxy: the response is compressed; not read\n"
    )


def test_identity_encoding_is_read() -> None:
    reply, _, _ = _main(_http("200 OK", ["Content-Encoding: identity"], b"{}"))
    assert reply == proxy.Reply(0, b"{}", b"HTTP 200\n")


def test_headers_past_64_kib_are_exit_5() -> None:
    headers = [f"X-Filler-{n}: {'a' * 1000}" for n in range(70)]
    reply, _, _ = _main(_http("200 OK", headers, b"{}"))
    assert reply == proxy.Reply(
        5, b"", b"lucille-host proxy: the response's headers are too large\n"
    )


def test_more_than_100_headers_are_exit_5() -> None:
    headers = [f"X-H{n}: 1" for n in range(101)]
    reply, _, _ = _main(_http("200 OK", headers, b"{}"))
    assert reply == proxy.Reply(5, b"", b"lucille-host proxy: the response has too many headers\n")


def test_a_body_of_exactly_1_mib_is_whole() -> None:
    body = b"x" * proxy.BODY_CAP
    reply, _, _ = _main(_http("200 OK", [], body))
    assert reply.code == 0 and reply.stdout == body


def test_a_body_past_1_mib_is_cut_there_and_exit_5() -> None:
    body = b"x" * proxy.BODY_CAP + b"tail"
    reply, _, _ = _main(_http("200 OK", [], body))
    assert reply == proxy.Reply(
        5,
        b"x" * proxy.BODY_CAP,
        b"HTTP 200\nlucille-host proxy: the response is larger than 1 MiB; cut there\n",
    )


@pytest.mark.parametrize("chunked", [False, True])
def test_at_the_cut_a_beginning_of_the_token_is_dropped(chunked: bool) -> None:
    raw = TOKEN.encode()
    body = b"y" * (proxy.BODY_CAP - 12) + raw
    if chunked:
        payload = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        response = _http("200 OK", ["Transfer-Encoding: chunked"], payload)
    else:
        response = _http("200 OK", [], body)
    reply, _, _ = _main(response)
    assert reply.code == 5
    assert reply.stdout == b"y" * (proxy.BODY_CAP - 12)


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n",
        b"not http at all\r\n\r\n",
        _http("200 OK", ["Content-Length: 100"], b"short"),
    ],
)
def test_a_connection_that_breaks_before_the_response_is_read_is_exit_4(response: bytes) -> None:
    reply, _, _ = _main(response)
    assert reply == proxy.Reply(
        4, b"", b"lucille-host proxy: the connection broke before the response\n"
    )


# --- the scrub ------------------------------------------------------------------------------


@pytest.mark.parametrize("token", [TOKEN, FINE_GRAINED])
@pytest.mark.parametrize("before", [b"", b"a", b"ab"])
def test_the_scrub_covers_the_token_and_its_base64_at_each_alignment(
    token: str, before: bytes
) -> None:
    encoded = base64.b64encode(before + token.encode() + b"tail")
    scrubbed = proxy.scrub(b"x " + encoded + b" y " + token.encode(), token)
    assert token.encode() not in scrubbed
    for form in proxy._forms(token):
        assert form not in scrubbed
    assert scrubbed.count(MARK) == 2


def test_the_scrub_covers_url_and_json_escaped_echoes() -> None:
    # The token's characters are left as they are by URL-encoding and JSON's escaping.
    reply, _, _ = _main(
        _http(
            "200 OK",
            [f'Link: <https://api.github.com/x?t={TOKEN}>; rel="next"'],
            f'{{"url":"https:\\/\\/x?t={TOKEN}","q":"a%20{TOKEN}"}}'.encode(),
        )
    )
    assert TOKEN.encode() not in reply.stdout + reply.stderr
    assert reply.stdout.count(MARK) == 2 and reply.stderr.count(MARK) == 1


def test_the_scrub_drops_a_tail_that_begins_the_token_only_at_a_cut() -> None:
    raw = TOKEN.encode()
    assert proxy.scrub(b"abc" + raw[:5], TOKEN, cut=True) == b"abc"
    assert proxy.scrub(b"abc" + raw[:5], TOKEN) == b"abc" + raw[:5]
    assert proxy.scrub(b"abc", TOKEN, cut=True) == b"abc"


# --- the process ----------------------------------------------------------------------------


def _child(*args: str, code: str | None = None) -> subprocess.CompletedProcess[bytes]:
    command = [sys.executable, "-I", "-B", "-W", "ignore", "-X", "utf8"]
    command += ["-c", code] if code is not None else ["-m", "lucille_host.proxy"]
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


@pytest.mark.parametrize("args", [(), ("--help",), ("--state",), tuple(ARGV[:-1])])
def test_the_child_refuses_a_malformed_argument_list_with_one_line(args: tuple[str, ...]) -> None:
    done = _child(*args)
    assert (done.returncode, done.stdout, done.stderr) == (
        6,
        b"",
        b"lucille-host proxy: internal error\n",
    )


def test_the_childs_handler_writes_no_exceptions_text() -> None:
    code = (
        "import sys\n"
        "from lucille_host import proxy\n"
        "def boom(argv):\n"
        f"    raise RuntimeError({TOKEN!r})\n"
        "proxy.main = boom\n"
        "sys.argv = ['proxy']\n"
        "proxy.run()\n"
    )
    done = _child(code=code)
    assert (done.returncode, done.stdout, done.stderr) == (
        6,
        b"",
        b"lucille-host proxy: internal error\n",
    )


def test_the_child_writes_each_stream_once_and_ends_with_its_code() -> None:
    code = (
        "import sys\n"
        "from lucille_host import proxy\n"
        "proxy.main = lambda argv: proxy.Reply(3, b'body', b'HTTP 404\\n')\n"
        "proxy.run()\n"
    )
    done = _child(code=code)
    assert (done.returncode, done.stdout, done.stderr) == (3, b"body", b"HTTP 404\n")
