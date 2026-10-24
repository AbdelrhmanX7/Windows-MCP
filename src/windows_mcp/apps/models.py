"""Data model for one installed application."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_VENDOR_PREFIXES = ("microsoft ", "google ", "mozilla ", "adobe ", "windows ")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Return a lowercase, dash-separated identifier for *value*."""
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def normalize_name(value: str) -> str:
    """Normalize a display name for joining records from different sources."""
    value = value.lower()
    value = re.sub(r"\(.*?\)", " ", value)  # "(64-bit)", "(x64 en-US)"
    value = re.sub(r"\b\d+(\.\d+)+\b", " ", value)  # embedded version numbers
    return " ".join(_SLUG_RE.sub(" ", value).split())


@dataclass
class AppRecord:
    """One installed application, merged from every inventory source.

    Attributes:
        name: Display name (Start-menu name when known).
        aliases: Extra lowercase names that should resolve to this app.
        aumid: AppUserModelID usable with ``shell:AppsFolder\\<aumid>``.
        exe: Absolute path of the main executable, when known.
        process_names: Lowercase executable basenames of the app's processes.
        publisher: Publisher string from the registry or AppX package.
        version: Version string.
        uri_schemes: URI schemes (without the trailing colon) the app handles.
        sources: Which inventory sources contributed (startmenu, uninstall, appx, protocol).
        install_location: Installation directory, when known.
        package_family: AppX PackageFamilyName for packaged apps.
    """

    name: str
    aliases: list[str] = field(default_factory=list)
    aumid: str | None = None
    exe: str | None = None
    process_names: list[str] = field(default_factory=list)
    publisher: str | None = None
    version: str | None = None
    uri_schemes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    install_location: str | None = None
    package_family: str | None = None

    @property
    def id(self) -> str:
        return slugify(self.name)

    def add_source(self, source: str) -> None:
        if source not in self.sources:
            self.sources.append(source)

    def add_alias(self, alias: str | None) -> None:
        if not alias:
            return
        alias = alias.strip().lower()
        if alias and alias != self.name.lower() and alias not in self.aliases:
            self.aliases.append(alias)

    def add_process(self, name: str | None) -> None:
        if not name:
            return
        name = name.strip().lower()
        if name and name not in self.process_names:
            self.process_names.append(name)

    def add_scheme(self, scheme: str | None) -> None:
        if not scheme:
            return
        scheme = scheme.strip().lower().rstrip(":")
        if scheme.startswith("explorer.") or ".assoc" in scheme:
            return  # shell verbs registered as protocols, not useful deep links
        if scheme and scheme not in self.uri_schemes:
            self.uri_schemes.append(scheme)

    def all_names(self) -> list[str]:
        """Return the display name plus aliases, lowercase."""
        return [self.name.lower(), *self.aliases]

    def finalize(self) -> None:
        """Derive aliases from the name and executable, then sort list fields."""
        lowered = self.name.lower()
        for prefix in _VENDOR_PREFIXES:
            if lowered.startswith(prefix) and len(lowered) > len(prefix) + 2:
                self.add_alias(lowered[len(prefix) :])
        for process in self.process_names:
            self.add_alias(process.removesuffix(".exe"))
        self.uri_schemes.sort()

    def to_dict(self) -> dict:
        return asdict(self) | {"id": self.id}

    @classmethod
    def from_dict(cls, data: dict) -> AppRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
