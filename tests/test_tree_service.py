from _ctypes import COMError
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from windows_mcp.desktop.views import Size
from windows_mcp.tree.budget import WINDOW_CUT_TIME, TreeElementBudget
from windows_mcp.tree.config import THREAD_MAX_RETRIES
from windows_mcp.tree.service import (
    Tree,
    _excluded_process_names,
    _host_process_ids,
    _is_comtypes_variant_ord_typeerror,
    _merge_unspaced_word_runs,
    _skip_host_processes_enabled,
    _window_process_id,
    _window_process_name,
)
from windows_mcp.tree.views import BoundingBox, SemanticNode
from windows_mcp.uia import PropertyId, Rect


@pytest.fixture
def tree_instance():
    mock_desktop = MagicMock()
    mock_desktop.get_screen_size.return_value = Size(width=1920, height=1080)
    mock_desktop.get_screen_box.return_value = make_box(0, 0, 1920, 1080)
    return Tree(mock_desktop)


def make_box(left: int, top: int, right: int, bottom: int):
    return BoundingBox(
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        width=right - left,
        height=bottom - top,
    )


class TestAppNameCorrection:
    def test_progman(self, tree_instance):
        assert tree_instance.app_name_correction("Progman") == "Desktop"

    def test_shell_traywnd(self, tree_instance):
        assert tree_instance.app_name_correction("Shell_TrayWnd") == "Taskbar"

    def test_shell_secondary_traywnd(self, tree_instance):
        assert tree_instance.app_name_correction("Shell_SecondaryTrayWnd") == "Taskbar"

    def test_popup_window_site_bridge(self, tree_instance):
        assert (
            tree_instance.app_name_correction("Microsoft.UI.Content.PopupWindowSiteBridge")
            == "Context Menu"
        )

    def test_passthrough(self, tree_instance):
        assert tree_instance.app_name_correction("Notepad") == "Notepad"
        assert tree_instance.app_name_correction("Calculator") == "Calculator"


class TestIouBoundingBox:
    def test_full_overlap(self, tree_instance):
        window = Rect(0, 0, 500, 500)
        element = Rect(100, 100, 200, 200)
        result = tree_instance.iou_bounding_box(window, element)
        assert result.left == 100
        assert result.top == 100
        assert result.right == 200
        assert result.bottom == 200
        assert result.width == 100
        assert result.height == 100

    def test_partial_overlap(self, tree_instance):
        window = Rect(0, 0, 150, 150)
        element = Rect(100, 100, 200, 200)
        result = tree_instance.iou_bounding_box(window, element)
        assert result.left == 100
        assert result.top == 100
        assert result.right == 150
        assert result.bottom == 150
        assert result.width == 50
        assert result.height == 50

    def test_no_overlap(self, tree_instance):
        window = Rect(0, 0, 50, 50)
        element = Rect(100, 100, 200, 200)
        result = tree_instance.iou_bounding_box(window, element)
        assert result.width == 0
        assert result.height == 0

    def test_screen_clamping(self, tree_instance):
        # Element extends beyond screen (1920x1080)
        window = Rect(0, 0, 2000, 2000)
        element = Rect(1900, 1060, 2000, 1200)
        result = tree_instance.iou_bounding_box(window, element)
        assert result.left == 1900
        assert result.top == 1060
        assert result.right == 1920
        assert result.bottom == 1080
        assert result.width == 20
        assert result.height == 20

    def test_screen_box_keeps_virtual_screen_origin(self):
        mock_desktop = MagicMock()
        mock_desktop.get_screen_size.return_value = Size(width=3840, height=1080)
        mock_desktop.get_screen_box.return_value = make_box(-1920, 0, 1920, 1080)

        tree = Tree(mock_desktop)
        result = tree.iou_bounding_box(
            Rect(-1920, 0, 0, 1080),
            Rect(-100, 100, 100, 200),
        )

        assert result.left == -100
        assert result.top == 100
        assert result.right == 0
        assert result.bottom == 200


def _type_error_from(filename: str) -> TypeError:
    namespace = {}
    code = compile("def trigger():\n    ord('hello')\n", filename, "exec")
    exec(code, namespace)

    with pytest.raises(TypeError) as exc_info:
        namespace["trigger"]()

    return exc_info.value


class TestComtypesVariantOrdTypeError:
    def test_matches_comtypes_automation_traceback(self):
        error = _type_error_from(
            "C:/Python313/Lib/site-packages/comtypes/automation.py",
        )

        assert _is_comtypes_variant_ord_typeerror(error) is True

    def test_rejects_same_message_from_non_comtypes_traceback(self):
        error = _type_error_from(
            "C:/QA_Automation/Windows-MCP-PR/tests/helpers/fake_source.py",
        )

        assert _is_comtypes_variant_ord_typeerror(error) is False


class TestTreeTraversal:
    def test_unnamed_interactive_control_does_not_add_semantic_child(
        self, tree_instance, monkeypatch
    ):
        child = MagicMock()
        child.CachedIsOffscreen = False
        child.CachedControlTypeName = "ButtonControl"
        child.CachedIsControlElement = True
        child.CachedBoundingRectangle = Rect(10, 10, 110, 60)
        child.CachedIsEnabled = True
        child.CachedHasKeyboardFocus = False
        child.CachedName = "   "
        child.CachedLocalizedControlType = "button"
        child.CachedAcceleratorKey = ""
        child.GetCachedPropertyValue.return_value = 43

        semantic_root = SemanticNode(
            control_type="Window",
            element_type="window",
            name="Window",
            window_name="Window",
        )

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            lambda node, cache_request: [],
        )
        monkeypatch.setattr(
            "windows_mcp.tree.service.random_point_within_bounding_box",
            lambda node, scale_factor: (60, 35),
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        interactive_nodes = []
        tree_instance.tree_traversal(
            child,
            Rect(0, 0, 200, 200),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
            current_semantic_node=semantic_root,
        )

        assert interactive_nodes == []
        assert semantic_root.children == []

    def test_stale_uia_subtree_is_pruned(self, tree_instance):
        class StaleNode:
            @property
            def CachedIsOffscreen(self):
                raise COMError(
                    -2147220991,
                    "An event was unable to invoke any of the subscribers",
                    (None, None, None, 0, None),
                )

        tree_instance.tree_traversal(
            StaleNode(),
            Rect(0, 0, 200, 200),
            "Window",
            False,
            [],
            [],
            [],
            [],
        )


def _make_button_child(name: str, left: int) -> MagicMock:
    child = MagicMock()
    child.CachedIsOffscreen = False
    child.CachedControlTypeName = "ButtonControl"
    child.CachedIsControlElement = True
    child.CachedBoundingRectangle = Rect(left, 10, left + 40, 60)
    child.CachedIsEnabled = True
    child.CachedHasKeyboardFocus = False
    child.CachedName = name
    child.CachedLocalizedControlType = "button"
    child.CachedAcceleratorKey = ""
    child.CachedHelpText = ""
    child.GetCachedPropertyValue.return_value = 43
    return child


def _make_text_child(text: str, left: int) -> MagicMock:
    child = MagicMock()
    child.CachedIsOffscreen = False
    child.CachedControlTypeName = "TextControl"
    child.CachedIsControlElement = True
    child.CachedBoundingRectangle = Rect(left, 10, left + 40, 60)
    child.CachedIsEnabled = True
    child.CachedIsKeyboardFocusable = False
    child.CachedName = text
    return child


def _make_pane_parent() -> MagicMock:
    parent = MagicMock()
    parent.CachedIsOffscreen = False
    parent.CachedControlTypeName = "PaneControl"
    parent.CachedIsControlElement = True
    parent.CachedBoundingRectangle = Rect(0, 0, 500, 500)
    parent.CachedIsEnabled = True
    parent.CachedIsKeyboardFocusable = False
    parent.CachedName = ""
    # Disable the scrollable-container branch — irrelevant to this test and
    # would otherwise call random_point_within_bounding_box on an unconfigured mock.
    parent.GetCachedPattern.return_value = None
    return parent


class TestElementBudgetStopsTraversal:
    """A huge flat list/grid (e.g. thousands of UIA rows) must not be walked in full —
    see budget.py. These tests exercise the wiring in tree_traversal / get_window_wise_nodes.
    """

    def test_stops_appending_and_recursing_once_budget_exhausted(
        self, tree_instance, monkeypatch
    ):
        parent = _make_pane_parent()
        children = [_make_button_child(f"btn{i}", 10 * i) for i in range(5)]

        def fake_get_children(node, cache_request):
            return children if node is parent else []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        tree_instance.element_budget = TreeElementBudget(limit=3)

        interactive_nodes = []
        tree_instance.tree_traversal(
            parent,
            Rect(0, 0, 500, 500),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
        )

        assert len(interactive_nodes) == 3
        assert tree_instance.element_budget.truncated is True
        assert tree_instance.element_budget.count == 3

    def test_all_elements_captured_when_under_budget(self, tree_instance, monkeypatch):
        parent = _make_pane_parent()
        children = [_make_button_child(f"btn{i}", 10 * i) for i in range(5)]

        def fake_get_children(node, cache_request):
            return children if node is parent else []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        tree_instance.element_budget = TreeElementBudget(limit=10)

        interactive_nodes = []
        tree_instance.tree_traversal(
            parent,
            Rect(0, 0, 500, 500),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
        )

        assert len(interactive_nodes) == 5
        assert tree_instance.element_budget.truncated is False

    def test_dom_informative_text_nodes_consume_budget(self, tree_instance, monkeypatch):
        # Text-heavy DOM pages must not bypass the budget just because the nodes
        # are informative rather than interactive — see PR review finding on
        # unbudgeted `dom_informative_nodes.append(TextElementNode(...))`.
        parent = _make_pane_parent()
        children = [_make_text_child(f"paragraph {i}", 10 * i) for i in range(5)]

        def fake_get_children(node, cache_request):
            return children if node is parent else []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )

        tree_instance.element_budget = TreeElementBudget(limit=3)

        dom_informative_nodes = []
        tree_instance.tree_traversal(
            parent,
            Rect(0, 0, 500, 500),
            "Window",
            True,
            [],
            [],
            [],
            dom_informative_nodes,
            is_dom=True,
        )

        assert len(dom_informative_nodes) == 3
        assert tree_instance.element_budget.truncated is True
        assert tree_instance.element_budget.count == 3

    def test_get_window_wise_nodes_skips_remaining_windows_once_exhausted(
        self, tree_instance, monkeypatch
    ):
        tree_instance.element_budget = TreeElementBudget(limit=1)
        tree_instance.element_budget.try_consume(1)
        assert tree_instance.element_budget.exhausted is True

        # `tree_instance.desktop` is a weakref.proxy to a mock that only lives for the
        # duration of the fixture — swap in a plain, still-alive mock before touching it.
        live_desktop = MagicMock()
        live_desktop.is_window_browser.return_value = False
        tree_instance.desktop = live_desktop

        calls = []
        monkeypatch.setattr(
            "windows_mcp.tree.service.ControlFromHandle",
            lambda handle: MagicMock(ClassName="SomeWindow", Name="Some Window"),
        )

        def fake_get_nodes(self, handle, is_browser=False, wait_time=0, use_dom=False):
            calls.append(handle)
            return ([], [], [], None)

        monkeypatch.setattr(Tree, "get_nodes", fake_get_nodes)

        tree_instance.get_window_wise_nodes(
            windows_handles=[111, 222, 333], active_window_flag=False
        )

        assert calls == []


def _with_runtime_id(control: MagicMock, runtime_id: tuple[int, ...]) -> MagicMock:
    """Report `runtime_id` from the cached RuntimeId property, keeping the role default."""

    def get_cached_property(property_id):
        if property_id == PropertyId.RuntimeIdProperty:
            return list(runtime_id)
        return 43

    control.GetCachedPropertyValue.side_effect = get_cached_property
    return control


class TestCyclicRuntimeIdGuard:
    """A provider can hand back a child whose RuntimeId equals one of its own ancestors.

    The element budget cannot bound that walk, because a cyclic pane appends no output
    nodes and so never trips the limit — see budget.py.
    """

    def test_unbounded_cycle_terminates_without_losing_its_siblings(
        self, tree_instance, monkeypatch
    ):
        """The reported shape: a pane keeps handing back a child carrying its own id.

        Unguarded this recurses until RecursionError and Snapshot never returns.
        """
        select = _with_runtime_id(_make_pane_parent(), (1, 100))
        echo = _with_runtime_id(_make_pane_parent(), (1, 100))
        option = _with_runtime_id(_make_button_child("option", 60), (1, 200))

        def fake_get_children(node, cache_request):
            if node is select:
                return [echo, option]
            if node is echo:
                return [echo]
            return []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        interactive_nodes = []
        tree_instance.tree_traversal(
            select,
            Rect(0, 0, 500, 500),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
        )

        assert {node.name for node in interactive_nodes} == {"option"}

    def test_repeated_id_in_separate_branches_is_not_cut(self, tree_instance, monkeypatch):
        """Siblings may legitimately share an id; only an ancestor repeat is a cycle."""
        parent = _with_runtime_id(_make_pane_parent(), (1, 100))
        first = _with_runtime_id(_make_pane_parent(), (1, 200))
        second = _with_runtime_id(_make_pane_parent(), (1, 200))
        first_child = _with_runtime_id(_make_button_child("first", 10), (1, 300))
        second_child = _with_runtime_id(_make_button_child("second", 60), (1, 301))

        def fake_get_children(node, cache_request):
            if node is parent:
                return [first, second]
            if node is first:
                return [first_child]
            if node is second:
                return [second_child]
            return []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        interactive_nodes = []
        tree_instance.tree_traversal(
            parent,
            Rect(0, 0, 500, 500),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
        )

        assert {node.name for node in interactive_nodes} == {"first", "second"}

    def test_unreadable_runtime_id_leaves_traversal_unchanged(self, tree_instance, monkeypatch):
        """Providers that expose no usable RuntimeId keep walking exactly as before."""
        parent = _make_pane_parent()
        children = [_make_button_child(f"btn{i}", 10 * i) for i in range(5)]

        def fake_get_children(node, cache_request):
            return children if node is parent else []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            fake_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        interactive_nodes = []
        tree_instance.tree_traversal(
            parent,
            Rect(0, 0, 500, 500),
            "Window",
            False,
            interactive_nodes,
            [],
            [],
            [],
        )

        assert len(interactive_nodes) == 5


def _traversal_recorder(
    tree_instance, monkeypatch, process_names, excluded, forbidden_uia=frozenset()
):
    """Wire the stubs shared by the `WINDOWS_MCP_EXCLUDE_PROCESSES` tests.

    Args:
        tree_instance: The `Tree` fixture under test.
        monkeypatch: The pytest `monkeypatch` fixture.
        process_names: Mapping of HWND to the process name a lookup should report;
            a missing key resolves to None, mirroring an unresolvable owner.
        excluded: Value for `WINDOWS_MCP_EXCLUDE_PROCESSES`, or None to leave it unset.
        forbidden_uia: Handles whose accessibility provider must never be entered.
            Any invocation of `ControlFromHandle` for them is recorded and then
            raises.

    Returns:
        A namespace with the handles passed to `get_nodes` (`traversed`), every
        handle `ControlFromHandle` was invoked for (`uia_attempts` — the entry
        point into a window's accessibility provider), and the handles whose
        process name was resolved (`lookups`).
    """
    live_desktop = MagicMock()
    live_desktop.is_window_browser.return_value = False
    # `tree_instance.desktop` is a weakref.proxy to a mock that only lives for the
    # duration of the fixture — swap in a plain, still-alive mock before touching it.
    tree_instance.desktop = live_desktop

    # Keep the real host-process chain of the test runner out of these tests.
    monkeypatch.setattr("windows_mcp.tree.service._host_process_ids", lambda: frozenset())

    if excluded is None:
        monkeypatch.delenv("WINDOWS_MCP_EXCLUDE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("WINDOWS_MCP_EXCLUDE_PROCESSES", excluded)

    lookups = []

    def fake_window_process_name(handle):
        lookups.append(handle)
        return process_names.get(handle)

    monkeypatch.setattr("windows_mcp.tree.service._window_process_name", fake_window_process_name)

    uia_attempts = []

    def fake_control_from_handle(handle):
        # Recorded *before* the forbidden check: get_window_wise_nodes wraps this
        # call in a broad `except Exception`, so a raise on its own would be
        # swallowed and a forbidden handle would leave no trace in the test.
        uia_attempts.append(handle)
        if handle in forbidden_uia:
            raise AssertionError(f"UIA touched excluded process (handle {handle})")
        return MagicMock(ClassName="SomeWindow", Name="Some Window")

    monkeypatch.setattr("windows_mcp.tree.service.ControlFromHandle", fake_control_from_handle)

    traversed = []

    def fake_get_nodes(self, handle, is_browser=False, wait_time=0, use_dom=False):
        traversed.append(handle)
        return ([], [], [], None)

    monkeypatch.setattr(Tree, "get_nodes", fake_get_nodes)

    return SimpleNamespace(traversed=traversed, uia_attempts=uia_attempts, lookups=lookups)


class TestExcludedProcessNames:
    """Parsing and normalization of `WINDOWS_MCP_EXCLUDE_PROCESSES`."""

    def test_unset_yields_no_exclusions(self):
        assert _excluded_process_names({}) == set()

    def test_blank_entries_are_ignored(self):
        assert _excluded_process_names({"WINDOWS_MCP_EXCLUDE_PROCESSES": "  ,  ,"}) == set()

    def test_strips_whitespace_and_casefolds(self):
        parsed = _excluded_process_names(
            {"WINDOWS_MCP_EXCLUDE_PROCESSES": " Code.exe , CURSOR.EXE ,, "}
        )

        assert parsed == {"code.exe", "cursor.exe"}

    def test_duplicate_names_are_harmless(self):
        parsed = _excluded_process_names(
            {"WINDOWS_MCP_EXCLUDE_PROCESSES": "Code.exe,code.exe,CODE.EXE"}
        )

        assert parsed == {"code.exe"}


class TestWindowProcessName:
    """HWND -> process name is resolved without going through UI Automation."""

    def test_returns_the_owning_process_name(self, monkeypatch):
        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId",
            lambda handle: (1234, 4242),
        )
        process = MagicMock()
        process.name.return_value = "Code.exe"
        monkeypatch.setattr("windows_mcp.tree.service.Process", lambda pid: process)

        assert _window_process_name(100) == "Code.exe"

    def test_returns_none_without_a_pid(self, monkeypatch):
        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId",
            lambda handle: (1234, 0),
        )

        assert _window_process_name(100) is None

    def test_returns_none_when_the_lookup_raises(self, monkeypatch):
        def explode(handle):
            raise OSError("process is gone")

        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId", explode
        )

        assert _window_process_name(100) is None


class TestProcessExclusion:
    """`WINDOWS_MCP_EXCLUDE_PROCESSES` keeps UIA out of the listed processes (#383)."""

    def test_unset_env_traverses_every_window_unchanged(self, tree_instance, monkeypatch):
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "notepad.exe"},
            excluded=None,
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.traversed == [100, 200]
        assert recorded.uia_attempts == [100, 200]
        # Unset means no process lookup is attempted at all — no added per-window cost.
        assert recorded.lookups == []

    def test_excluded_process_is_skipped(self, tree_instance, monkeypatch):
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe"},
            excluded="Code.exe",
            forbidden_uia={100},
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100], active_window_flag=False)

        assert recorded.traversed == []
        assert recorded.uia_attempts == []

    def test_exclusion_happens_before_uia_is_touched(self, tree_instance, monkeypatch):
        """The key regression test for the safety requirement.

        `ControlFromHandle` is the entry point into a window's accessibility
        provider, so it raises for the excluded HWND. Passing this test proves that
        provider was never entered; a guard placed after the call would fail here.
        """
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "notepad.exe"},
            excluded="Code.exe",
            forbidden_uia={100},
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.uia_attempts == [200]

    def test_mixed_windows_traverse_only_the_allowed_one(self, tree_instance, monkeypatch):
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "notepad.exe"},
            excluded="Code.exe",
            forbidden_uia={100},
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.traversed == [200]

    def test_matching_is_case_insensitive_and_whitespace_tolerant(self, tree_instance, monkeypatch):
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "Cursor.exe", 300: "notepad.exe"},
            excluded=" Code.exe , CURSOR.EXE ,, ",
            forbidden_uia={100, 200},
        )

        tree_instance.get_window_wise_nodes(
            windows_handles=[100, 200, 300], active_window_flag=False
        )

        assert recorded.traversed == [300]

    def test_matching_is_exact_basename_only(self, tree_instance, monkeypatch):
        """No wildcards and no substring matching."""
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe.bak", 200: "NotCode.exe"},
            excluded="Code.exe",
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.traversed == [100, 200]

    def test_lookup_failure_fails_open(self, tree_instance, monkeypatch):
        """An unresolvable owner must not silently drop the window."""
        recorded = _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: None, 200: "notepad.exe"},
            excluded="Code.exe",
        )

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.traversed == [100, 200]
        assert recorded.uia_attempts == [100, 200]

    def test_skipped_window_is_not_reported_as_failed(self, tree_instance, monkeypatch):
        """A skip is not a failure, and it spends none of the retry attempts."""
        attempts = []
        _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "notepad.exe"},
            excluded="Code.exe",
        )
        # Retries sleep between attempts; irrelevant here and slow in a test.
        monkeypatch.setattr("windows_mcp.tree.service.sleep", lambda seconds: None)

        def failing_get_nodes(self, handle, is_browser=False, wait_time=0, use_dom=False):
            attempts.append(handle)
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(Tree, "get_nodes", failing_get_nodes)

        _, _, _, failed_handles, _ = tree_instance.get_window_wise_nodes(
            windows_handles=[100, 200], active_window_flag=False
        )

        assert failed_handles == [200]
        # Every attempt belongs to the traversed window; the skipped one took none.
        assert attempts == [200] * (THREAD_MAX_RETRIES + 1)

    def test_skipped_window_consumes_no_element_budget(self, tree_instance, monkeypatch):
        traversed = []
        tree_instance.element_budget = TreeElementBudget(limit=10)
        _traversal_recorder(
            tree_instance,
            monkeypatch,
            process_names={100: "Code.exe", 200: "notepad.exe"},
            excluded="Code.exe",
        )

        def budgeting_get_nodes(self, handle, is_browser=False, wait_time=0, use_dom=False):
            traversed.append(handle)
            self.element_budget.try_consume(1)
            return ([], [], [], None)

        monkeypatch.setattr(Tree, "get_nodes", budgeting_get_nodes)

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert traversed == [200]
        assert tree_instance.element_budget.count == 1


def _word_run(words: list[str], left: int = 100, top: int = 0, gap: int = 0, glyph: int = 16):
    """Lay `words` out left-to-right on one line, `glyph` px per character."""
    elements = []
    for word in words:
        right = left + glyph * len(word)
        elements.append((word, Rect(left, top, right, top + 16)))
        left = right + gap
    return elements


def _as_tuples(elements):
    return [(word, (r.left, r.top, r.right, r.bottom)) for word, r in elements]


class TestMergeUnspacedWordRuns:
    """#495: Chromium/Electron expose one UIA word range per CJK glyph."""

    def test_cjk_glyphs_and_trailing_punctuation_merge_into_one_run(self):
        merged = _merge_unspaced_word_runs(_word_run(["基", "本", "设", "置", "。"]))
        assert _as_tuples(merged) == [("基本设置。", (100, 0, 180, 16))]

    def test_latin_words_are_left_unchanged(self):
        elements = _word_run(["Hello", "world", "."])
        assert _as_tuples(_merge_unspaced_word_runs(elements)) == _as_tuples(elements)

    def test_cjk_followed_by_latin_stays_split(self):
        merged = _merge_unspaced_word_runs(_word_run(["中", "文", "abc"]))
        assert [word for word, _ in merged] == ["中文", "abc"]

    def test_opening_punctuation_joins_cjk(self):
        merged = _merge_unspaced_word_runs(_word_run(["(", "中", ")"]))
        assert [word for word, _ in merged] == ["(中)"]

    def test_separate_lines_are_not_joined(self):
        elements = _word_run(["中", "文"]) + _word_run(["下", "行"], top=30)
        merged = _merge_unspaced_word_runs(elements)
        assert _as_tuples(merged) == [
            ("中文", (100, 0, 132, 16)),
            ("下行", (100, 30, 132, 46)),
        ]

    def test_wrapped_word_boxes_on_different_lines_stay_separate(self):
        elements = [("中文", Rect(300, 0, 332, 16)), ("中文", Rect(0, 20, 16, 36))]
        assert len(_merge_unspaced_word_runs(elements)) == 2

    def test_distant_glyphs_on_the_same_line_stay_separate(self):
        merged = _merge_unspaced_word_runs(_word_run(["中", "文"], gap=20))
        assert [word for word, _ in merged] == ["中", "文"]

    def test_input_rects_are_not_mutated(self):
        elements = _word_run(["中", "文"])
        before = _as_tuples(elements)
        _merge_unspaced_word_runs(elements)
        assert _as_tuples(elements) == before


class TestAppendWordNodes:
    def test_cjk_run_becomes_a_single_word_node(self, tree_instance):
        semantic_root = SemanticNode(
            control_type="Document",
            element_type="interactive",
            name="Settings",
            window_name="App",
        )
        interactive_nodes = []

        tree_instance._append_word_nodes(
            _word_run(["基", "本", "设", "置"]) + _word_run(["Save"], left=200),
            Rect(0, 0, 500, 500),
            "App",
            interactive_nodes,
            semantic_root,
        )

        assert [node.name for node in interactive_nodes] == ["基本设置", "Save"]
        box = interactive_nodes[0].bounding_box
        assert (box.left, box.top, box.right, box.bottom) == (100, 0, 164, 16)
        assert [child.name for child in semantic_root.children] == ["基本设置", "Save"]
        assert all(child.control_type == "Word" for child in semantic_root.children)
        assert tree_instance.element_budget.count == 2
class _FakeParent:
    """Stand-in for a psutil parent process."""

    def __init__(self, pid: int, name: str) -> None:
        self.pid = pid
        self._name = name

    def name(self) -> str:
        return self._name


class TestHostProcessIds:
    """The MCP host's process chain, read from psutil without UI Automation (#383)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _host_process_ids.cache_clear()
        yield
        _host_process_ids.cache_clear()

    def _patch_parents(self, monkeypatch, parents, windowed=frozenset()):
        process = MagicMock()
        process.parents.return_value = parents
        monkeypatch.setattr("windows_mcp.tree.service.Process", lambda pid: process)
        monkeypatch.setattr(
            "windows_mcp.tree.service._pids_with_visible_windows", lambda: frozenset(windowed)
        )

    def test_stops_after_the_first_ancestor_that_owns_a_window(self, monkeypatch):
        """A browser that launched VS Code (vscode:// link) is not part of the host."""
        self._patch_parents(
            monkeypatch,
            [
                _FakeParent(10, "uv.exe"),
                _FakeParent(20, "claude.exe"),
                _FakeParent(30, "Code.exe"),
                _FakeParent(40, "chrome.exe"),
                _FakeParent(50, "explorer.exe"),
            ],
            windowed={30, 40, 50},
        )

        assert _host_process_ids() == frozenset({10, 20, 30})

    def test_collects_ancestors_up_to_the_shell(self, monkeypatch):
        self._patch_parents(
            monkeypatch,
            [
                _FakeParent(10, "uv.exe"),
                _FakeParent(20, "Code.exe"),
                _FakeParent(30, "explorer.exe"),
                _FakeParent(40, "svchost.exe"),
            ],
        )

        assert _host_process_ids() == frozenset({10, 20})

    def test_never_includes_explorer_or_system_processes(self, monkeypatch):
        self._patch_parents(
            monkeypatch,
            [_FakeParent(30, "EXPLORER.EXE"), _FakeParent(20, "Code.exe")],
        )

        assert _host_process_ids() == frozenset()

    def test_unreadable_chain_yields_no_host(self, monkeypatch):
        def explode(pid):
            raise RuntimeError("access denied")

        monkeypatch.setattr("windows_mcp.tree.service.Process", explode)

        assert _host_process_ids() == frozenset()

    def test_unreadable_parent_name_stops_the_walk(self, monkeypatch):
        broken = MagicMock(pid=20)
        broken.name.side_effect = RuntimeError("gone")
        self._patch_parents(monkeypatch, [_FakeParent(10, "uv.exe"), broken])

        assert _host_process_ids() == frozenset({10})


class TestSkipHostProcessesEnabled:
    def test_on_by_default(self):
        assert _skip_host_processes_enabled({}) is True

    def test_explicit_off_values_disable(self):
        for raw in ("0", "false", "No", " OFF "):
            assert _skip_host_processes_enabled({"WINDOWS_MCP_SKIP_HOST_PROCESSES": raw}) is False

    def test_other_values_keep_it_on(self):
        assert _skip_host_processes_enabled({"WINDOWS_MCP_SKIP_HOST_PROCESSES": "1"}) is True


class TestWindowProcessId:
    def test_returns_the_owning_pid(self, monkeypatch):
        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId",
            lambda handle: (1, 4242),
        )
        assert _window_process_id(100) == 4242

    def test_zero_pid_is_none(self, monkeypatch):
        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId",
            lambda handle: (0, 0),
        )
        assert _window_process_id(100) is None

    def test_lookup_failure_is_none(self, monkeypatch):
        def explode(handle):
            raise OSError("invalid window handle")

        monkeypatch.setattr(
            "windows_mcp.tree.service.win32process.GetWindowThreadProcessId", explode
        )
        assert _window_process_id(100) is None


class TestHostProcessSkip:
    """Windows of the MCP host (e.g. VS Code) are never entered by default (#383)."""

    def _wire(self, tree_instance, monkeypatch, host_pids, window_pids):
        recorded = _traversal_recorder(tree_instance, monkeypatch, process_names={}, excluded=None)
        monkeypatch.setattr("windows_mcp.tree.service._host_process_ids", lambda: host_pids)
        monkeypatch.setattr(
            "windows_mcp.tree.service._window_process_id", lambda handle: window_pids.get(handle)
        )
        return recorded

    def test_host_window_is_skipped_before_uia(self, tree_instance, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SKIP_HOST_PROCESSES", raising=False)
        recorded = self._wire(tree_instance, monkeypatch, frozenset({42}), {100: 42, 200: 7})

        _, _, _, failed_handles, _ = tree_instance.get_window_wise_nodes(
            windows_handles=[100, 200], active_window_flag=False
        )

        assert recorded.uia_attempts == [200]
        assert recorded.traversed == [200]
        assert failed_handles == []

    def test_opt_out_traverses_host_windows(self, tree_instance, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_SKIP_HOST_PROCESSES", "false")
        recorded = self._wire(tree_instance, monkeypatch, frozenset({42}), {100: 42, 200: 7})

        tree_instance.get_window_wise_nodes(windows_handles=[100, 200], active_window_flag=False)

        assert recorded.traversed == [100, 200]

    def test_unresolvable_window_owner_is_traversed(self, tree_instance, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SKIP_HOST_PROCESSES", raising=False)
        recorded = self._wire(tree_instance, monkeypatch, frozenset({42}), {})

        tree_instance.get_window_wise_nodes(windows_handles=[100], active_window_flag=False)

        assert recorded.traversed == [100]


class _SteppingClock:
    """A clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestPerWindowBudget:
    """One slow or enormous window must not use up the whole capture (#383)."""

    def test_time_budget_stops_a_slow_window_and_keeps_its_partial_nodes(
        self, tree_instance, monkeypatch
    ):
        clock = _SteppingClock()
        parent = _make_pane_parent()
        children = [_make_button_child(f"btn{i}", 10 * i) for i in range(5)]

        def slow_get_children(node, cache_request):
            if node is parent:
                return children
            # Each child's subtree takes 3 s, as a sluggish provider would.
            clock.now += 3.0
            return []

        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            slow_get_children,
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        tree_instance.element_budget = TreeElementBudget(limit=100, clock=clock)
        tree_instance.element_budget.begin_window(max_seconds=5.0)

        interactive_nodes = []
        tree_instance.tree_traversal(
            parent, Rect(0, 0, 500, 500), "Window", False, interactive_nodes, [], [], []
        )

        # Children 1 and 2 fit in the budget (t=3, t=6 >= 5 stops the loop).
        assert len(interactive_nodes) == 2
        assert tree_instance.element_budget.end_window() == WINDOW_CUT_TIME
        # A per-window cut is not the capture-wide truncation the output note reports.
        assert tree_instance.element_budget.truncated is False

    def test_window_cap_does_not_skip_later_windows(self, tree_instance, monkeypatch):
        live_desktop = MagicMock()
        live_desktop.is_window_browser.return_value = False
        tree_instance.desktop = live_desktop
        monkeypatch.delenv("WINDOWS_MCP_EXCLUDE_PROCESSES", raising=False)
        monkeypatch.setattr("windows_mcp.tree.service._host_process_ids", lambda: frozenset())

        parents = {111: _make_pane_parent(), 222: _make_pane_parent()}
        for parent in parents.values():
            parent.Name = "Some Window"
            parent.ClassName = "SomeWindow"
            parent.BoundingRectangle = Rect(0, 0, 500, 500)
        children = {
            id(parent): [_make_button_child(f"{handle}-btn{i}", 10 * i) for i in range(5)]
            for handle, parent in parents.items()
        }

        monkeypatch.setattr(
            "windows_mcp.tree.service.ControlFromHandle", lambda handle: parents[handle]
        )
        monkeypatch.setattr(
            "windows_mcp.tree.service.CacheRequestFactory.create_tree_traversal_cache",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "windows_mcp.tree.service.CachedControlHelper.get_cached_children",
            lambda node, cache_request: children.get(id(node), []),
        )
        monkeypatch.setattr("windows_mcp.tree.service.AccessibleRoleNames", {43: "PushButton"})

        tree_instance.element_budget = TreeElementBudget(limit=100)
        tree_instance.max_window_elements = 2
        tree_instance.window_time_budget = None

        interactive_nodes, _, _, failed_handles, _ = tree_instance.get_window_wise_nodes(
            windows_handles=[111, 222], active_window_flag=False
        )

        names = [node.name for node in interactive_nodes]
        assert len([name for name in names if name.startswith("111-")]) == 2
        assert len([name for name in names if name.startswith("222-")]) == 2
        assert failed_handles == []
        assert tree_instance.element_budget.count == 4
        assert tree_instance.element_budget.truncated is False
