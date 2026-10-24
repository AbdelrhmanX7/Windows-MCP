from windows_mcp.tree.budget import (
    DEFAULT_MAX_TREE_ELEMENTS,
    DEFAULT_WINDOW_TIME_BUDGET_SEC,
    WINDOW_CUT_ELEMENTS,
    WINDOW_CUT_TIME,
    TreeElementBudget,
    resolve_max_tree_elements,
    resolve_max_window_elements,
    resolve_window_time_budget,
)


class TestTreeElementBudget:
    def test_starts_unexhausted(self):
        budget = TreeElementBudget(limit=3)
        assert budget.exhausted is False
        assert budget.truncated is False
        assert budget.remaining == 3

    def test_try_consume_under_limit(self):
        budget = TreeElementBudget(limit=3)
        assert budget.try_consume() is True
        assert budget.count == 1
        assert budget.truncated is False

    def test_try_consume_reaching_limit_marks_truncated(self):
        budget = TreeElementBudget(limit=2)
        assert budget.try_consume() is True
        assert budget.try_consume() is True
        assert budget.exhausted is True
        assert budget.truncated is True

    def test_try_consume_beyond_limit_returns_false(self):
        budget = TreeElementBudget(limit=1)
        assert budget.try_consume() is True
        assert budget.try_consume() is False
        assert budget.count == 1
        assert budget.truncated is True

    def test_try_consume_batch_exceeding_remaining(self):
        budget = TreeElementBudget(limit=5)
        assert budget.try_consume(3) is True
        assert budget.try_consume(10) is False
        assert budget.count == 5
        assert budget.truncated is True

    def test_try_consume_zero_amount_does_not_change_count(self):
        budget = TreeElementBudget(limit=5)
        assert budget.try_consume(0) is True
        assert budget.count == 0

    def test_limit_is_clamped_to_at_least_one(self):
        budget = TreeElementBudget(limit=0)
        assert budget.limit == 1

    def test_remaining_never_negative(self):
        budget = TreeElementBudget(limit=2)
        budget.try_consume(10)
        assert budget.remaining == 0
        assert budget.count == 2


class TestResolveMaxTreeElements:
    def test_default_when_unset(self):
        assert resolve_max_tree_elements({}) == DEFAULT_MAX_TREE_ELEMENTS

    def test_default_when_blank(self):
        assert (
            resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "  "})
            == DEFAULT_MAX_TREE_ELEMENTS
        )

    def test_valid_override(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "150"}) == 150

    def test_invalid_value_falls_back_to_default(self):
        assert (
            resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "not-a-number"})
            == DEFAULT_MAX_TREE_ELEMENTS
        )

    def test_zero_falls_back_to_default(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "0"}) == DEFAULT_MAX_TREE_ELEMENTS

    def test_negative_falls_back_to_default(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "-5"}) == DEFAULT_MAX_TREE_ELEMENTS


class FakeClock:
    """A manually advanced clock for deterministic deadline tests."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestWindowScope:
    """Per-window time/element scope that keeps one window from eating a capture (#383)."""

    def test_no_window_scope_behaves_like_global_budget(self):
        budget = TreeElementBudget(limit=3)
        assert budget.window_exhausted is False
        assert budget.window_remaining is None
        assert budget.try_consume(3) is True
        assert budget.exhausted is True

    def test_deadline_exhausts_window_without_marking_truncated(self):
        clock = FakeClock()
        budget = TreeElementBudget(limit=100, clock=clock)
        budget.begin_window(max_seconds=2.0)
        assert budget.exhausted is False
        assert budget.try_consume() is True
        clock.now += 2.0
        assert budget.exhausted is True
        assert budget.try_consume() is False
        assert budget.count == 1
        assert budget.truncated is False
        assert budget.global_exhausted is False
        assert budget.end_window() == WINDOW_CUT_TIME

    def test_element_cap_exhausts_window(self):
        budget = TreeElementBudget(limit=100)
        budget.begin_window(max_elements=2)
        assert budget.try_consume() is True
        assert budget.try_consume() is True
        assert budget.exhausted is True
        assert budget.try_consume() is False
        assert budget.count == 2
        assert budget.truncated is False
        assert budget.end_window() == WINDOW_CUT_ELEMENTS

    def test_batch_over_window_cap_is_clipped_to_the_cap(self):
        budget = TreeElementBudget(limit=100)
        budget.begin_window(max_elements=3)
        assert budget.try_consume(10) is False
        assert budget.count == 3
        assert budget.truncated is False
        assert budget.end_window() == WINDOW_CUT_ELEMENTS

    def test_global_limit_still_marks_truncated_inside_a_window(self):
        budget = TreeElementBudget(limit=2)
        budget.begin_window(max_elements=10, max_seconds=60.0)
        assert budget.try_consume(5) is False
        assert budget.count == 2
        assert budget.truncated is True

    def test_end_window_restores_global_only_checks(self):
        clock = FakeClock()
        budget = TreeElementBudget(limit=100, clock=clock)
        budget.begin_window(max_elements=1, max_seconds=1.0)
        budget.try_consume()
        clock.now += 5.0
        assert budget.exhausted is True
        budget.end_window()
        assert budget.exhausted is False
        assert budget.try_consume() is True

    def test_each_window_gets_a_fresh_scope(self):
        clock = FakeClock()
        budget = TreeElementBudget(limit=100, clock=clock)
        budget.begin_window(max_elements=2, max_seconds=1.0)
        budget.try_consume(2)
        clock.now += 5.0
        assert budget.exhausted is True
        assert budget.end_window() == WINDOW_CUT_ELEMENTS
        budget.begin_window(max_elements=2, max_seconds=1.0)
        assert budget.exhausted is False
        assert budget.try_consume(2) is True
        assert budget.count == 4
        # Filling the cap exactly, with nothing left unvisited, is not a cut.
        assert budget.end_window() is None

    def test_uncut_window_reports_no_reason(self):
        budget = TreeElementBudget(limit=100, clock=FakeClock())
        budget.begin_window(max_elements=5, max_seconds=5.0)
        budget.try_consume()
        assert budget.end_window() is None

    def test_none_and_zero_disable_window_limits(self):
        clock = FakeClock()
        budget = TreeElementBudget(limit=100, clock=clock)
        budget.begin_window(max_elements=0, max_seconds=None)
        clock.now += 1000.0
        assert budget.try_consume(50) is True
        assert budget.end_window() is None


class TestResolveWindowTimeBudget:
    def test_default_when_unset(self):
        assert resolve_window_time_budget({}) == DEFAULT_WINDOW_TIME_BUDGET_SEC

    def test_default_when_blank(self):
        env = {"WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC": "  "}
        assert resolve_window_time_budget(env) == DEFAULT_WINDOW_TIME_BUDGET_SEC

    def test_valid_override(self):
        assert resolve_window_time_budget({"WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC": "2.5"}) == 2.5

    def test_zero_disables(self):
        assert resolve_window_time_budget({"WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC": "0"}) is None

    def test_invalid_values_fall_back_to_default(self):
        for raw in ("soon", "-1", "nan", "inf"):
            env = {"WINDOWS_MCP_WINDOW_TIME_BUDGET_SEC": raw}
            assert resolve_window_time_budget(env) == DEFAULT_WINDOW_TIME_BUDGET_SEC, raw


class TestResolveMaxWindowElements:
    def test_unset_means_no_cap(self):
        assert resolve_max_window_elements({}) is None

    def test_valid_override(self):
        assert resolve_max_window_elements({"WINDOWS_MCP_MAX_WINDOW_ELEMENTS": "200"}) == 200

    def test_zero_means_no_cap(self):
        assert resolve_max_window_elements({"WINDOWS_MCP_MAX_WINDOW_ELEMENTS": "0"}) is None

    def test_invalid_or_negative_means_no_cap(self):
        for raw in ("lots", "-3", "  "):
            env = {"WINDOWS_MCP_MAX_WINDOW_ELEMENTS": raw}
            assert resolve_max_window_elements(env) is None, raw
