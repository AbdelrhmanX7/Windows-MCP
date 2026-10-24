"""Policy classification for Tasks and RunParallel (strictest inner category)."""

import pytest

from windows_mcp.tools.control_policy import category, is_screen_call, requires_control


def rp(*calls):
    return {"calls": [{"tool": t, "args": a} for t, a in calls]}


@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
def test_tasks_is_never_gated(policy):
    assert not requires_control(policy, "Tasks", {"mode": "update"})
    assert not is_screen_call("Tasks", {})


@pytest.mark.parametrize(
    "args,expected",
    [
        (rp(), "none"),
        (rp(("FileSystem", {}), ("Process", {"mode": "list"})), "other"),
        (rp(("FileSystem", {}), ("PowerShell", {})), "possible"),
        (rp(("PowerShell", {}), ("App", {"mode": "switch"})), "direct"),
        (rp(("Tasks", {}), ("Clipboard", {"mode": "get"})), "other"),
        (rp(("NewTool", {})), "direct"),
        (rp(("RunParallel", {})), "direct"),
        ({"calls": "[not json"}, "direct"),
        ({"calls": '[{"tool": "Registry"}]'}, "other"),
    ],
)
def test_run_parallel_inherits_strictest(args, expected):
    assert category("RunParallel", args) == expected
    for policy in ("relaxed", "balanced"):
        want = expected == "direct" or (policy == "balanced" and expected == "possible")
        assert requires_control(policy, "RunParallel", args) is want
    assert requires_control("strict", "RunParallel", args)
    assert not is_screen_call("RunParallel", args)  # Inner calls take the lock themselves.


@pytest.mark.parametrize(
    "name,args,screen",
    [
        ("Click", {}, True),
        ("Type", {}, True),
        ("MultiEdit", {}, True),
        ("App", {"mode": "switch"}, True),
        ("App", {"mode": "resize"}, True),
        ("App", {"mode": "launch"}, False),
        ("App", {}, False),
        ("PowerShell", {}, False),
        ("Clipboard", {"mode": "set"}, False),
        ("Screenshot", {}, False),
        ("ControlStatus", {}, False),
        ("UnknownTool", {}, True),
    ],
)
def test_screen_classification(name, args, screen):
    assert is_screen_call(name, args) is screen
