"""Target resolver: parsing, ranking, path chains, the no-click threshold and tier 2.

The Windows layer is replaced by FakeSource (windows + candidates) and a fake
desktop, so no UI Automation call is made.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import requests

from windows_mcp.resolver import scoring, service, vision
from windows_mcp.resolver.scoring import Candidate, parse_target, rank
from windows_mcp.resolver.service import ResolveError, Resolver
from windows_mcp.resolver.uia_source import TargetWindow

MAIN = TargetWindow(handle=100, title="Untitled - Notepad", class_name="Notepad", process_id=7)
MENU = TargetWindow(handle=200, title="", class_name="#32768", process_id=7)


def cand(name: str, ctype: str = "ButtonControl", x: int = 100, y: int = 100, **kw) -> Candidate:
    return Candidate(
        name=name,
        control_type=ctype,
        left=x - 20,
        top=y - 10,
        right=x + 20,
        bottom=y + 10,
        **kw,
    )


CALCULATOR = [
    cand("Seven", automation_id="num7Button", x=50, y=400),
    cand("Eight", automation_id="num8Button", x=100, y=400),
    cand("Nine", automation_id="num9Button", x=150, y=400),
    cand("Plus", automation_id="plusButton", x=200, y=450),
    cand("Minus", automation_id="minusButton", x=200, y=400),
    cand("Equals", automation_id="equalButton", x=200, y=500),
    cand("Display is 0", "TextControl", x=120, y=100),
]


class FakeSource:
    """Stands in for uia_source: windows, popups and their elements."""

    def __init__(self, elements: dict[int, list[Candidate]], popups: list[TargetWindow] = ()):
        self.elements = elements
        self.popups = list(popups)
        self.collect_calls: list[tuple[int, bool]] = []

    def foreground_window(self) -> TargetWindow:
        return MAIN

    def find_window(self, name: str) -> TargetWindow | None:
        return MAIN if "notepad" in name.casefold() else None

    def popup_windows(self, root: TargetWindow) -> list[TargetWindow]:
        return list(self.popups)

    def collect(self, window: TargetWindow, *, secondary: bool = False) -> list[Candidate]:
        self.collect_calls.append((window.handle, secondary))
        items = self.elements.get(window.handle, [])
        primary = {"ButtonControl", "MenuItemControl", "EditControl", "TabItemControl"}
        return [
            Candidate(**{**c.__dict__, "extra": {}})
            for c in items
            if (c.control_type not in primary) == secondary
        ]

    def virtual_screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "target,text,hint",
    [
        ("Save button", "save", "ButtonControl"),
        ("search box", "search", "EditControl"),
        ("File menu", "file", "MenuItemControl"),
        ("the Settings tab", "settings", "TabItemControl"),
        ("Privacy link", "privacy", "HyperlinkControl"),
        ("Remember me checkbox", "remember me", "CheckBoxControl"),
        ("Remember me check box", "remember me", "CheckBoxControl"),
        ("click Play", "play", None),
    ],
)
def test_parse_target_extracts_text_and_type_hints(target, text, hint):
    query = parse_target(target)
    assert query.text == text
    if hint is None:
        assert query.type_hints == frozenset()
    else:
        assert hint in query.type_hints


def test_parse_target_keeps_hint_word_when_it_is_the_whole_name():
    assert parse_target("Menu").text == "menu"
    assert "MenuItemControl" in parse_target("Menu").type_hints


@pytest.mark.parametrize(
    "value,expected",
    [
        ("button", {"ButtonControl", "SplitButtonControl"}),
        ("ButtonControl", {"ButtonControl"}),
        ("edit", {"EditControl", "ComboBoxControl", "DocumentControl"}),
        ("Menu Item", {"MenuItemControl"}),
        (None, set()),
    ],
)
def test_explicit_control_type(value, expected):
    assert scoring.explicit_type_hint(value) == frozenset(expected)


def test_normalize_strips_accelerators_ellipsis_and_shortcuts():
    assert scoring.normalize("&Save As...\tCtrl+Shift+S") == "save as"
    assert scoring.normalize("Page Set&up…") == "page setup"
    assert scoring.split_identifier("num7Button") == "num 7 button"


def test_split_path():
    assert scoring.split_path("File > Save As") == ["File", "Save As"]
    assert scoring.split_path("Edit -> Find → Replace") == ["Edit", "Find", "Replace"]
    assert scoring.split_path("Play") == ["Play"]


# --------------------------------------------------------------------------- ranking


def test_exact_beats_prefix():
    items = [cand("Save As...", "MenuItemControl", y=120), cand("Save", "MenuItemControl", y=100)]
    assert rank(parse_target("Save"), items).best.name == "Save"
    assert rank(parse_target("Save As"), items).best.name == "Save As..."


@pytest.mark.parametrize(
    "target,expected",
    [("Seven", "Seven"), ("7", "Seven"), ("+", "Plus"), ("plus", "Plus"), ("=", "Equals")],
)
def test_calculator_names_and_aliases(target, expected):
    ranking = rank(parse_target(target), list(CALCULATOR))
    assert ranking.best.name == expected
    assert ranking.confidence >= 0.9


def test_type_hint_prefers_matching_control():
    items = [cand("Search", "ButtonControl", y=100), cand("Search", "EditControl", y=100, x=300)]
    assert rank(parse_target("search box"), items).best.control_type == "EditControl"
    assert rank(parse_target("search button"), items).best.control_type == "ButtonControl"


def test_explicit_control_type_outweighs_name_order():
    items = [cand("Search", "ButtonControl"), cand("Search", "EditControl", x=300)]
    query = parse_target("Search", control_type="edit")
    assert rank(query, items).best.control_type == "EditControl"


def test_automation_id_and_help_text_match():
    items = [cand("", automation_id="SubmitOrderButton"), cand("X", help_text="Close window")]
    assert rank(parse_target("submit order"), items).best.automation_id == "SubmitOrderButton"
    assert rank(parse_target("close window"), items).best.name == "X"


def test_disabled_and_zero_size_elements_lose():
    items = [
        cand("Next", enabled=False, x=100),
        cand("Next", x=300, y=300),
        Candidate("Next", "ButtonControl", 0, 0, 0, 0),
    ]
    ranking = rank(parse_target("Next"), items)
    assert ranking.best.enabled
    assert all(c.width > 0 for c in ranking.candidates)


def test_unrelated_names_score_low():
    ranking = rank(parse_target("Bluetooth"), list(CALCULATOR))
    assert ranking.confidence < 0.7


def test_duplicates_lower_confidence_and_nth_picks_in_reading_order():
    items = [cand("Play", x=500, y=300), cand("Play", x=100, y=300), cand("Play", x=100, y=600)]
    ranking = rank(parse_target("Play"), items)
    assert "ambiguous" in ranking.note
    assert ranking.confidence < 1.0
    second = rank(parse_target("Play"), items, nth=2)
    assert second.best.center == (500, 300)
    first = rank(parse_target("Play"), items, nth=1)
    assert first.best.center == (100, 300)
    too_far = rank(parse_target("Play"), items, nth=9)
    assert too_far.best is None and "nth=9" in too_far.note


def test_label_and_button_at_same_spot_are_deduped():
    items = [cand("OK", "ButtonControl"), cand("OK", "TextControl")]
    ranking = rank(parse_target("OK"), items)
    assert len(ranking.candidates) == 1
    assert ranking.note == ""


def test_foreground_breaks_ties():
    items = [cand("Close", x=100, in_foreground=False), cand("Close", x=400)]
    assert rank(parse_target("Close"), items).best.in_foreground


# --------------------------------------------------------------------------- resolver


def test_resolve_reports_tier_latency_and_confidence():
    resolver = Resolver(source=FakeSource({100: list(CALCULATOR)}), env={})
    result = resolver.resolve("Seven")
    assert result.ok and result.tier == "uia"
    assert result.summary().startswith('resolved: "Seven" (button) via uia in ')
    assert "confidence 1.00" in result.summary()


def test_secondary_pass_only_when_primary_is_weak():
    source = FakeSource({100: list(CALCULATOR)})
    Resolver(source=source, env={}).resolve("Seven")
    assert source.collect_calls == [(100, False)]
    source.collect_calls.clear()
    Resolver(source=source, env={}).resolve("Display is 0")
    assert (100, True) in source.collect_calls


def test_below_threshold_does_not_click_and_lists_candidates():
    clicked: list[Candidate] = []
    resolver = Resolver(source=FakeSource({100: list(CALCULATOR)}), env={})
    with pytest.raises(ResolveError) as info:
        resolver.act("Bluetooth settings", clicked.append)
    assert clicked == []
    message = str(info.value)
    assert message.startswith('Not clicked: no confident match for "Bluetooth settings"')
    assert "1. " in message and "score" in message


def test_min_confidence_env_is_honored():
    clicked: list[Candidate] = []
    items = [cand("Save As...", "MenuItemControl")]
    strict = Resolver(
        source=FakeSource({100: items}), env={"WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE": "0.99"}
    )
    with pytest.raises(ResolveError):
        strict.act("Save", clicked.append)
    lenient = Resolver(
        source=FakeSource({100: items}), env={"WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE": "0.5"}
    )
    lenient.act("Save", clicked.append)
    assert [c.name for c in clicked] == ["Save As..."]


def test_off_screen_candidates_are_never_returned():
    items = [cand("Play", x=5000, y=5000)]
    resolver = Resolver(source=FakeSource({100: items}), env={})
    result = resolver.resolve("Play")
    assert result.best is None and not result.ok


def test_named_window_not_found():
    resolver = Resolver(source=FakeSource({}), env={})
    with pytest.raises(ResolveError, match="No visible window"):
        resolver.resolve("Play", window="Spotify")


def test_path_chain_clicks_each_step_and_waits_for_popup(monkeypatch):
    monkeypatch.setattr(service, "sleep", lambda s: None)
    source = FakeSource({100: [cand("File", "MenuItemControl", x=20, y=40)]})
    clicked: list[str] = []
    polls = {"n": 0}
    original_popups = source.popup_windows

    def popups(root):
        # The drop-down appears only after File was clicked, and a poll later.
        if clicked:
            polls["n"] += 1
            if polls["n"] >= 2:
                source.elements[200] = [cand("Save As...", "MenuItemControl", x=60, y=120)]
                return [MENU]
        return original_popups(root)

    source.popup_windows = popups
    results = Resolver(source=source, env={}).act(
        "File > Save As", lambda c: clicked.append(c.name)
    )
    assert clicked == ["File", "Save As..."]
    assert [r.best.name for r in results] == ["File", "Save As..."]


def test_path_chain_failure_reports_what_was_already_clicked(monkeypatch):
    monkeypatch.setattr(service, "sleep", lambda s: None)
    monkeypatch.setattr(service, "PATH_STEP_TIMEOUT", 0.0)
    source = FakeSource({100: [cand("File", "MenuItemControl")]})
    clicked: list[str] = []
    with pytest.raises(ResolveError) as info:
        Resolver(source=source, env={}).act("File > Bogus Entry", lambda c: clicked.append(c.name))
    assert clicked == ["File"]
    assert str(info.value).startswith('Already clicked "File" (path step 1 of 2).')


def test_collect_timeout_is_reported_not_raised(monkeypatch):
    from windows_mcp import uia

    source = FakeSource({100: list(CALCULATOR)})

    def boom(window, *, secondary=False):
        raise TimeoutError("UIA timed out")

    source.collect = boom
    monkeypatch.setattr(uia, "is_timeout_exception", lambda exc: isinstance(exc, TimeoutError))
    result = Resolver(source=source, env={}).resolve("Seven")
    assert not result.ok
    assert "not responding to UI Automation" in result.note


# --------------------------------------------------------------------------- tier 2


class FakeDesktop:
    def __init__(self) -> None:
        self.shots = 0

    def get_annotated_screenshot(self, nodes, capture_rect=None, **kwargs):
        from PIL import Image

        self.shots += 1
        return Image.new("RGB", (1600, 900), "white")


class FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.status_code = status
        self._text = text

    def json(self) -> dict:
        return {"content": [{"type": "text", "text": self._text}]}


@pytest.fixture
def sparse_resolver(monkeypatch):
    """A window with no accessible elements, so tier 1 can never succeed."""

    def make(env: dict) -> tuple[Resolver, FakeDesktop]:
        desktop = FakeDesktop()
        resolver = Resolver(desktop, source=FakeSource({100: []}), env=env)
        monkeypatch.setattr(resolver, "_window_rect", lambda root: (0, 0, 800, 600))
        return resolver, desktop

    return make


def test_tier2_never_runs_without_opt_in_even_with_api_key(monkeypatch, sparse_resolver):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))
    resolver, desktop = sparse_resolver({"ANTHROPIC_API_KEY": "sk-test"})
    result = resolver.resolve("Play")
    assert calls == [] and desktop.shots == 0
    assert not result.ok and result.tier == "uia"


def test_tier2_opt_in_without_key_degrades(monkeypatch, sparse_resolver):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))
    resolver, desktop = sparse_resolver({"WINDOWS_MCP_RESOLVER_VISION": "anthropic"})
    assert not resolver.resolve("Play").ok
    assert calls == [] and desktop.shots == 0


def test_tier2_picks_a_box_with_jpeg_and_model(monkeypatch, sparse_resolver):
    seen: dict = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return FakeResponse("9")

    monkeypatch.setattr(requests, "post", post)
    resolver, desktop = sparse_resolver(
        {
            "WINDOWS_MCP_RESOLVER_VISION": "anthropic",
            "ANTHROPIC_API_KEY": "sk-test",
            "WINDOWS_MCP_RESOLVER_VISION_TIMEOUT": "2.5",
        }
    )
    result = resolver.resolve("Play")
    assert result.ok and result.tier == "vision"
    assert seen["url"] == vision.ANTHROPIC_URL
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["body"]["model"] == "claude-haiku-5-5"
    assert seen["timeout"] == 2.5
    image = seen["body"]["messages"][0]["content"][0]
    assert image["source"]["media_type"] == "image/jpeg"
    # Box 9 of the default 8x6 grid over 800x600 is column 1, row 1.
    assert result.best.center == (150, 150)
    assert desktop.shots == 1


def test_tier2_uses_tier1_candidates_as_boxes(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse("1"))
    items = [cand("Alpha", x=100), cand("Bravo", x=300), cand("Charlie", x=500)]
    resolver = Resolver(
        FakeDesktop(),
        source=FakeSource({100: items}),
        env={"WINDOWS_MCP_RESOLVER_VISION": "anthropic", "ANTHROPIC_API_KEY": "k"},
    )
    monkeypatch.setattr(resolver, "_window_rect", lambda root: (0, 0, 800, 600))
    result = resolver.resolve("the shuffle toggle")
    assert result.tier == "vision"
    assert result.best.name in {"Alpha", "Bravo", "Charlie"}
    assert result.best.name == result.candidates[1].name


@pytest.mark.parametrize(
    "behavior",
    [
        lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")),
        lambda *a, **k: FakeResponse("", status=529),
        lambda *a, **k: FakeResponse("none"),
    ],
)
def test_tier2_failures_degrade_without_clicking(monkeypatch, sparse_resolver, behavior):
    monkeypatch.setattr(requests, "post", behavior)
    resolver, _ = sparse_resolver(
        {"WINDOWS_MCP_RESOLVER_VISION": "anthropic", "ANTHROPIC_API_KEY": "k"}
    )
    clicked: list = []
    with pytest.raises(ResolveError):
        resolver.act("Play", clicked.append)
    assert clicked == []


@pytest.mark.parametrize(
    "reply,expected", [("3", 3), (" 12\n", 12), ("Box 4", 4), ("none", None), ("99", None)]
)
def test_parse_pick(reply, expected):
    assert vision.parse_pick(reply, 20) == expected


def test_vision_config_parsing():
    from windows_mcp.resolver import config

    assert config.vision_provider({}) is None
    assert config.vision_provider({"WINDOWS_MCP_RESOLVER_VISION": "on"}) == "anthropic"
    assert config.vision_provider({"WINDOWS_MCP_RESOLVER_VISION": "gpt"}) is None
    assert config.min_confidence({"WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE": "2"}) == 0.7
    assert config.vision_model({"WINDOWS_MCP_RESOLVER_MODEL": "m"}) == "m"


# --------------------------------------------------------------------------- tools


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


class ToolDesktop:
    def __init__(self) -> None:
        self.desktop_state = None
        self.clicks: list[dict] = []
        self.typed: list[dict] = []
        self.raised: list[int] = []

    def click(self, **kwargs) -> None:
        self.clicks.append(kwargs)

    def type(self, **kwargs) -> None:
        self.typed.append(kwargs)

    def bring_window_to_top(self, handle: int) -> None:
        self.raised.append(handle)


@pytest.fixture
def tools(monkeypatch):
    from windows_mcp.tools import input as input_tools

    source = FakeSource(
        {
            100: list(CALCULATOR)
            + [cand("Search", "EditControl", x=600, y=50), cand("Search", x=700, y=50)]
        }
    )
    monkeypatch.setattr(
        input_tools, "Resolver", lambda desktop: Resolver(desktop, source=source, env={})
    )
    desktop = ToolDesktop()
    mcp = FakeMCP()
    input_tools.register(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools, desktop


def run(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def test_click_target_clicks_resolved_center(tools):
    registered, desktop = tools
    out = run(registered["Click"], target="Seven")
    assert desktop.clicks == [{"loc": [50, 400], "button": "left", "clicks": 1}]
    assert 'resolved: "Seven" (button) via uia in' in out


def test_click_target_refusal_is_an_error_and_clicks_nothing(tools):
    from fastmcp.exceptions import ToolError

    registered, desktop = tools
    with pytest.raises(ToolError, match="Not clicked"):
        run(registered["Click"], target="Bluetooth settings")
    assert desktop.clicks == []


def test_click_requires_exactly_one_of_loc_label_target(tools):
    registered, desktop = tools
    with pytest.raises(ValueError, match="exactly one"):
        run(registered["Click"], loc=[1, 2], target="Seven")
    with pytest.raises(ValueError, match="exactly one"):
        run(registered["Click"], label=3, target="Seven")
    with pytest.raises(ValueError, match="nth"):
        run(registered["Click"], target="Seven", nth=0)
    assert desktop.clicks == []


def test_click_loc_path_unchanged(tools):
    registered, desktop = tools
    assert run(registered["Click"], loc=[10, 20]) == "Single left clicked at (10,20)."
    assert desktop.clicks == [{"loc": [10, 20], "button": "left", "clicks": 1}]


def test_locate_is_read_only(tools):
    registered, desktop = tools
    out = run(registered["Locate"], target="Plus")
    assert out.startswith('resolved: "Plus" (button) via uia')
    assert "would click (200,450)" in out
    assert desktop.clicks == [] and desktop.typed == []


def test_locate_weak_match_lists_candidates(tools):
    registered, _ = tools
    out = run(registered["Locate"], target="Bluetooth")
    assert out.startswith("Not resolved:")


def test_locate_path_only_first_step(tools):
    registered, desktop = tools
    out = run(registered["Locate"], target="Seven > Plus")
    assert "only the first was located" in out
    assert desktop.clicks == []


def test_type_target_prefers_edit_fields(tools):
    registered, desktop = tools
    out = run(registered["Type"], text="hello", target="Search")
    assert desktop.typed[0]["loc"] == [600, 50]
    assert desktop.typed[0]["text"] == "hello"
    assert 'resolved: "Search" (edit)' in out
