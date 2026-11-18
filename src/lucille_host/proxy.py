"""``github.read``'s child: one read of GitHub's REST API with a credential kept on this computer.

The catalog runs it as ``<the host's interpreter> -I -B -W ignore -X utf8 -m lucille_host.proxy
--state <state folder> --profile <profile> -- <credential> <path>`` (decisions/0003, "First use"),
never inside ``run``: only this short-lived process reads a credential's value. What it does, in
order, each refusal writing one fixed line on standard error, holding no value and sending nothing:

0. It reads its arguments by position, never with ``argparse`` (whose usage error exits 2, which
   is the credential's code below), and checks them again against the catalog's schema. A
   malformed list, and any exception it does not raise on purpose, writes
   :data:`INTERNAL_LINE`, never the exception's text, and exits 6.
1. It reads ``github/<credential>`` from the profile's credential store, in one read of the
   keyring item, and refuses (exit 2) when there is none, when its bound hosts do not include
   ``api.github.com``, or when its value is not in the form of a GitHub token (:data:`TOKEN`).
2. It resolves ``api.github.com`` and refuses (exit 4) unless every address it gets is allowed by
   :func:`address_allowed`, a fixed list, never ``ipaddress``'s ``is_global``, whose meaning
   changed between Python versions.
3. It connects to a vetted address, never to the name again, with TLS for ``api.github.com``
   checked against certifi's bundle, and makes one ``GET`` with fixed headers and ``Connection:
   close``. It reads no proxy setting from the environment.
4. It follows no redirect, reads at most :data:`HEADER_CAP` bytes of headers and
   :data:`BODY_CAP` of body (exit 5 past either, with what it read of the body), and reads no
   compressed body and no transfer coding but a plain ``chunked`` (exit 5, no body).
5. It replaces the token, raw and in base64 at each of the three byte alignments, in the whole
   body and in what goes on standard error (:func:`scrub`), and at a cut drops any tail that
   begins the token.
6. It writes standard output once, at the end, then standard error (``HTTP <status>``, and
   GitHub's ``Link`` header when there is one), and ends with ``os._exit``, so no finalizer's text
   or failed final flush follows. From its start, descriptors 1 and 2 point at the null device and
   it writes the reply through copies of them, so nothing a library writes reaches the reply.

Exit codes: 0 for a 2xx, 3 for any other status, 2 the credential, 4 the address or the
connection (also when it breaks before the response has been read), 5 a cap or a compressed body,
6 anything else. 1 is the interpreter's own, when it could not start this module, before any
credential was read.
"""

from __future__ import annotations

import binascii
import http.client
import ipaddress
import os
import re
import socket
import ssl
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

import certifi

from lucille_host import __version__
from lucille_host._output import TOKEN_MARK

__all__ = [
    "BODY_CAP",
    "HEADER_CAP",
    "HOST",
    "INTERNAL_LINE",
    "Reply",
    "TOKEN",
    "address_allowed",
    "main",
    "scrub",
]

HOST: Final = "api.github.com"
PORT: Final = 443
SERVICE: Final = "github"
HEADER_CAP: Final = 64 * 1024
BODY_CAP: Final = 1024 * 1024
# Seconds for each step on the network; the catalog's time limit (30 s) bounds the whole.
NET_TIMEOUT_S: Final = 20.0
_READ_CHUNK: Final = 64 * 1024

TOKEN: Final = re.compile(r"github_pat_[A-Za-z0-9_]{22,255}|ghp_[A-Za-z0-9]{36,251}", re.ASCII)

OK: Final = 0
CREDENTIAL: Final = 2
STATUS: Final = 3
ADDRESS: Final = 4
CAP: Final = 5
INTERNAL: Final = 6

_LINE: Final = "lucille-host proxy: "
INTERNAL_LINE: Final = _LINE + "internal error"

# IPv4 networks refused (decisions/0003, "First use", step 2). RFC 6598's shared address space for
# carrier-grade NAT is built from its integer value, never from text.
_V4_REFUSED: Final = (
    *(
        ipaddress.IPv4Network(text)
        for text in (
            "0.0.0.0/8",
            "10.0.0.0/8",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.0.0.0/24",
            "192.0.2.0/24",
            "192.88.99.0/24",
            "192.168.0.0/16",
            "198.18.0.0/15",
            "198.51.100.0/24",
            "203.0.113.0/24",
            "224.0.0.0/4",
            "240.0.0.0/4",
        )
    ),
    ipaddress.IPv4Network((0x64400000, 10)),
)
# IPv6: only this network is allowed, less the ones after it.
_V6_ALLOWED: Final = ipaddress.IPv6Network("2000::/3")
_V6_REFUSED: Final = tuple(
    ipaddress.IPv6Network(text) for text in ("2001::/23", "2001:db8::/32", "3fff::/20", "2002::/16")
)


class _Stop(Exception):
    """A refusal or a failure the proxy reports with one fixed line and an exit code."""

    def __init__(self, code: int, line: str, body: bytes = b"", head: str = "") -> None:
        super().__init__(line)
        self.code = code
        self.line = line
        self.body = body
        self.head = head


@dataclass(frozen=True)
class Reply:
    """What the child writes: standard output, standard error and its exit code."""

    code: int
    stdout: bytes
    stderr: bytes


# --- the arguments --------------------------------------------------------------------------


def _arguments(argv: Sequence[str]) -> tuple[str, str, str, str]:
    """(state folder, profile, credential, path), by position. Anything else is internal."""
    if len(argv) != 7 or argv[0] != "--state" or argv[2] != "--profile" or argv[4] != "--":
        raise _Stop(INTERNAL, INTERNAL_LINE)
    state, profile, credential, path = argv[1], argv[3], argv[5], argv[6]
    from lucille_host import ops, paths

    if not os.path.isabs(state) or not paths.valid_profile(profile):
        raise _Stop(INTERNAL, INTERNAL_LINE)
    operation = ops.OPERATIONS["github.read"]
    if not ops.args_valid(operation, {"credential": credential, "path": path}):
        raise _Stop(INTERNAL, INTERNAL_LINE)
    return state, profile, credential, path


# --- the credential -------------------------------------------------------------------------

# (state folder, profile, credential name) -> (bound hosts, value), or None when there is none.
CredentialReader = Callable[[str, str, str], "tuple[tuple[str, ...], str] | None"]


def _read_credential(state: str, profile: str, name: str) -> tuple[tuple[str, ...], str] | None:
    from lucille_host import credentials

    found = credentials.CredentialStore(Path(state), profile).for_use(SERVICE, name)
    if found is None:
        return None
    saved, value = found
    return saved.hosts, value


def _token(read: CredentialReader, state: str, profile: str, name: str) -> str:
    try:
        found = read(state, profile, name)
    except Exception:  # noqa: BLE001 - a keyring's error may hold any credential's value
        raise _Stop(CREDENTIAL, _LINE + "the keyring did not give the credential") from None
    if found is None:
        raise _Stop(CREDENTIAL, _LINE + "no such credential")
    hosts, value = found
    if HOST not in hosts:
        raise _Stop(CREDENTIAL, _LINE + f"the credential is not bound to {HOST}")
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise _Stop(CREDENTIAL, _LINE + "the credential is not a GitHub token")
    return value


# --- the address ----------------------------------------------------------------------------


def address_allowed(text: str) -> bool:
    """Whether the proxy may connect to this address (decisions/0003, "First use", step 2)."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in network for network in _V4_REFUSED)
    if address.scope_id:
        return False
    return address in _V6_ALLOWED and not any(address in network for network in _V6_REFUSED)


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    return found


def _vetted(resolve: Callable[[str, int], list[str]]) -> list[str]:
    try:
        addresses = resolve(HOST, PORT)
    except (OSError, UnicodeError):
        raise _Stop(ADDRESS, _LINE + f"{HOST} did not resolve") from None
    if not addresses:
        raise _Stop(ADDRESS, _LINE + f"{HOST} did not resolve")
    if not all(address_allowed(address) for address in addresses):
        raise _Stop(ADDRESS, _LINE + f"{HOST} resolved to an address that is not allowed")
    return addresses


def _connect(address: str) -> Any:
    """A TLS connection to ``address`` for :data:`HOST`, checked against certifi's bundle."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((address, PORT), timeout=NET_TIMEOUT_S)
    try:
        return context.wrap_socket(raw, server_hostname=HOST)
    except BaseException:
        raw.close()
        raise


def _connected(connect: Callable[[str], Any], addresses: list[str]) -> Any:
    for address in addresses:
        try:
            return connect(address)
        except (OSError, ssl.SSLError):
            continue
    raise _Stop(ADDRESS, _LINE + f"could not connect to {HOST}")


# --- the response ---------------------------------------------------------------------------


class _HeadersTooLarge(Exception):
    pass


class _Reader:
    """The response's file, counting what the status line and headers take."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.head_bytes = 0
        self.in_head = True

    def readline(self, limit: int = -1) -> bytes:
        line: bytes = self._raw.readline(limit)
        if self.in_head:
            self.head_bytes += len(line)
            if self.head_bytes > HEADER_CAP:
                raise _HeadersTooLarge
        return line

    def read(self, amount: int | None = None) -> bytes:
        data: bytes = self._raw.read() if amount is None else self._raw.read(amount)
        return data

    def readinto(self, buffer: Any) -> int:
        count: int = self._raw.readinto(buffer)
        return count

    def peek(self, size: int = 0) -> bytes:
        data: bytes = self._raw.peek(size)
        return data

    def read1(self, size: int = -1) -> bytes:
        data: bytes = self._raw.read1(size)
        return data

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._raw.close()


class _Socket:
    """What ``http.client.HTTPResponse`` reads from: the connection, through :class:`_Reader`."""

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self.reader: _Reader | None = None

    def makefile(self, mode: str) -> _Reader:
        self.reader = _Reader(self._sock.makefile(mode))
        return self.reader


def _request(path: str, token: str) -> bytes:
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}",
        f"Authorization: Bearer {token}",
        "Accept: application/vnd.github+json",
        "Accept-Encoding: identity",
        "X-GitHub-Api-Version: 2022-11-28",
        f"User-Agent: lucille-host/{__version__}",
        "Connection: close",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _head(status: int, link: str | None) -> str:
    return f"HTTP {status}" + ("" if link is None else f"\nLink: {link}")


def _exchange(sock: Any, path: str, token: str) -> tuple[int, bytes, str]:
    """(exit code, body, head) for one request on ``sock``."""
    try:
        sock.sendall(_request(path, token))
        holder = _Socket(sock)
        response = http.client.HTTPResponse(holder, method="GET")  # type: ignore[arg-type]
        try:
            response.begin()
        except _HeadersTooLarge:
            raise _Stop(CAP, _LINE + "the response's headers are too large") from None
        except http.client.LineTooLong:
            raise _Stop(CAP, _LINE + "the response's headers are too large") from None
        except http.client.HTTPException as exc:
            if "more than" in str(exc) and "headers" in str(exc):
                raise _Stop(CAP, _LINE + "the response has too many headers") from None
            raise _Stop(ADDRESS, _LINE + "the connection broke before the response") from None
        assert holder.reader is not None
        holder.reader.in_head = False
        status = response.status
        links = response.headers.get_all("Link") or []
        head = _head(status, ", ".join(links) if links else None)
        encodings = response.headers.get_all("Content-Encoding") or []
        if any(value.strip().lower() not in ("", "identity") for value in encodings):
            raise _Stop(CAP, _LINE + "the response is compressed; not read", head=head)
        # http.client undoes a plain "chunked" alone: any other transfer coding ("gzip, chunked"
        # among them) would reach the scrub as bytes it cannot see into, so it is not read.
        transfer = response.headers.get_all("Transfer-Encoding") or []
        # Decided on what http.client will do, not on the header's text alone: it must be one
        # header, and http.client must be reading it as chunked.
        if transfer and (len(transfer) != 1 or response.chunked is not True):
            raise _Stop(
                CAP,
                _LINE + "the response's transfer encoding is not chunked alone; not read",
                head=head,
            )
        body = bytearray()
        while len(body) <= BODY_CAP:
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            body += chunk
        if len(body) > BODY_CAP:
            raise _Stop(
                CAP,
                _LINE + "the response is larger than 1 MiB; cut there",
                body=bytes(body[:BODY_CAP]),
                head=head,
            )
        if response.length:
            # The connection ended before the Content-Length it promised: http.client's read
            # with a size returns what came, without saying so.
            raise _Stop(ADDRESS, _LINE + "the connection broke before the response")
    except (OSError, http.client.HTTPException, ValueError):
        raise _Stop(ADDRESS, _LINE + "the connection broke before the response") from None
    return (OK if 200 <= status <= 299 else STATUS), bytes(body), head


# --- the scrub ------------------------------------------------------------------------------


def _forms(token: str) -> list[bytes]:
    """The raw token, and its base64 at each of the three byte alignments: the four-character
    groups that depend only on the token."""
    raw = token.encode("ascii")
    forms = [raw]
    for before in range(3):
        start = (3 - before) % 3
        rest = raw[start:]
        whole = len(rest) // 3 * 3
        if whole:
            forms.append(binascii.b2a_base64(rest[:whole], newline=False))
    return forms


def scrub(data: bytes, token: str, *, cut: bool = False) -> bytes:
    """``data`` with every form of the token replaced. With ``cut``, a tail that is a beginning
    of the raw token is dropped as well."""
    mark = TOKEN_MARK.encode("ascii")
    for form in _forms(token):
        data = data.replace(form, mark)
    if cut:
        raw = token.encode("ascii")
        for size in range(min(len(raw) - 1, len(data)), 0, -1):
            if data.endswith(raw[:size]):
                data = data[:-size]
                break
    return data


# --- the whole ------------------------------------------------------------------------------


def main(
    argv: Sequence[str],
    *,
    read_credential: CredentialReader | None = None,
    resolve: Callable[[str, int], list[str]] | None = None,
    connect: Callable[[str], Any] | None = None,
) -> Reply:
    """What the child writes for ``argv``; the network and the keyring can be given (tests)."""
    token: str | None = None
    try:
        state, profile, name, path = _arguments(argv)
        token = _token(read_credential or _read_credential, state, profile, name)
        addresses = _vetted(resolve or _resolve)
        sock = _connected(connect or _connect, addresses)
        try:
            code, body, head = _exchange(sock, path, token)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return Reply(code, scrub(body, token), scrub(head.encode("utf-8") + b"\n", token))
    except _Stop as stop:
        err = (stop.head + "\n" if stop.head else "") + stop.line + "\n"
        if token is None:
            return Reply(stop.code, stop.body, err.encode("utf-8"))
        return Reply(
            stop.code, scrub(stop.body, token, cut=True), scrub(err.encode("utf-8"), token)
        )


def _write(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        except OSError:
            return
        view = view[written:]


def _set_aside_streams() -> tuple[int, int]:
    """Copies of standard output and standard error for the reply alone, with descriptors 1 and 2
    pointed at the null device from here on. So nothing a library writes there, through
    ``sys.stdout``, ``sys.stderr`` (line-buffered under ``-I``) or from C, reaches the reply
    unscrubbed: the child writes only what :func:`main` returns."""
    out = os.dup(1)
    err = os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
    finally:
        os.close(null)
    return out, err


def run() -> NoReturn:
    """The child's entry point: the streams set aside, the top-level handler, one write of each
    stream, ``os._exit``."""
    try:
        out, err = _set_aside_streams()
    except OSError:
        os._exit(INTERNAL)
    try:
        reply = main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 - its text could hold a header's value: never written
        reply = Reply(INTERNAL, b"", (INTERNAL_LINE + "\n").encode("ascii"))
    _write(out, reply.stdout)
    _write(err, reply.stderr)
    os._exit(reply.code)


if __name__ == "__main__":
    run()
