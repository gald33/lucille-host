"""The service templates: what this release can do with a credential of each service.

decisions/0003, "Hardening", rule 4. A credential is used only through the operations of its
service's reviewed template, each sent only to the template's hosts, and only in the form the
template says its value takes. A service with no template here cannot be used: nothing reads a
credential saved under it. The template also says what ``credential.ask`` may ask the user for
(decisions/0003, "Entry"): only a credential of a service listed here, bound to the template's own
hosts, never to hosts a request names.

Rule 1 of "Hardening", risk tiers, in the form this release has it: every operation a template
lists reads and changes nothing, so it runs on its own once the user enables it. An operation that
changes something would need a prompt on this computer before each use, which this release does
not have; so a template lists reads only, and :class:`Service` refuses one that lists anything
else (``changes``) when it is defined.

This module holds data and regular expressions only. Any process may import it: the command line,
``run`` and each child.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = [
    "NAME",
    "GITHUB_TOKEN",
    "GMAIL_ADDRESS_MAX",
    "GMAIL_VALUE",
    "SERVICES",
    "Service",
    "value_fits",
]

# A service's or a credential's name, the rule Lucille checks too: ``gmail``, ``default``. Matched
# whole (``fullmatch``): ``$`` would also match before a newline at the end.
NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# A GitHub token: a fine-grained one, or a classic one (decisions/0003, "First use"). Matched whole.
GITHUB_TOKEN: Final = re.compile(
    r"github_pat_[A-Za-z0-9_]{22,255}|ghp_[A-Za-z0-9]{36,251}", re.ASCII
)
# A Gmail credential: the account's address, one space, the app password's 16 lowercase letters
# (decisions/0003, "Second use"). Matched whole, and its address is at most GMAIL_ADDRESS_MAX.
GMAIL_VALUE: Final = re.compile(
    r"^[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}"
    r"[A-Za-z]{2,63} [a-z]{16}$",
    re.ASCII,
)
GMAIL_ADDRESS_MAX: Final = 254


@dataclass(frozen=True)
class Service:
    """One service's template."""

    # The service's name, as a credential is saved under it: ``github``.
    name: str
    # What a person calls it: ``GitHub``.
    title: str
    # The only hosts a credential of this service is sent to, and the ones an ask binds it to.
    hosts: tuple[str, ...]
    # The catalog's operations that use a credential of this service. Each reads only.
    reads: tuple[str, ...]
    # The form its value must take, matched whole; a value in another form is never sent.
    value: re.Pattern[str]
    # What the value is, in a few words, for a prompt and a warning.
    value_words: str
    # Where the user makes one, the narrowest the service offers (rule 2 of "Hardening").
    make_at: str
    # Operations that change something. Always empty in this release (rule 1).
    changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.changes:
            raise ValueError(
                f"{self.name}: an operation that changes something needs a prompt on this "
                "computer before each use, which this release does not have"
            )


SERVICES: Final[Mapping[str, Service]] = MappingProxyType(
    {
        "github": Service(
            name="github",
            title="GitHub",
            hosts=("api.github.com",),
            reads=("github.read",),
            value=GITHUB_TOKEN,
            value_words=(
                "a GitHub token, starting github_pat_ or ghp_; best a read-only, fine-grained one"
            ),
            make_at="https://github.com/settings/personal-access-tokens",
        ),
        "gmail": Service(
            name="gmail",
            title="Gmail",
            hosts=("imap.gmail.com",),
            reads=("gmail.check", "gmail.mailboxes", "gmail.search", "gmail.read"),
            value=GMAIL_VALUE,
            value_words=(
                "the account's address, one space, and a Google app password's 16 letters "
                "without spaces"
            ),
            make_at="https://myaccount.google.com/apppasswords",
        ),
    }
)


def value_fits(service: Service, value: str) -> bool:
    """Whether ``value`` is in the form ``service``'s template says, as its operation's child
    checks it before it sends anything."""
    if service.value.fullmatch(value) is None:
        return False
    if service is SERVICES["gmail"]:
        return len(value.split(" ")[0]) <= GMAIL_ADDRESS_MAX
    return True
