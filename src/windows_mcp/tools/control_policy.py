"""One table for desktop ownership, separate from authorization or data safety.

When adding a tool or a mode, classify it here. Registration coverage tests catch
omissions; unclassified extensions conservatively retain the desktop gate.
"""

import json

# Categories describe desktop interference, not whether an operation is destructive.
DIRECT = frozenset({"Click", "Type", "Scroll", "Move", "Shortcut", "MultiSelect", "MultiEdit"})
# AppSkill is "possible": generate/regenerate may launch an app and read its window.
POSSIBLE = frozenset({"PowerShell", "Notification", "AppSkill"})
OTHER = frozenset(
    {
        "FileSystem",
        "Registry",
        "Snapshot",
        "Screenshot",
        "Scrape",
        "Wait",
        "WaitFor",
        "DisplayInventory",
        "Locate",
        "Apps",
        "Route",
    }
)
MODES = {
    "App": {
        "launch": "possible",
        "launch_executable": "possible",
        "resize": "direct",
        "switch": "direct",
    },
    "Process": {"list": "other", "kill": "possible"},
    "Clipboard": {"get": "other", "set": "possible"},
}
# Never gated and never take the screen lock: ControlStatus reports ownership, Tasks is
# local in-memory state, and RunParallel is an orchestrator whose inner calls are each
# gated (and screen-locked) individually. Gating the orchestrator itself would deadlock
# its inner calls on the gate's non-reentrant lock.
# Do is an orchestrator too (its inner calls are gated one by one), but unlike RunParallel it
# is itself a screen call: it holds the screen lock for its whole run so no other request's
# input lands between its steps. Its inner calls re-enter that lock (see tasks/runtime.py).
ORCHESTRATION = frozenset({"Tasks", "RunParallel", "Do"})
GATE_EXEMPT = frozenset({"ControlStatus"}) | ORCHESTRATION
CLASSIFIED_TOOLS = DIRECT | POSSIBLE | OTHER | MODES.keys() | GATE_EXEMPT

_RANK = {"none": 0, "other": 1, "possible": 2, "direct": 3}


def inner_calls(arguments: dict | None) -> list[tuple[str, dict]]:
    """Leniently extract (tool, args) pairs from RunParallel arguments for classification."""
    calls = (arguments or {}).get("calls")
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return [("", {})]  # Unparseable: classify as unknown (direct).
    if not isinstance(calls, list):
        return []
    out = []
    for call in calls:
        if not isinstance(call, dict):
            out.append(("", {}))
            continue
        name = call.get("tool")
        args = call.get("args")
        out.append((name if isinstance(name, str) else "", args if isinstance(args, dict) else {}))
    return out


def category(name: str, arguments: dict | None = None) -> str:
    """Return direct, possible, other or none (gate-exempt) for one tool call."""
    if name in ("ControlStatus", "Tasks"):
        return "none"
    if name == "Do":
        return "direct"  # Drives the mouse/keyboard through whatever the inner model picks.
    if name == "RunParallel":
        # The strictest category of everything it would run.
        worst = "none"
        for inner_name, inner_args in inner_calls(arguments):
            inner = "direct" if inner_name == "RunParallel" else category(inner_name, inner_args)
            if _RANK[inner] > _RANK[worst]:
                worst = inner
        return worst
    if name in MODES:
        mode = (arguments or {}).get("mode", "launch" if name == "App" else None)
        if isinstance(mode, str):
            return MODES[name].get(mode, "direct")
        return "direct"
    if name == "AppSkill" and any((arguments or {}).get(k) for k in ("generate", "regenerate")):
        return "direct"  # Generating may launch the app and walk its window.
    if name in POSSIBLE:
        return "possible"
    if name in OTHER:
        return "other"
    return "direct"  # Unknown tools/modes must not silently bypass ownership.


def is_screen_call(name: str, arguments: dict | None = None) -> bool:
    """True for calls that drive the shared mouse/keyboard/focus and must never interleave.

    Policy-independent: these are the ``direct`` operations (unknown tools included).
    RunParallel itself is not a screen call; its inner calls are checked one by one. Do is:
    it owns the screen for its whole run (its inner calls re-enter the lock).
    """
    if name == "Do":
        return True
    if name in GATE_EXEMPT:
        return False
    return category(name, arguments) == "direct"


def requires_control(policy: str, name: str, arguments: dict | None = None) -> bool:
    """Decide before taking the ownership lock; never inspect arbitrary scripts."""
    if name in ("ControlStatus", "Tasks"):
        return False
    if policy == "strict":
        return True
    if policy not in ("relaxed", "balanced"):
        raise ValueError(f"Unknown control policy: {policy}")
    found = category(name, arguments)
    return found == "direct" or (policy == "balanced" and found == "possible")
