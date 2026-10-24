"""Pure scoring for the target resolver: text in, ranked candidates out.

Nothing here touches UI Automation, so the ranking rules are unit-testable on any
platform. ``uia_source`` turns live UIA elements into :class:`Candidate` objects;
this module decides which one the planner meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from thefuzz import fuzz

# --------------------------------------------------------------------------------------
# Control-type hints
# --------------------------------------------------------------------------------------

BUTTON_TYPES = frozenset({"ButtonControl", "SplitButtonControl"})
EDIT_TYPES = frozenset({"EditControl", "ComboBoxControl", "DocumentControl"})

# Multi-word phrases are matched before single words. Each maps to the UIA control
# types the phrase usually means. Phrases are removed from the text that is matched
# against element names ("File menu" -> name "file" + a MenuItem hint).
_HINT_PHRASES: list[tuple[str, frozenset[str]]] = [
    ("check box", frozenset({"CheckBoxControl"})),
    ("radio button", frozenset({"RadioButtonControl"})),
    ("menu item", frozenset({"MenuItemControl"})),
    ("list item", frozenset({"ListItemControl", "DataItemControl"})),
    ("tree item", frozenset({"TreeItemControl"})),
    ("text box", EDIT_TYPES),
    ("text field", EDIT_TYPES),
    ("input box", EDIT_TYPES),
    ("drop down", frozenset({"ComboBoxControl", "SplitButtonControl", "MenuItemControl"})),
]
_HINT_WORDS: dict[str, frozenset[str]] = {
    "button": BUTTON_TYPES,
    "btn": BUTTON_TYPES,
    "box": EDIT_TYPES,
    "field": EDIT_TYPES,
    "input": EDIT_TYPES,
    "textbox": EDIT_TYPES,
    "searchbox": EDIT_TYPES,
    "tab": frozenset({"TabItemControl"}),
    "link": frozenset({"HyperlinkControl"}),
    "hyperlink": frozenset({"HyperlinkControl"}),
    "checkbox": frozenset({"CheckBoxControl"}),
    "toggle": frozenset({"CheckBoxControl", "ButtonControl"}),
    "radio": frozenset({"RadioButtonControl"}),
    "menu": frozenset({"MenuItemControl"}),
    "dropdown": frozenset({"ComboBoxControl", "SplitButtonControl", "MenuItemControl"}),
    "combo": frozenset({"ComboBoxControl"}),
    "combobox": frozenset({"ComboBoxControl"}),
    "slider": frozenset({"SliderControl"}),
}
_STOP_WORDS = frozenset({"the", "a", "an", "click", "press", "tap", "on", "select", "hit"})

# Explicit control_type argument: accept "button", "Button", "ButtonControl", "edit" ...
_EXPLICIT_TYPES: dict[str, frozenset[str]] = {
    **_HINT_WORDS,
    "edit": EDIT_TYPES,
    "text": frozenset({"TextControl"}),
    "menuitem": frozenset({"MenuItemControl"}),
    "tabitem": frozenset({"TabItemControl"}),
    "listitem": frozenset({"ListItemControl", "DataItemControl"}),
    "treeitem": frozenset({"TreeItemControl"}),
    "radiobutton": frozenset({"RadioButtonControl"}),
    "splitbutton": frozenset({"SplitButtonControl"}),
    "image": frozenset({"ImageControl"}),
    "document": frozenset({"DocumentControl"}),
}

# Types that are usually clickable vs. merely present.
_INTERACTIVE_TYPES = frozenset(
    {
        "ButtonControl",
        "SplitButtonControl",
        "MenuItemControl",
        "TabItemControl",
        "HyperlinkControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "ComboBoxControl",
        "EditControl",
        "ListItemControl",
        "TreeItemControl",
        "DataItemControl",
        "HeaderItemControl",
        "SliderControl",
        "SpinnerControl",
        "DocumentControl",
    }
)
_CHROME_TYPES = frozenset(
    {"WindowControl", "TitleBarControl", "ScrollBarControl", "ThumbControl", "SeparatorControl"}
)

# Calculator-style aliases: "7" also matches "Seven", "+" matches "Plus", ...
_ALIASES: dict[str, str] = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "+": "plus",
    "-": "minus",
    "=": "equals",
    "*": "multiply by",
    "x": "multiply by",
    "×": "multiply by",
    "times": "multiply by",
    "/": "divide by",
    "÷": "divide by",
    ".": "decimal separator",
    "equal": "equals",
    "multiply": "multiply by",
    "divide": "divide by",
}

_PATH_SPLIT_RE = re.compile(r"\s*(?:->|→|>)\s*")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Za-z])|[_\-.]+")
_SHORTCUT_SUFFIX_RE = re.compile(r"\s*(?:\t.*|\s{2,}ctrl\+.*)$", re.IGNORECASE)


def normalize(text: str | None) -> str:
    """Casefold and strip the noise UI frameworks put into names.

    Removes ``&`` access-key markers, trailing ``...``/``…``, a tab-separated
    shortcut suffix (``"Save\\tCtrl+S"``) and surrounding punctuation.
    """
    if not text:
        return ""
    value = _SHORTCUT_SUFFIX_RE.sub("", str(text))
    value = value.replace("&&", "\0").replace("&", "").replace("\0", "&")
    value = value.casefold().strip()
    value = re.sub(r"(\.\.\.|…)+$", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    if len(value) > 1:
        value = value.strip(" \"'`:;,")
    return value


def split_identifier(identifier: str | None) -> str:
    """Turn an AutomationId like ``num7Button`` into ``num 7 button``."""
    if not identifier:
        return ""
    return normalize(" ".join(part for part in _CAMEL_RE.split(identifier) if part))


def split_path(target: str) -> list[str]:
    """Split a ``"File > Save As"`` menu chain into its steps."""
    return [step for step in _PATH_SPLIT_RE.split(target.strip()) if step]


def explicit_type_hint(control_type: str | None) -> frozenset[str]:
    """Map a ``control_type`` argument to UIA control type names."""
    if not control_type:
        return frozenset()
    key = control_type.strip()
    if key.endswith("Control") and key[0].isupper():
        return frozenset({key})
    key = key.casefold().replace(" ", "").replace("_", "")
    if key.endswith("control"):
        key = key[: -len("control")]
    if key in _EXPLICIT_TYPES:
        return _EXPLICIT_TYPES[key]
    # Unknown word: treat it as a literal UIA type name ("Spinner" -> SpinnerControl).
    return frozenset({control_type.strip()[:1].upper() + control_type.strip()[1:] + "Control"})


@dataclass(frozen=True)
class Query:
    """A parsed target: the text to match plus any control types it implies."""

    raw: str
    text: str
    type_hints: frozenset[str] = frozenset()
    explicit_types: frozenset[str] = frozenset()

    @property
    def variants(self) -> list[str]:
        """Every spelling worth matching: stripped text, raw text, and aliases."""
        out: list[str] = []
        for value in (self.text, normalize(self.raw)):
            if value and value not in out:
                out.append(value)
            alias = _ALIASES.get(value)
            if alias and alias not in out:
                out.append(alias)
        return out


def parse_target(target: str, control_type: str | None = None) -> Query:
    """Parse ``"Save button"`` into name text ``"save"`` and a Button hint."""
    raw = target.strip()
    text = " " + normalize(raw) + " "
    hints: set[str] = set()
    for phrase, types in _HINT_PHRASES:
        padded = f" {phrase} "
        if padded in text:
            hints |= types
            text = text.replace(padded, " ")
    words = [word for word in text.split() if word not in _STOP_WORDS]
    kept: list[str] = []
    for word in words:
        if word in _HINT_WORDS:
            hints |= _HINT_WORDS[word]
        else:
            kept.append(word)
    stripped = " ".join(kept)
    if not stripped:
        # "Menu" or "Search box" alone: the hint word *is* the name.
        stripped = " ".join(words) or normalize(raw)
    return Query(
        raw=raw,
        text=stripped,
        type_hints=frozenset(hints),
        explicit_types=explicit_type_hint(control_type),
    )


# --------------------------------------------------------------------------------------
# Candidates and scoring
# --------------------------------------------------------------------------------------


@dataclass
class Candidate:
    """One on-screen element that might be what the planner meant."""

    name: str
    control_type: str
    left: int
    top: int
    right: int
    bottom: int
    automation_id: str = ""
    help_text: str = ""
    legacy_name: str = ""
    enabled: bool = True
    focused: bool = False
    in_foreground: bool = True
    window_title: str = ""
    window_handle: int = 0
    score: float = 0.0
    matched_on: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def type_label(self) -> str:
        """``ButtonControl`` -> ``button`` for human-readable output."""
        label = self.control_type
        if label.endswith("Control"):
            label = label[: -len("Control")]
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).lower()

    def to_dict(self) -> dict:
        x, y = self.center
        return {
            "name": self.name,
            "type": self.type_label,
            "loc": [x, y],
            "score": round(self.score, 3),
            "enabled": self.enabled,
            "window": self.window_title,
        }


def text_similarity(query: str, value: str) -> float:
    """Score how well a normalized ``value`` matches a normalized ``query``.

    Ordering matters more than the exact numbers: exact > prefix > same words >
    fuzzy. ``partial_ratio("save", "save as")`` is 100, so without the explicit
    tiers "Save" and "Save As" would tie.
    """
    if not query or not value:
        return 0.0
    if value == query:
        return 1.0
    if fuzz.token_sort_ratio(query, value) == 100:
        return 0.95
    if len(query) >= 2 and (value.startswith(query + " ") or value.startswith(query)):
        # Prefix: "save" -> "save as". Shorter extras are closer to exact.
        extra = len(value) - len(query)
        return max(0.8, 0.9 - 0.005 * extra)
    query_words = set(query.split())
    value_words = set(value.split())
    if query_words and query_words <= value_words:
        return max(0.72, 0.85 - 0.02 * (len(value_words) - len(query_words)))
    ratio = fuzz.ratio(query, value) / 100
    partial = fuzz.partial_ratio(query, value) / 100 * 0.8 if len(query) >= 4 else 0.0
    token = fuzz.token_set_ratio(query, value) / 100 * 0.8
    return min(0.85, max(ratio * 0.9, partial, token))


def _text_score(query: Query, candidate: Candidate) -> tuple[float, str]:
    fields = (
        ("name", normalize(candidate.name), 1.0),
        ("legacy_name", normalize(candidate.legacy_name), 1.0),
        ("automation_id", split_identifier(candidate.automation_id), 0.9),
        ("help_text", normalize(candidate.help_text), 0.8),
    )
    best, matched = 0.0, ""
    for variant in query.variants:
        for field_name, value, weight in fields:
            if not value:
                continue
            score = text_similarity(variant, value) * weight
            if score > best:
                best, matched = score, field_name
    return best, matched


def _type_factor(query: Query, candidate: Candidate, prefer_types: frozenset[str]) -> float:
    ctype = candidate.control_type
    if query.explicit_types:
        return 1.0 if ctype in query.explicit_types else 0.55
    if query.type_hints:
        return 1.0 if ctype in query.type_hints else 0.8
    if prefer_types:
        return 1.0 if ctype in prefer_types else 0.85
    if ctype in _INTERACTIVE_TYPES:
        return 1.0
    if ctype in _CHROME_TYPES:
        return 0.7
    return 0.88


def _tiebreak(candidate: Candidate, screen_height: int) -> float:
    """Tiny preference for the foreground window, focus, size and top position."""
    bonus = 0.0
    if candidate.in_foreground:
        bonus += 0.004
    if candidate.focused:
        bonus += 0.002
    area = candidate.width * candidate.height
    bonus += 0.002 * min(1.0, area / 4000)
    if screen_height > 0:
        bonus += 0.002 * (1 - min(1.0, max(0, candidate.top) / screen_height))
    return bonus


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse elements that are the same thing on screen (a button and its label)."""
    kept: list[Candidate] = []
    for cand in candidates:
        cx, cy = cand.center
        duplicate = False
        for other in kept:
            ox, oy = other.center
            if (
                abs(cx - ox) <= 4
                and abs(cy - oy) <= 4
                and normalize(cand.name) == normalize(other.name)
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept


@dataclass
class Ranking:
    """The outcome of scoring: ranked candidates plus the chosen one."""

    candidates: list[Candidate]
    best: Candidate | None
    confidence: float
    note: str = ""


def rank(
    query: Query,
    candidates: list[Candidate],
    *,
    nth: int | None = None,
    prefer_types: frozenset[str] = frozenset(),
    min_confidence: float = 0.7,
    screen_height: int = 1080,
) -> Ranking:
    """Score, sort and pick. ``nth`` is 1-based, in reading order among strong matches."""
    scored: list[tuple[float, Candidate]] = []
    for cand in candidates:
        if cand.width <= 0 or cand.height <= 0:
            continue
        text, matched = _text_score(query, cand)
        if text <= 0:
            continue
        score = text * _type_factor(query, cand, prefer_types)
        if not cand.enabled:
            score *= 0.6
        cand.score = round(min(1.0, score), 4)
        cand.matched_on = matched
        scored.append((cand.score + _tiebreak(cand, screen_height), cand))
    scored.sort(key=lambda item: item[0], reverse=True)
    ordered = _dedupe([cand for _, cand in scored])
    if not ordered:
        return Ranking(candidates=[], best=None, confidence=0.0, note="no matching elements")

    if nth is not None:
        if nth < 1:
            raise ValueError("nth must be 1 or greater (1 = first match in reading order)")
        floor = max(min_confidence, ordered[0].score - 0.1)
        pool = [cand for cand in ordered if cand.score >= floor]
        # Reading order: rows (8 px tolerance) top-to-bottom, then left-to-right.
        pool.sort(key=lambda cand: (cand.top // 8, cand.left))
        if nth > len(pool):
            return Ranking(
                candidates=ordered,
                best=None,
                confidence=0.0,
                note=f"nth={nth} requested but only {len(pool)} strong match(es) found",
            )
        chosen = pool[nth - 1]
        return Ranking(candidates=ordered, best=chosen, confidence=chosen.score)

    best = ordered[0]
    confidence = best.score
    note = ""
    rivals = [
        cand
        for cand in ordered[1:]
        if cand.score >= best.score - 0.02 and cand.score >= min_confidence
    ]
    if rivals:
        # Several equally good matches: still usable, but say so and lower confidence
        # so a strict threshold refuses to guess.
        confidence = max(0.0, best.score - 0.1)
        note = f"ambiguous: {len(rivals) + 1} similar matches; pass nth to pick one"
    return Ranking(candidates=ordered, best=best, confidence=round(confidence, 4), note=note)
