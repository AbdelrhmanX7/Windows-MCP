"""Skill format, store precedence/matching, builtin files, and generation."""

from datetime import date
from pathlib import Path

import pytest

from windows_mcp.apps.models import AppRecord
from windows_mcp.skills import frontmatter
from windows_mcp.skills import generate as gen
from windows_mcp.skills.models import Skill
from windows_mcp.skills.store import SkillStore, builtin_dir


def _doc(name, source=None, process=("x.exe",), names=(), title=None, extra=""):
    lines = ["---", f"name: {name}"]
    if title:
        lines.append(f"title: {title}")
    lines.append(f"description: {name} skill")
    if source:
        lines.append(f"source: {source}")
    lines += [
        "match:",
        f"  process: [{', '.join(process)}]",
        f"  name: [{', '.join(repr(n).replace(chr(39), chr(34)) for n in names)}]",
        extra,
        "---",
        "",
        f"# {name}",
        "",
        "## What it's for",
        "",
        "Body.",
    ]
    return "\n".join(lines)


# -- frontmatter ------------------------------------------------------------


def test_frontmatter_round_trip():
    data = {
        "name": "spotify",
        "title": "Spotify: Music",
        "description": 'Plays "music", podcasts',
        "version": 3,
        "source": "builtin",
        "match": {
            "process": ["spotify.exe"],
            "aumid": ["SpotifyAB.SpotifyMusic_*", "Spotify"],
            "name": ["* - Spotify", "Spotify Premium"],
            "exe": [],
        },
        "uri_schemes": ["spotify"],
        "flag": True,
    }
    text = frontmatter.render(data, "# Body\n\ntext")
    meta, body = frontmatter.split(text)
    assert frontmatter.parse(meta) == data
    assert body == "# Body\n\ntext\n"


def test_frontmatter_backslashes_round_trip():
    data = {"match": {"aumid": ["*\\notepad.exe", "{GUID}\\x.exe"]}}
    meta, _ = frontmatter.split(frontmatter.render(data, ""))
    assert frontmatter.parse(meta) == data


def test_frontmatter_block_lists_and_comments():
    text = """# comment
name: demo
uri_schemes:
  - "ms-demo:"
  - other
match:
  process:
    - demo.exe
    - Demo2.exe
  name: [Demo]
empty:
"""
    parsed = frontmatter.parse(text)
    assert parsed["uri_schemes"] == ["ms-demo:", "other"]
    assert parsed["match"] == {"process": ["demo.exe", "Demo2.exe"], "name": ["Demo"]}
    assert parsed["empty"] == ""


def test_frontmatter_errors():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.split("---\nname: x\n")
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("  indented: first")
    assert frontmatter.split("no frontmatter") == ("", "no frontmatter")


def test_skill_parse_normalizes():
    skill = Skill.parse(
        _doc("My App", source="generated", process=("MyApp.exe",), extra="uri_schemes: [myapp:]")
    )
    assert skill.name == "my-app"
    assert skill.source == "generated"
    assert skill.processes == ["myapp.exe"]
    assert skill.uri_schemes == ["myapp"]
    assert skill.summary() == "My App skill"
    assert Skill.parse(skill.render()).frontmatter_dict() == skill.frontmatter_dict()


# -- store ------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    return builtin, user


def test_precedence_builtin_over_user_over_generated(dirs):
    builtin, user = dirs
    (user / "gen.md").write_text(_doc("shared", source="generated", process=("app.exe",)))
    (user / "mine.md").write_text(_doc("shared", source="user", process=("app.exe",)))
    store = SkillStore(user_dir=user, builtin=builtin)
    assert store.get("shared").source == "user"
    app = AppRecord(name="App", process_names=["app.exe"])
    assert store.for_app(app).source == "user"

    (builtin / "shared.md").write_text(_doc("shared", process=("app.exe",)))
    store.reload()
    assert store.get("shared").source == "builtin"
    assert store.for_app(app).source == "builtin"
    assert store.for_window("APP.EXE").source == "builtin"
    assert store.get("shared", source="generated").source == "generated"


def test_user_folder_cannot_claim_builtin(dirs):
    builtin, user = dirs
    (user / "fake.md").write_text(_doc("fake", source="builtin"))
    store = SkillStore(user_dir=user, builtin=builtin)
    assert store.get("fake").source == "user"


def test_unreadable_skill_is_skipped(dirs):
    builtin, user = dirs
    (user / "bad.md").write_text("---\nname: bad\n")
    (user / "good.md").write_text(_doc("good"))
    store = SkillStore(user_dir=user, builtin=builtin)
    assert [s.name for s in store.skills()] == ["good"]


def test_for_window_uwp_title_and_explorer_desktop(dirs):
    builtin, user = dirs
    (builtin / "calculator.md").write_text(
        _doc("calculator", process=("calculatorapp.exe",), names=("Calculator",))
    )
    (builtin / "file-explorer.md").write_text(
        _doc("file-explorer", process=("explorer.exe",), names=("File Explorer",))
    )
    store = SkillStore(user_dir=user, builtin=builtin)
    assert store.for_window("ApplicationFrameHost.exe", "Calculator").name == "calculator"
    assert store.for_window("ApplicationFrameHost.exe", "Mail") is None
    assert store.for_window("explorer.exe", "Program Manager") is None
    assert store.for_window("explorer.exe", "") is None
    assert store.for_window("explorer.exe", "Downloads").name == "file-explorer"
    assert store.for_window(None, None) is None


def test_find_by_title_process_and_fuzzy(dirs):
    builtin, user = dirs
    (builtin / "visual-studio-code.md").write_text(
        _doc("visual-studio-code", title="Visual Studio Code", process=("code.exe",))
    )
    store = SkillStore(user_dir=user, builtin=builtin)
    assert store.find("Visual Studio Code").name == "visual-studio-code"
    assert store.find("code").name == "visual-studio-code"
    assert store.find("code.exe").name == "visual-studio-code"
    assert store.find("visual studio cod").name == "visual-studio-code"
    assert store.find("photoshop") is None


def test_env_var_overrides_user_dir(monkeypatch, tmp_path):
    from windows_mcp.skills.store import default_user_dir

    monkeypatch.setenv("WINDOWS_MCP_SKILLS_DIR", str(tmp_path))
    assert default_user_dir() == tmp_path


# -- bundled skills -----------------------------------------------------------

EXPECTED_BUILTINS = {
    "file-explorer",
    "settings",
    "microsoft-edge",
    "google-chrome",
    "notepad",
    "calculator",
    "spotify",
    "microsoft-word",
    "microsoft-excel",
    "microsoft-powerpoint",
    "outlook",
    "microsoft-teams",
    "visual-studio-code",
    "discord",
    "windows-terminal",
    "paint",
    "snipping-tool",
    "task-manager",
}


def _builtin_skills():
    return [
        Skill.parse(p.read_text(encoding="utf-8"), path=p, source="builtin")
        for p in sorted(builtin_dir().glob("*.md"))
    ]


def test_all_builtin_skills_parse_and_are_complete():
    skills = _builtin_skills()
    names = {s.name for s in skills}
    assert EXPECTED_BUILTINS <= names
    for skill in skills:
        assert skill.path.stem == skill.name
        raw = frontmatter.parse(frontmatter.split(skill.path.read_text(encoding="utf-8"))[0])
        assert raw.get("source") == "builtin", skill.name
        assert skill.description, skill.name
        assert skill.match.get("process") or skill.match.get("aumid"), skill.name
        assert "## What it's for" in skill.body, skill.name
        assert "## Keyboard shortcuts" in skill.body, skill.name
        assert "## Recipes" in skill.body, skill.name
        assert all(not s.endswith(":") for s in skill.uri_schemes), skill.name


def test_builtin_skills_resolve_the_obvious_apps():
    store = SkillStore(user_dir=Path("Z:/does/not/exist"))
    cases = [
        (AppRecord(name="Notepad", process_names=["notepad.exe"]), "notepad"),
        (AppRecord(name="Google Chrome", process_names=["chrome.exe"]), "google-chrome"),
        (AppRecord(name="Spotify", process_names=["spotify.exe"]), "spotify"),
        (
            AppRecord(
                name="Settings",
                aumid="windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
                process_names=["systemsettings.exe"],
            ),
            "settings",
        ),
        (AppRecord(name="Visual Studio Code", process_names=["code.exe"]), "visual-studio-code"),
    ]
    for app, expected in cases:
        assert store.for_app(app).name == expected
    assert store.for_window("ApplicationFrameHost.exe", "Calculator").name == "calculator"


# -- generation -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("&Save\tCtrl+S", ("Save", "S", "Ctrl+S")),
        ("Save &As...\tCtrl+Shift+S", ("Save As", "A", "Ctrl+Shift+S")),
        ("Page Set&up...", ("Page Setup", "U", "")),
        ("Fish && &Chips", ("Fish & Chips", "C", "")),
    ],
)
def test_split_menu_text(text, expected):
    assert gen.split_menu_text(text) == expected


def _paint_app():
    return AppRecord(
        name="Paint",
        aumid="{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\mspaint.exe",
        exe="C:\\Windows\\system32\\mspaint.exe",
        process_names=["mspaint.exe"],
        publisher="Microsoft",
        version="10.0",
    )


def _paint_info():
    info = gen.AppUIInfo(window_title="Untitled - Paint", visited=42, elapsed_sec=0.5)
    info.menus = {
        "File": [
            gen.UIElementInfo("New", "MenuItemControl", "Ctrl+N", "N", "File > New"),
            gen.UIElementInfo("Exit", "MenuItemControl", "", "X", "File > Exit"),
        ]
    }
    info.menu_access_keys = {"File": "F"}
    info.elements = [
        gen.UIElementInfo("Pencil", "ButtonControl", "", "P"),
        gen.UIElementInfo("Undo", "ButtonControl", "Ctrl+Z", ""),
    ]
    return info


def test_build_skill_content():
    skill = gen.build_skill(_paint_app(), _paint_info(), today=date(2026, 10, 24))
    assert skill.name == "paint"
    assert skill.source == "generated"
    assert skill.match["process"] == ["mspaint.exe"]
    assert skill.match["exe"] == ["mspaint.exe"]
    body = skill.body
    assert "| File > New | `Ctrl+N` |" in body
    assert "| Undo | `Ctrl+Z` |" in body
    assert "**File** (`Alt+F`): New [N], Exit [X]" in body
    assert "- Button: Pencil (access key `P`)" in body
    assert 'Shortcut(shortcut="ctrl+n")' in body
    assert "2026-10-24" in body
    # The rendered document must load back through the store's parser.
    assert Skill.parse(skill.render()).name == "paint"


def test_write_generated_never_clobbers_user_skill(dirs):
    builtin, user = dirs
    (user / "paint.md").write_text(_doc("paint", source="user", process=("mspaint.exe",)))
    store = SkillStore(user_dir=user, builtin=builtin)
    skill = gen.build_skill(_paint_app(), _paint_info())
    with pytest.raises(gen.GenerationError, match="user skill"):
        gen.write_generated(store, skill, regenerate=True)
    assert "source: user" in (user / "paint.md").read_text()


def test_write_generated_and_regenerate(dirs):
    builtin, user = dirs
    store = SkillStore(user_dir=user, builtin=builtin)
    path = gen.write_generated(store, gen.build_skill(_paint_app(), _paint_info()))
    assert path == user / "paint.md"
    assert store.get("paint").source == "generated"
    assert store.get("paint").version == 1

    with pytest.raises(gen.GenerationError, match="regenerate"):
        gen.write_generated(store, gen.build_skill(_paint_app(), _paint_info()))
    gen.write_generated(store, gen.build_skill(_paint_app(), _paint_info()), regenerate=True)
    assert store.get("paint").version == 2
    assert not list(user.glob("*.tmp"))
    assert not list(builtin.iterdir())


def test_ribbon_keytips_table_and_recipe():
    info = gen.AppUIInfo(visited=5)
    info.elements = [
        gen.UIElementInfo("Bold", "ButtonControl", "", "Alt, H, B"),
        gen.UIElementInfo("Align text left", "ButtonControl", "", "Alt, H, A L"),
        gen.UIElementInfo("Copy (Ctrl+C)", "ButtonControl", "Ctrl+C", ""),
    ]
    skill = gen.build_skill(_paint_app(), info)
    assert "| Bold | `Alt, H, B` |" in skill.body
    assert "| Align text left | `Alt, H, A L` |" in skill.body
    assert (
        '`Shortcut(shortcut="alt")`, `Shortcut(shortcut="h")`, `Shortcut(shortcut="b")`'
        in skill.body
    )
    assert "ribbon KeyTips" in skill.description
    assert gen.keytip_to_shortcuts("Alt, H, A L") == []
    assert gen.keytip_to_shortcuts("Alt, 1") == ["alt", "1"]


class FakeControl:
    def __init__(self, name, control_type, children=(), access_key="", fail=False):
        self.Name, self.ControlTypeName = name, control_type
        self.AcceleratorKey, self.AccessKey = "", access_key
        self._children, self._fail = list(children), fail
        self._next = None

    def GetChildren(self):
        if self._fail:
            raise OSError("An event was unable to invoke any of the subscribers")
        return self._children

    def GetFirstChildControl(self):
        for a, b in zip(self._children, self._children[1:]):
            a._next = b
        return self._children[0] if self._children else None

    def GetNextSiblingControl(self):
        return self._next


def test_walk_uia_bounds_dedupes_and_falls_back(monkeypatch):
    from windows_mcp import uia

    ribbon = FakeControl(
        "",
        "PaneControl",
        [
            FakeControl("Paste", "SplitButtonControl"),
            FakeControl("Paste", "SplitButtonControl", access_key="Alt, H, V"),
            FakeControl("Copy (Ctrl+C)", "ButtonControl"),
            FakeControl("-", "ButtonControl"),
            FakeControl("Close", "ButtonControl"),
        ],
        fail=True,
    )
    root = FakeControl(
        "Doc - App",
        "WindowControl",
        [ribbon, FakeControl("Body", "DocumentControl", [FakeControl("Hidden", "ButtonControl")])],
    )
    monkeypatch.setattr(uia, "ControlFromHandle", lambda hwnd: root)
    info = gen.walk_uia(1)
    assert [(e.name, e.access_key, e.accelerator) for e in info.elements] == [
        ("Paste", "Alt, H, V", ""),
        ("Copy (Ctrl+C)", "", "Ctrl+C"),
    ]
    assert info.window_title == "Doc - App"
    assert not info.truncated

    limited = gen.walk_uia(1, max_elements=2)
    assert limited.truncated and limited.visited == 2
