"""URL / host / email parsing and registrable-domain (eTLD+1) lookup. Pure Python, no network.

The Public Suffix List snapshot in ``data/public_suffix_list.dat`` includes the PRIVATE section,
so user-content hosts such as ``evil.github.io``, ``x.azurewebsites.net`` or ``y.pages.dev``
get their own registrable domain instead of collapsing onto the hosting provider's.
"""

from __future__ import annotations

import ipaddress
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from urllib.parse import unquote, urlsplit

_PSL: tuple[frozenset, frozenset, frozenset] | None = None
_PSL_LOCK = threading.Lock()

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@([^@\s<>]+)$")


def _load_psl() -> tuple[frozenset, frozenset, frozenset]:
    global _PSL
    if _PSL is None:
        with _PSL_LOCK:
            if _PSL is None:
                text = (
                    resources.files("windows_mcp.guard")
                    .joinpath("data/public_suffix_list.dat")
                    .read_text(encoding="utf-8")
                )
                rules, wild, exc = set(), set(), set()
                for line in text.splitlines():
                    if not line or line.startswith("//"):
                        continue
                    line = line.lower()
                    if line.startswith("!"):
                        exc.add(line[1:])
                    elif line.startswith("*."):
                        wild.add(line[2:])
                    else:
                        rules.add(line)
                _PSL = (frozenset(rules), frozenset(wild), frozenset(exc))
    return _PSL


def warm() -> None:
    """Load the suffix list now (a few ms) so the first real check is fast."""
    _load_psl()


def public_suffix(host: str) -> str:
    """The longest matching public suffix of *host* (Unicode, lowercase, no trailing dot)."""
    rules, wild, exc = _load_psl()
    labels = host.split(".")
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in exc:
            return ".".join(labels[i + 1 :])
        if cand in rules:
            return cand
        if i + 1 < len(labels) and ".".join(labels[i + 1 :]) in wild:
            return cand
    return labels[-1]


@dataclass(frozen=True)
class HostInfo:
    """A parsed host.

    Attributes:
        host: Unicode, lowercase host without trailing dot.
        domain: Registrable domain (eTLD+1); the host itself when it is a bare suffix or IP.
        suffix: Public suffix ("" for IPs).
        label: The registrable label (``rnicorsoft`` for ``login.rnicorsoft.com``).
        subdomains: Labels left of the registrable domain.
        is_ip: Host is an IP literal (including decimal/hex integer forms).
        idn: Host contained punycode (``xn--``) or non-ASCII labels.
    """

    host: str
    domain: str
    suffix: str
    label: str
    subdomains: tuple[str, ...] = ()
    is_ip: bool = False
    idn: bool = False


@dataclass(frozen=True)
class Target:
    """What CheckLink / the watcher hands the detector."""

    kind: str  # "url" | "domain" | "email"
    host: HostInfo | None
    scheme: str | None = None  # None when the input had no scheme (address bars hide it)
    userinfo: str = ""
    url: str = ""
    email: str = ""
    display_name: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _decode_label(label: str) -> tuple[str, bool]:
    if label.startswith("xn--"):
        try:
            return label.encode("ascii").decode("idna"), True
        except UnicodeError, ValueError:
            return label, True
    return label, not label.isascii()


def _ip_literal(host: str) -> bool:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    # Browsers accept http://3232235777/ and http://0xC0A80001/ as IPv4.
    if re.fullmatch(r"(0x[0-9a-f]+|\d+)", h):
        return True
    # Dotted forms with hex/octal parts (0x7f.0.0.1, 0177.0.0.1).
    parts = h.split(".")
    return 2 <= len(parts) <= 4 and all(re.fullmatch(r"0x[0-9a-f]+|\d+", p) for p in parts)


@lru_cache(maxsize=4096)
def parse_host(raw: str) -> HostInfo | None:
    """Parse a bare host name. Returns None for empty/invalid input."""
    host = unquote(raw or "").strip().strip(".").lower()
    if not host or " " in host:
        return None
    host = unicodedata.normalize("NFKC", host)
    # Unicode full stops (IDNA treats them as dots).
    for dot in ("。", "．", "｡"):
        host = host.replace(dot, ".")
    if _ip_literal(host):
        return HostInfo(host=host, domain=host, suffix="", label=host, is_ip=True)
    idn = False
    labels = []
    for part in host.split("."):
        if not part:
            continue
        decoded, was_idn = _decode_label(part)
        idn = idn or was_idn
        labels.append(decoded)
    if not labels:
        return None
    host = ".".join(labels)
    suffix = public_suffix(host)
    rest = host[: -len(suffix)].rstrip(".") if host != suffix else ""
    if not rest:
        # The host is itself a public suffix (e.g. "s3.amazonaws.com"): treat it as the domain.
        return HostInfo(host=host, domain=host, suffix=suffix, label=labels[0], idn=idn)
    rest_labels = rest.split(".")
    label = rest_labels[-1]
    return HostInfo(
        host=host,
        domain=f"{label}.{suffix}",
        suffix=suffix,
        label=label,
        subdomains=tuple(rest_labels[:-1]),
        idn=idn,
    )


def parse_target(raw: str) -> Target:
    """Parse a URL, bare domain or email address (``Name <a@b.c>`` and ``mailto:`` accepted)."""
    text = (raw or "").strip()
    lowered = text.lower()
    display = ""
    m = re.match(r"^\s*\"?([^\"<]*)\"?\s*<([^>]+)>\s*$", text)
    if m:
        display, text = m.group(1).strip(), m.group(2).strip()
        lowered = text.lower()
    if lowered.startswith("mailto:"):
        text = text[7:].split("?", 1)[0]
        lowered = text.lower()
    em = _EMAIL_RE.match(text)
    if em and not _SCHEME_RE.match(text) and "/" not in text:
        host = parse_host(em.group(1))
        return Target(kind="email", host=host, email=text.lower(), display_name=display)

    # Browsers treat "\" like "/" in http(s) URLs: https://evil.xyz\@microsoft.com goes to evil.xyz.
    url = text.replace("\\", "/")
    scheme = None
    if _SCHEME_RE.match(url):
        scheme = url.split(":", 1)[0].lower()
    else:
        url = "http://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
    except ValueError:
        return Target(kind="url", host=None, scheme=scheme, url=text)
    userinfo, _, hostport = netloc.rpartition("@")
    hostname = hostport
    if hostname.startswith("["):
        hostname = hostname[: hostname.find("]") + 1]
    else:
        hostname = hostname.split(":", 1)[0]
    host = parse_host(hostname)
    kind = "url" if scheme or parts.path not in ("", "/") or parts.query else "domain"
    return Target(kind=kind, host=host, scheme=scheme, userinfo=unquote(userinfo), url=text)
