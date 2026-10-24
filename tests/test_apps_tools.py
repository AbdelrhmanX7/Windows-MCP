"""Apps / AppSkill tools over an in-process FastMCP client, plus the Snapshot hint."""

import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.apps.models import AppRecord
from windows_mcp.apps.service import AppInventory
from windows_mcp.skills import generate as gen
from windows_mcp.skills.store import SkillStore
from windows_mcp.tools import apps as apps_tools
from windows_mcp.tools.control_policy import requires_control

SPOTIFY_SKILL = """---
name: spotify
title: Spotify
description: Music streaming.
source: builtin
match:
  process: [spotify.exe]
uri_schemes: [spotify]
---

# Spotify

## What it's for

Music.

## Recipes

1. Search: PowerShell Start-Process "spotify:search:x"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    (builtin / "spotify.md").write_text(SPOTIFY_SKILL, encoding="utf-8")
    records = [
        AppRecord(
            name="Paint", exe="C:\\Windows\\system32\\mspaint.exe", process_names=["mspaint.exe"]
        ),
        AppRecord(
            name="Spotify",
            aumid="Spotify",
            process_names=["spotify.exe"],
            uri_schemes=["spotify"],
            publisher="Spotify AB",
        ),
    ]
    gathers = []

    def gather():
        gathers.append(1)
        return list(records), {"spotify": None}

    inventory = AppInventory(gather=gather, cache_dir=None, ttl=3600)
    store = SkillStore(user_dir=user, builtin=builtin)
    monkeypatch.setattr(apps_tools, "_inventory", inventory)
    monkeypatch.setattr(apps_tools, "_store", store)
    mcp = FastMCP("test")
    apps_tools.register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    return SimpleNamespace(mcp=mcp, user=user, store=store, gathers=gathers)


async def _call(mcp, name, args):
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    return result.content[0].text


async def test_apps_list_marks_skills(env):
    text = await _call(env.mcp, "Apps", {"mode": "list"})
    lines = text.splitlines()
    assert lines[0] == "2 of 2 installed apps:"
    assert lines[1] == "Paint [paint] | skill=- | proc=mspaint.exe"
    assert (
        lines[2] == "Spotify [spotify] | skill=spotify(builtin) | uri=spotify: | proc=spotify.exe"
    )


async def test_apps_list_query_and_limit(env):
    text = await _call(env.mcp, "Apps", {"mode": "list", "query": "spot", "limit": 1})
    assert text.splitlines()[0] == '1 of 2 installed apps matching "spot":'
    empty = await _call(env.mcp, "Apps", {"mode": "list", "query": "zzzqqq"})
    assert empty.startswith("0 of 2")


async def test_apps_info(env):
    payload = json.loads(await _call(env.mcp, "Apps", {"mode": "info", "query": "spotify"}))
    assert payload["app"]["aumid"] == "Spotify"
    assert payload["skill"]["name"] == "spotify"
    assert payload["skill"]["sections"] == ["What it's for", "Recipes"]
    paint = json.loads(await _call(env.mcp, "Apps", {"mode": "info", "query": "paint"}))
    assert paint["skill"] is None
    assert "generate=true" in paint["hint"]


async def test_apps_refresh_regathers(env):
    await _call(env.mcp, "Apps", {"mode": "list"})
    text = await _call(env.mcp, "Apps", {"mode": "refresh"})
    assert "2 installed apps, 1 skills loaded, 1 apps have a skill" in text
    assert len(env.gathers) == 2


async def test_app_skill_returns_markdown(env):
    text = await _call(env.mcp, "AppSkill", {"name": "Spotify"})
    assert text.startswith("<!-- skill: spotify | source: builtin")
    assert "## Recipes" in text
    by_process = await _call(env.mcp, "AppSkill", {"name": "spotify.exe"})
    assert "name: spotify" in by_process


async def test_app_skill_missing_suggests_generate(env):
    text = await _call(env.mcp, "AppSkill", {"name": "paint"})
    assert "generate=true" in text
    unknown = await _call(env.mcp, "AppSkill", {"name": "zzzqqq"})
    assert "No skill and no installed app" in unknown


async def test_app_skill_rejects_non_boolean_flag(env):
    async with Client(env.mcp) as client:
        result = await client.call_tool(
            "AppSkill", {"name": "paint", "generate": "yes"}, raise_on_error=False
        )
    assert result.is_error


@pytest.fixture
def fake_ui(monkeypatch):
    calls = SimpleNamespace(closed=[], walked=[])
    monkeypatch.setattr(gen, "ensure_window", lambda app, timeout=10: (4242, True))
    monkeypatch.setattr(
        gen,
        "read_win32_menu",
        lambda hwnd: (
            {"File": [gen.UIElementInfo("New", "MenuItemControl", "Ctrl+N", "N", "File > New")]},
            {"File": "F"},
        ),
    )

    def walk(hwnd):
        calls.walked.append(hwnd)
        info = gen.AppUIInfo(visited=10)
        info.elements = [gen.UIElementInfo("Brushes", "SplitButtonControl", "", "B")]
        return info

    monkeypatch.setattr(gen, "walk_uia", walk)
    monkeypatch.setattr(gen, "close_window", calls.closed.append)
    return calls


async def test_app_skill_generate_writes_user_dir(env, fake_ui):
    text = await _call(env.mcp, "AppSkill", {"name": "paint", "generate": True})
    assert "source: generated" in text
    assert "| File > New | `Ctrl+N` |" in text
    assert (env.user / "paint.md").is_file()
    assert fake_ui.walked == [4242]
    assert fake_ui.closed == [4242]  # we launched it, so we close it

    # A second call returns the stored skill instead of walking again.
    again = await _call(env.mcp, "AppSkill", {"name": "paint", "generate": True})
    assert "source: generated" in again
    assert fake_ui.walked == [4242]

    regenerated = await _call(env.mcp, "AppSkill", {"name": "paint", "regenerate": "true"})
    assert "version: 2" in regenerated
    assert fake_ui.walked == [4242, 4242]


async def test_generate_does_not_replace_builtin(env, fake_ui):
    text = await _call(env.mcp, "AppSkill", {"name": "spotify", "regenerate": True})
    assert "source: builtin" in text
    assert "regenerate ignored" in text
    assert fake_ui.walked == []
    assert not (env.user / "spotify.md").exists()


async def test_generate_reports_launch_failure(env, monkeypatch):
    def fail(app, timeout=10):
        raise gen.GenerationError("No window for Paint appeared within 10s")

    monkeypatch.setattr(gen, "ensure_window", fail)
    text = await _call(env.mcp, "AppSkill", {"name": "paint", "generate": True})
    assert text == "Skill generation failed for Paint: No window for Paint appeared within 10s"


async def test_skill_resources(env):
    async with Client(env.mcp) as client:
        doc = await client.read_resource("skill://spotify")
        index = await client.read_resource("skill://index")
    assert "name: spotify" in doc[0].text
    assert index[0].text.startswith("spotify | builtin | Spotify | Music streaming.")


def test_foreground_skill_hint(env, monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "Spotify.exe"))
    window = SimpleNamespace(process_id=1, name="Spotify Premium")
    assert apps_tools.foreground_skill_hint(window) == (
        'Skill available for Spotify: call AppSkill(name="spotify")'
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "other.exe"))
    assert apps_tools.foreground_skill_hint(window) is None
    assert apps_tools.foreground_skill_hint(None) is None


def test_snapshot_appends_hint(env, monkeypatch):
    from windows_mcp.tools import snapshot as snapshot_module

    state = SimpleNamespace(active_window=SimpleNamespace(process_id=1, name="Spotify"))
    monkeypatch.setattr(
        snapshot_module, "capture_desktop_state", lambda *a, **k: {"desktop_state": state}
    )
    monkeypatch.setattr(snapshot_module, "build_snapshot_response", lambda *a, **k: ["State\n"])
    monkeypatch.setattr(
        snapshot_module, "foreground_skill_hint", lambda window: "Skill available for Spotify"
    )

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name, **kwargs):
            def decorator(func):
                self.tools[name] = func
                return func

            return decorator

    import asyncio

    mcp = FakeMCP()
    snapshot_module.register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    result = asyncio.run(mcp.tools["Snapshot"]())
    assert result == ["State\n\nSkill available for Spotify\n"]


@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
def test_control_policy_classification(policy):
    assert requires_control(policy, "Apps", {"mode": "list"}) is (policy == "strict")
    assert requires_control(policy, "AppSkill", {"name": "x"}) is (policy != "relaxed")
