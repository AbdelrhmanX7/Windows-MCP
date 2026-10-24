"""Apps and AppSkill tools: installed-app inventory and per-app skills."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.apps.models import AppRecord
from windows_mcp.apps.service import AppInventory, get_inventory
from windows_mcp.desktop.utils import as_bool
from windows_mcp.infrastructure import with_analytics
from windows_mcp.infrastructure.thread_affinity import run_uia_affine
from windows_mcp.skills import generate as gen
from windows_mcp.skills.models import Skill
from windows_mcp.skills.store import SkillStore, get_store

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 60

# Injection points for tests; None means the process-wide singletons.
_inventory: AppInventory | None = None
_store: SkillStore | None = None


def inventory() -> AppInventory:
    return _inventory if _inventory is not None else get_inventory()


def store() -> SkillStore:
    return _store if _store is not None else get_store()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _app_line(app: AppRecord, skill: Skill | None) -> str:
    parts = [f"{app.name} [{app.id}]"]
    parts.append(f"skill={skill.name}({skill.source})" if skill else "skill=-")
    if app.uri_schemes:
        schemes = ",".join(f"{s}:" for s in app.uri_schemes[:3])
        if len(app.uri_schemes) > 3:
            schemes += ",..."
        parts.append(f"uri={schemes}")
    if app.process_names:
        parts.append(f"proc={app.process_names[0]}")
    return " | ".join(parts)


def _skill_sections(skill: Skill) -> list[str]:
    return [line[3:].strip() for line in skill.body.splitlines() if line.startswith("## ")]


def _skill_header(skill: Skill, note: str | None = None) -> str:
    header = f"<!-- skill: {skill.name} | source: {skill.source} | version: {skill.version}"
    if skill.path:
        header += f" | path: {skill.path}"
    header += " -->"
    if note:
        header += f"\n<!-- {note} -->"
    return header


def format_skill(skill: Skill, note: str | None = None) -> str:
    """Return the skill as markdown with a one-line provenance header."""
    return f"{_skill_header(skill, note)}\n{skill.render()}"


# ---------------------------------------------------------------------------
# Snapshot integration
# ---------------------------------------------------------------------------


def foreground_skill_hint(active_window) -> str | None:
    """One line for Snapshot when the foreground window's app has a skill.

    Pure in-memory lookup (process name + window title against the loaded skill
    index); returns ``None`` on any failure so it can never break a Snapshot.
    """
    if active_window is None:
        return None
    try:
        import psutil

        process_name = psutil.Process(active_window.process_id).name()
    except Exception:
        process_name = ""
    try:
        title = getattr(active_window, "name", "")
        skill = store().for_window(process_name, title if isinstance(title, str) else "")
    except Exception:
        logger.debug("Skill hint lookup failed", exc_info=True)
        return None
    if skill is None:
        return None
    return f'Skill available for {skill.title}: call AppSkill(name="{skill.name}")'


# ---------------------------------------------------------------------------
# Tool implementations (plain functions so tests can call them directly)
# ---------------------------------------------------------------------------


def apps_list(query: str | None, limit: int) -> str:
    apps = inventory().search(query, limit=limit if limit > 0 else None)
    skills = store()
    total = len(inventory().apps())
    header = f"{len(apps)} of {total} installed apps"
    header += f' matching "{query}"' if query else ""
    if not apps:
        return header + '. Try a shorter query or Apps(mode="refresh").'
    lines = [header + ":"] + [_app_line(app, skills.for_app(app)) for app in apps]
    return "\n".join(lines)


def apps_info(query: str) -> str:
    app = inventory().find(query)
    if app is None:
        return f'No installed app matches "{query}". Use Apps(mode="list", query=...) to search.'
    skill = store().for_app(app)
    payload: dict = {"app": app.to_dict()}
    if skill:
        payload["skill"] = {
            "name": skill.name,
            "source": skill.source,
            "description": skill.summary(),
            "sections": _skill_sections(skill),
            "fetch": f'AppSkill(name="{skill.name}")',
        }
    else:
        payload["skill"] = None
        payload["hint"] = f'No skill yet. AppSkill(name="{app.name}", generate=true) builds one.'
    return json.dumps(payload, indent=2, ensure_ascii=False)


def apps_refresh() -> str:
    started = time.perf_counter()
    apps = inventory().apps(refresh=True)
    skills = store().reload()
    elapsed = time.perf_counter() - started
    with_skill = sum(1 for app in apps if store().for_app(app))
    return (
        f"Refreshed in {elapsed:.1f}s: {len(apps)} installed apps, {len(skills)} skills loaded, "
        f"{with_skill} apps have a skill."
    )


async def app_skill(name: str, generate: bool, regenerate: bool) -> str:
    skills = store()
    skill = skills.get(name)
    app = None
    if skill is None or (regenerate and skill.source == "generated"):
        app = await asyncio.to_thread(inventory().find, name)
        if skill is None:
            skill = skills.find(name, app)

    if skill is not None and not (regenerate and skill.source == "generated"):
        note = None
        if regenerate:
            note = f"regenerate ignored: {skill.name} is a {skill.source} skill, not generated"
        return format_skill(skill, note)

    if skill is None and not (generate or regenerate):
        if app is None:
            return (
                f'No skill and no installed app match "{name}". Use Apps(mode="list", query=...).'
            )
        return (
            f'No skill for {app.name} yet. Call AppSkill(name="{app.name}", generate=true) to '
            f"build one from its UI, or use what the inventory knows:\n{apps_info(app.name)}"
        )

    if app is None:
        return f'Cannot generate a skill: no installed app matches "{name}".'

    try:
        hwnd, launched = await asyncio.to_thread(gen.ensure_window, app)
        try:
            menus, menu_keys = await asyncio.to_thread(gen.read_win32_menu, hwnd)
        except Exception:
            logger.debug("Win32 menu read failed", exc_info=True)
            menus, menu_keys = {}, {}
        info = await run_uia_affine(gen.walk_uia, hwnd)
        info.menus, info.menu_access_keys = menus, menu_keys
        if launched:
            await asyncio.to_thread(gen.close_window, hwnd)
        built = gen.build_skill(app, info)
        await asyncio.to_thread(gen.write_generated, skills, built, regenerate=regenerate)
    except gen.GenerationError as exc:
        return f"Skill generation failed for {app.name}: {exc}"
    return format_skill(built, f"generated from {app.name}'s UI ({info.visited} elements)")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Apps",
        description=(
            "Installed-app inventory. Keywords: installed apps, programs, what apps, find app, "
            "AUMID, URI scheme, deep link. mode='list' prints one line per app (name [id] | "
            "skill=<name>(source) or skill=- | uri=<schemes> | proc=<process>), filtered by an "
            "optional fuzzy query and limit. mode='info' returns the full record (aliases, "
            "AUMID, exe, process names, publisher, version, URI schemes) plus a skill summary. "
            "mode='refresh' re-reads the Start menu, AppX packages, uninstall keys and URL "
            "protocols and reloads skills. Results are cached; refresh after installing apps."
        ),
        annotations=ToolAnnotations(
            title="Apps",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Apps-Tool")
    def apps_tool(
        mode: Literal["list", "info", "refresh"] = "list",
        query: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        ctx: Context = None,
    ):
        if mode == "list":
            return apps_list(query, limit)
        if mode == "info":
            if not query:
                raise ValueError('query is required for mode="info"')
            return apps_info(query)
        if mode == "refresh":
            return apps_refresh()
        raise ValueError(f"Unknown mode: {mode}")

    @mcp.tool(
        name="AppSkill",
        description=(
            "Get the skill (how-to guide) for an app before driving it: launch commands and "
            "URI deep links, keyboard shortcuts, step-by-step recipes using these tools, and UI "
            "quirks. Call it first for any app task; it usually saves several Snapshot/Click "
            "turns. name accepts a skill name, app name, alias, or process (e.g. 'spotify', "
            "'Microsoft Word', 'code.exe'). Precedence: builtin > user > generated. Set "
            "generate=true to build a skill from the app's menus and accessibility tree when "
            "none exists (launches the app if it is not running, then closes it). Set "
            "regenerate=true to rebuild a previously generated skill. Never overwrites builtin "
            "or user skills."
        ),
        annotations=ToolAnnotations(
            title="AppSkill",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "AppSkill-Tool")
    async def app_skill_tool(
        name: str,
        generate: bool | str = False,
        regenerate: bool | str = False,
        ctx: Context = None,
    ):
        return await app_skill(
            name, as_bool(generate, "generate"), as_bool(regenerate, "regenerate")
        )

    if hasattr(mcp, "resource"):

        @mcp.resource(
            "skill://{name}",
            name="AppSkillResource",
            description="Markdown skill for one app (same content as the AppSkill tool).",
            mime_type="text/markdown",
        )
        def skill_resource(name: str) -> str:
            skill = store().get(name) or store().find(name)
            if skill is None:
                raise ValueError(f"No skill named {name!r}")
            return skill.render()

        @mcp.resource(
            "skill://index",
            name="AppSkillIndex",
            description="One line per loaded skill: name, source, title and description.",
            mime_type="text/plain",
        )
        def skill_index() -> str:
            return "\n".join(
                f"{s.name} | {s.source} | {s.title} | {s.summary()}" for s in store().skills()
            )
