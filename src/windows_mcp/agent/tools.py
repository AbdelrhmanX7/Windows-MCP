"""The compact tool menu the inner ``Do`` model sees, mapped onto the server's real tools.

Each entry is a real MCP tool name with a trimmed schema (the planner-facing descriptions
are long; the inner model gets one line each). ``Done`` is the loop's own finish tool.
"""

from __future__ import annotations

from windows_mcp.models.chat import ToolSpec

DONE = "Done"
# Never offered: orchestrators (re-entrancy) and tools that are pointless or heavy inside Do.
NEVER_INNER = frozenset({"Do", "Route", "RunParallel", "Tasks", "ControlStatus", "Screenshot"})


def _obj(props: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_TARGET = {
    "type": "string",
    "description": "Element in words, e.g. 'Save button', 'search box', 'File > Save As'.",
}

INNER_TOOLS: dict[str, ToolSpec] = {
    "Snapshot": ToolSpec(
        "Snapshot",
        "Read the screen as text: windows, focused window, interactive elements with "
        "names/types/coordinates. Use only when you need to see what is on screen.",
        _obj({"use_dom": {**_B, "description": "Browser page elements instead of browser UI."}}),
    ),
    "Click": ToolSpec(
        "Click",
        "Click an element by name (target) in the foreground window, or at loc [x, y].",
        _obj(
            {
                "target": _TARGET,
                "loc": {"type": "array", "items": _I, "minItems": 2, "maxItems": 2},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {**_I, "description": "0 hover, 1 single, 2 double."},
                "control_type": {**_S, "description": "button, edit, menu, tab, link, ..."},
                "window": {**_S, "description": "Window title to search instead of foreground."},
                "nth": {**_I, "description": "1-based match in reading order."},
            }
        ),
    ),
    "Type": ToolSpec(
        "Type",
        "Type text into a field named by target (or at loc). clear=true replaces existing "
        "text; press_enter=true submits.",
        _obj(
            {
                "text": _S,
                "target": _TARGET,
                "loc": {"type": "array", "items": _I, "minItems": 2, "maxItems": 2},
                "clear": _B,
                "press_enter": _B,
                "window": _S,
            },
            ["text"],
        ),
    ),
    "Shortcut": ToolSpec(
        "Shortcut",
        "Press a key chord, e.g. 'ctrl+s', 'enter', 'alt+f4', 'win+r'.",
        _obj({"shortcut": _S}, ["shortcut"]),
    ),
    "App": ToolSpec(
        "App",
        "mode=launch opens an app by Start menu name; mode=switch focuses a window by name.",
        _obj({"mode": {"type": "string", "enum": ["launch", "switch"]}, "name": _S}, ["name"]),
    ),
    "Scroll": ToolSpec(
        "Scroll",
        "Scroll at loc [x, y] (or the cursor). direction up/down/left/right, wheel_times.",
        _obj(
            {
                "loc": {"type": "array", "items": _I, "minItems": 2, "maxItems": 2},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "wheel_times": _I,
            }
        ),
    ),
    "Wait": ToolSpec(
        "Wait",
        "Rarely needed (actions already wait for readiness). Up to 1-5 s; returns early "
        "when nothing is loading.",
        _obj({"duration": _I}, ["duration"]),
    ),
    "WaitFor": ToolSpec(
        "WaitFor",
        "Wait until a condition holds: text_exists, active_window, element_exists "
        "(text and/or window_name), up to timeout seconds.",
        _obj(
            {
                "condition": {
                    "type": "string",
                    "enum": ["text_exists", "active_window", "element_exists"],
                },
                "text": _S,
                "window_name": _S,
                "timeout": {"type": "number"},
            },
            ["condition"],
        ),
    ),
    "PowerShell": ToolSpec(
        "PowerShell",
        "Run a PowerShell command. Best for URIs and files: Start-Process 'ms-settings:display', "
        "Start-Process msedge 'https://...', Get-ChildItem -Recurse -Filter ...",
        _obj({"command": _S, "timeout": _I}, ["command"]),
    ),
    "AppSkill": ToolSpec(
        "AppSkill",
        "How-to guide for an app (URI deep links, shortcuts, recipes). Cheap; use before "
        "exploring an unfamiliar app's UI.",
        _obj({"name": _S}, ["name"]),
    ),
    "Apps": ToolSpec(
        "Apps",
        "Find installed apps: mode=list with query, or mode=info for one app.",
        _obj({"mode": {"type": "string", "enum": ["list", "info"]}, "query": _S}),
    ),
    "Locate": ToolSpec(
        "Locate",
        "Find an element by name without clicking; returns candidates and coordinates.",
        _obj({"target": _TARGET, "control_type": _S, "window": _S}, ["target"]),
    ),
}

DONE_TOOL = ToolSpec(
    DONE,
    "Finish. answer: one short sentence for the user (or the answer they asked for). "
    "success: whether the goal was achieved. expect: what the screen should show now if it "
    "worked (e.g. 'Notepad window containing milk, eggs'); used to verify. Call it in the "
    "same turn as your last actions when you are confident.",
    _obj({"answer": _S, "success": _B, "expect": _S}, ["answer", "success"]),
)

# Extra arguments pinned on every inner call (keeps observations small and image-free).
FIXED_ARGS = {"Snapshot": {"use_vision": False, "use_annotation": False}}


def menu(available: set[str] | None) -> list[ToolSpec]:
    """Inner tools that are actually registered (so --exclude-tools is honored), plus Done."""
    names = [
        name
        for name in INNER_TOOLS
        if name not in NEVER_INNER and (available is None or name in available)
    ]
    return [INNER_TOOLS[name] for name in names] + [DONE_TOOL]


def inner_args(name: str, args: dict) -> dict:
    """Drop arguments the inner schema does not offer, then pin the fixed ones."""
    spec = INNER_TOOLS.get(name)
    allowed = set(spec.schema.get("properties", {})) if spec else set()
    clean = {k: v for k, v in (args or {}).items() if k in allowed and v is not None}
    if name == "Wait" and isinstance(clean.get("duration"), (int, float)):
        clean["duration"] = int(max(1, min(5, clean["duration"])))
    if name == "Scroll" and clean.get("direction") in ("left", "right"):
        clean["type"] = "horizontal"
    clean.update(FIXED_ARGS.get(name, {}))
    return clean
