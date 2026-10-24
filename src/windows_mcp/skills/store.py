"""Load skills from the bundled and user folders and resolve them by name or app."""

from __future__ import annotations

import logging
import os
import threading
from importlib import resources
from pathlib import Path

from windows_mcp.apps.models import AppRecord, slugify
from windows_mcp.skills.models import Skill

logger = logging.getLogger(__name__)

SKILLS_DIR_ENV = "WINDOWS_MCP_SKILLS_DIR"
# Process names that host other apps' windows; matching them by process would be wrong.
_HOST_PROCESSES = {"applicationframehost.exe"}


def default_user_dir() -> Path:
    """Return the user skill folder (``WINDOWS_MCP_SKILLS_DIR`` or the platformdirs data dir)."""
    override = os.getenv(SKILLS_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from platformdirs import user_data_dir

    return Path(user_data_dir("windows-mcp", appauthor=False)) / "skills"


def builtin_dir() -> Path:
    return Path(str(resources.files("windows_mcp.skills") / "builtin"))


class SkillStore:
    """Skill index over the builtin folder and one user folder.

    Precedence when several skills fit the same app or share a name:
    builtin > user > generated.

    Args:
        user_dir: User skill folder; defaults to :func:`default_user_dir`.
        builtin: Bundled skill folder; defaults to the package's ``builtin/``.
    """

    def __init__(self, user_dir: Path | None = None, builtin: Path | None = None) -> None:
        self._user_dir = user_dir
        self._builtin_dir = builtin
        self._lock = threading.Lock()
        self._skills: list[Skill] | None = None
        self._by_process: dict[str, list[Skill]] = {}

    @property
    def user_dir(self) -> Path:
        return self._user_dir if self._user_dir is not None else default_user_dir()

    @property
    def builtin_dir(self) -> Path:
        return self._builtin_dir if self._builtin_dir is not None else builtin_dir()

    # -- loading -------------------------------------------------------------

    def reload(self) -> list[Skill]:
        skills: list[Skill] = []
        skills.extend(self._load_dir(self.builtin_dir, forced_source="builtin"))
        skills.extend(self._load_dir(self.user_dir, forced_source=None))
        skills.sort(key=lambda s: (s.rank, s.name))
        by_process: dict[str, list[Skill]] = {}
        for skill in skills:
            for process in skill.processes:
                by_process.setdefault(process, []).append(skill)
        with self._lock:
            self._skills, self._by_process = skills, by_process
        return skills

    @staticmethod
    def _load_dir(folder: Path, forced_source: str | None) -> list[Skill]:
        if not folder.is_dir():
            return []
        loaded = []
        for path in sorted(folder.glob("*.md")):
            try:
                skill = Skill.parse(
                    path.read_text(encoding="utf-8"), path=path, source=forced_source
                )
            except Exception as exc:
                logger.warning("Skipping unreadable skill %s: %s", path, exc)
                continue
            if forced_source is None and skill.source == "builtin":
                skill.source = "user"  # only the bundled folder may claim builtin
            loaded.append(skill)
        return loaded

    def skills(self) -> list[Skill]:
        if self._skills is None:
            return self.reload()
        return self._skills

    # -- lookups -------------------------------------------------------------

    def get(self, name: str, source: str | None = None) -> Skill | None:
        """Return the highest-precedence skill called *name* (optionally from one source)."""
        slug = slugify(name)
        for skill in self.skills():
            if skill.name == slug and (source is None or skill.source == source):
                return skill
        return None

    def for_app(self, app: AppRecord) -> Skill | None:
        """Best skill for an inventory record (builtin > user > generated)."""
        for skill in self.skills():
            if skill.matches_app(app):
                return skill
        return self.get(app.id)

    def for_window(self, process_name: str | None, title: str | None = None) -> Skill | None:
        """Cheap in-memory lookup for the foreground window (used by Snapshot).

        UWP frames (``ApplicationFrameHost.exe``) are matched by window title, and
        ``explorer.exe`` only counts when it owns a real window rather than the desktop.
        """
        if self._skills is None:
            self.skills()
        process = (process_name or "").lower()
        title = (title or "").strip()
        if process == "explorer.exe" and title in ("", "Program Manager"):
            return None
        if process and process not in _HOST_PROCESSES:
            candidates = self._by_process.get(process)
            if candidates:
                return candidates[0]
        if title:
            for skill in self._skills or []:
                if skill.matches_title(title):
                    return skill
        return None

    def find(self, query: str, app: AppRecord | None = None) -> Skill | None:
        """Resolve *query*: skill name, then the resolved app, then title patterns, then fuzzy."""
        exact = self.get(query)
        if exact is not None:
            return exact
        if app is not None:
            by_app = self.for_app(app)
            if by_app is not None:
                return by_app
        q = query.strip().lower()
        for skill in self.skills():
            if skill.title.lower() == q or skill.matches_title(q) or skill.matches_process(q):
                return skill
            if skill.matches_process(q + ".exe"):
                return skill
        from thefuzz import fuzz

        best, best_score = None, 0
        for skill in self.skills():
            score = max(fuzz.WRatio(q, skill.name), fuzz.WRatio(q, skill.title.lower()))
            if score > best_score:
                best, best_score = skill, score
        return best if best_score >= 85 else None


_default_store: SkillStore | None = None
_default_lock = threading.Lock()


def get_store() -> SkillStore:
    """Return the process-wide skill store."""
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = SkillStore()
        return _default_store
