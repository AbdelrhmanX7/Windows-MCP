"""Skill document model and app matching."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from windows_mcp.apps.models import AppRecord, slugify
from windows_mcp.skills import frontmatter

SOURCES = ("builtin", "user", "generated")
SOURCE_RANK = {source: rank for rank, source in enumerate(SOURCES)}
MATCH_KEYS = ("process", "aumid", "name", "exe")


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]


@dataclass
class Skill:
    """One app skill: frontmatter metadata plus a markdown body.

    Attributes:
        name: Skill id (slug), also the file stem.
        title: Human-readable app name.
        description: One-line summary (the text a semantic index should embed first).
        version: Integer revision of the skill content.
        source: ``builtin``, ``user`` or ``generated``.
        match: ``{process, aumid, name, exe}`` lists; ``aumid`` and ``name`` accept
            ``fnmatch`` wildcards and are matched case-insensitively.
        uri_schemes: URI schemes the app handles, without the trailing colon.
        body: Markdown body.
        path: File the skill was loaded from.
    """

    name: str
    title: str = ""
    description: str = ""
    version: int = 1
    source: str = "user"
    match: dict[str, list[str]] = field(default_factory=dict)
    uri_schemes: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None

    @property
    def rank(self) -> int:
        return SOURCE_RANK.get(self.source, len(SOURCES))

    @property
    def processes(self) -> list[str]:
        return [p.lower() for p in self.match.get("process", [])]

    @classmethod
    def parse(cls, text: str, *, path: Path | None = None, source: str | None = None) -> Skill:
        """Parse a skill document. *source* overrides the frontmatter (used for builtins)."""
        meta_text, body = frontmatter.split(text)
        meta = frontmatter.parse(meta_text) if meta_text else {}
        raw_match = meta.get("match") or {}
        if not isinstance(raw_match, dict):
            raise frontmatter.FrontmatterError("'match' must be a mapping")
        match = {key: _as_list(raw_match.get(key)) for key in MATCH_KEYS}
        name = str(meta.get("name") or (path.stem if path else "")).strip()
        if not name:
            raise frontmatter.FrontmatterError("skill has no name")
        version = meta.get("version", 1)
        resolved_source = source or str(meta.get("source") or "user").lower()
        if resolved_source not in SOURCE_RANK:
            resolved_source = "user"
        return cls(
            name=slugify(name),
            title=str(meta.get("title") or name),
            description=str(meta.get("description") or ""),
            version=version if isinstance(version, int) else 1,
            source=resolved_source,
            match=match,
            uri_schemes=[s.lower().rstrip(":") for s in _as_list(meta.get("uri_schemes"))],
            body=body,
            path=path,
        )

    def frontmatter_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "match": {key: self.match.get(key, []) for key in MATCH_KEYS},
            "uri_schemes": self.uri_schemes,
        }

    def render(self) -> str:
        """Serialize back to the on-disk markdown format."""
        return frontmatter.render(self.frontmatter_dict(), self.body)

    def summary(self) -> str:
        """Return the description, or the first prose line of the body."""
        if self.description:
            return self.description
        for line in self.body.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-", "|", "`")):
                return line
        return ""

    # -- matching ------------------------------------------------------------

    def matches_process(self, process_name: str) -> bool:
        return process_name.lower() in self.processes

    def matches_title(self, title: str) -> bool:
        lowered = title.lower().strip()
        return bool(lowered) and any(
            fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in self.match.get("name", [])
        )

    def matches_app(self, app: AppRecord) -> bool:
        """True when any ``match`` rule fits *app*."""
        if any(self.matches_process(p) for p in app.process_names):
            return True
        if app.aumid and any(
            fnmatch.fnmatchcase(app.aumid.lower(), pattern.lower())
            for pattern in self.match.get("aumid", [])
        ):
            return True
        if app.exe:
            exe_name = re.split(r"[\\/]", app.exe)[-1].lower()
            if exe_name in (e.lower() for e in self.match.get("exe", [])):
                return True
        return any(self.matches_title(name) for name in app.all_names()[:1])
