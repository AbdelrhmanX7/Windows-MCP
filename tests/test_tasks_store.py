"""Task plans: DAG validation, state machine, next-lane splitting and persistence."""

import json
import threading

import pytest

from windows_mcp.tasks.store import TaskError, TaskStore, find_cycle


class Recorder:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def grocery_plan(store):
    return store.create_plan(
        [
            {"id": "note", "title": "Write grocery note", "app": "Notepad", "needs_screen": True},
            {"id": "list", "title": "Fetch grocery list file"},
            {"id": "music", "title": "Play favorite songs", "app": "Spotify"},
            {"id": "notify", "title": "Tell the user", "depends_on": ["note", "music"]},
        ],
        title="Groceries + music",
    )


def test_auto_ids_and_defaults():
    store = TaskStore()
    plan = store.create_plan([{"title": "a"}, "b", {"title": "c", "depends_on": ["t1", "t2"]}])
    assert plan.id == "p1"
    assert list(plan.tasks) == ["t1", "t2", "t3"]
    assert plan.tasks["t3"].depends_on == ["t1", "t2"]
    assert all(t.status == "pending" for t in plan.tasks.values())
    assert store.create_plan(["x"]).id == "p2"  # Multiple plans per session.


@pytest.mark.parametrize(
    "tasks,match",
    [
        ([{"id": "a", "title": "A", "depends_on": ["zzz"]}], "unknown task 'zzz'"),
        ([{"id": "a", "title": "A", "depends_on": ["a"]}], "depends on itself"),
        (
            [
                {"id": "a", "title": "A", "depends_on": ["c"]},
                {"id": "b", "title": "B", "depends_on": ["a"]},
                {"id": "c", "title": "C", "depends_on": ["b"]},
            ],
            "dependency cycle",
        ),
        ([{"id": "a", "title": "A"}, {"id": "a", "title": "B"}], "duplicate task id"),
        ([{"id": "a"}], "non-empty title"),
        ([{"id": "a b", "title": "A"}], "must match"),
        ([{"title": "A", "bogus": 1}], "unknown fields"),
        ([], "non-empty list"),
        ([{"title": "A", "needs_screen": "maybe"}], "true or false"),
    ],
)
def test_validation_rejects(tasks, match):
    store = TaskStore()
    with pytest.raises(TaskError, match=match):
        store.create_plan(tasks)
    assert store.plans() == []  # Nothing half-created.


def test_cycle_message_names_the_loop():
    cycle = find_cycle({"a": ["b"], "b": ["c"], "c": ["a"], "d": []})
    assert cycle[0] == cycle[-1] and set(cycle) == {"a", "b", "c"}
    assert find_cycle({"a": [], "b": ["a"], "c": ["a", "b"]}) is None


def test_add_tasks_validates_against_existing():
    store = TaskStore()
    plan = grocery_plan(store)
    store.add_tasks(plan.id, [{"id": "extra", "title": "E", "depends_on": ["notify"]}])
    assert "extra" in plan.tasks
    with pytest.raises(TaskError, match="unknown task"):
        store.add_tasks(plan.id, [{"title": "bad", "depends_on": ["nope"]}])


def test_state_machine():
    store = TaskStore()
    grocery_plan(store)
    store.update(None, "note", status="running", progress="typing")
    store.update(None, "note", progress="half way")  # Progress-only update.
    task = store.update(None, "note", status="done", result="saved")
    assert task.status == "done" and task.result == "saved" and task.finished_at
    with pytest.raises(TaskError, match="terminal"):
        store.update(None, "note", status="done")
    with pytest.raises(TaskError, match="cannot go from 'done'"):
        store.update(None, "note", status="running")
    with pytest.raises(TaskError, match="status must be one of"):
        store.update(None, "list", status="exploded")
    # failed -> pending/running is a retry.
    store.update(None, "list", status="running")
    store.update(None, "list", status="failed", error="disk")
    retried = store.update(None, "list", status="running")
    assert retried.attempts == 2 and retried.error is None
    with pytest.raises(TaskError, match="no plan has a task"):
        store.update(None, "ghost", status="done")


def test_next_lanes_and_blocking():
    store = TaskStore()
    plan = grocery_plan(store)
    ready = store.next_ready(plan.id)
    assert [t["id"] for t in ready["screen"]] == ["note"]
    assert [t["id"] for t in ready["background"]] == ["list", "music"]
    assert ready["waiting"] == ["notify"] and ready["plan_status"] == "pending"

    store.update(plan.id, "note", status="running")
    ready = store.next_ready(plan.id)
    assert ready["screen"] == [] and ready["screen_busy"] == ["note"]
    assert ready["plan_status"] == "active"

    store.update(plan.id, "note", status="done")
    store.update(plan.id, "music", status="skipped")  # skipped satisfies dependents
    ready = store.next_ready(plan.id)
    assert [t["id"] for t in ready["background"]] == ["list", "notify"]

    other = grocery_plan(store)
    store.update(other.id, "music", status="failed", error="no Spotify")
    ready = store.next_ready(other.id)
    assert ready["blocked"] == ["notify"] and ready["failed"] == ["music"]
    store.update(other.id, "note", status="done")
    store.update(other.id, "list", status="done")
    assert store.next_ready(other.id)["plan_status"] == "failed"


def test_plan_status_done_and_cancel():
    store = TaskStore()
    plan = grocery_plan(store)
    for tid in ("note", "list", "music", "notify"):
        store.update(plan.id, tid, status="done")
    assert plan.status() == "done"
    second = grocery_plan(store)
    store.update(second.id, "list", status="done")
    cancelled = store.cancel(second.id)
    assert sorted(t.id for t in cancelled) == ["music", "note", "notify"]
    assert second.status() == "cancelled"
    assert store.cancel(second.id) == []  # Idempotent.


def test_clear():
    store = TaskStore()
    a, b = grocery_plan(store), grocery_plan(store)
    assert store.clear(a.id) == [a.id]
    assert [p.id for p in store.plans()] == [b.id]
    assert store.clear() == [b.id] and store.plans() == []


def test_events_in_mutation_order():
    bus = Recorder()
    store = TaskStore(bus=bus)
    plan = grocery_plan(store)
    store.update(plan.id, "note", status="running", progress="go")
    kinds = [(e["type"], e.get("task_id"), e.get("status")) for e in bus.events]
    assert kinds[0] == ("plan_event", None, None)
    assert kinds[1:5] == [("task_event", t, "pending") for t in ("note", "list", "music", "notify")]
    last = bus.events[-1]
    assert last == {
        "type": "task_event",
        "plan_id": plan.id,
        "task_id": "note",
        "title": "Write grocery note",
        "status": "running",
        "prev_status": "pending",
        "progress": "go",
        "result": None,
        "error": None,
        "needs_screen": True,
        "plan_status": "active",
    }


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "state" / "tasks.json"
    store = TaskStore(path=path)
    plan = grocery_plan(store)
    store.update(plan.id, "note", status="done", result={"file": "groceries.txt"})
    store.update(plan.id, "music", status="running")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["plans"][0]["id"] == plan.id

    reloaded = TaskStore(path=path)
    again = reloaded.get_plan(plan.id)
    assert again.title == "Groceries + music"
    assert again.tasks["note"].result == {"file": "groceries.txt"}
    # A task running when the process died is reported, not silently resumed.
    assert again.tasks["music"].status == "failed"
    assert "restarted" in again.tasks["music"].error
    assert again.tasks["notify"].depends_on == ["note", "music"]
    assert reloaded.create_plan(["next"]).id == "p2"  # Counter survives too.


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text("{not json", encoding="utf-8")
    store = TaskStore(path=path)
    assert store.plans() == []
    store.create_plan(["a"])
    assert json.loads(path.read_text(encoding="utf-8"))["plans"]


def test_thread_safe_updates():
    store = TaskStore()
    plan = store.create_plan([{"id": f"t{i}", "title": str(i)} for i in range(50)])
    errors = []

    def work(i):
        try:
            store.update(plan.id, f"t{i}", status="running")
            for n in range(20):
                store.update(plan.id, f"t{i}", progress=str(n))
            store.update(plan.id, f"t{i}", status="done")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and plan.status() == "done"


def test_large_results_are_truncated():
    store = TaskStore()
    store.create_plan(["a"])
    task = store.update(None, "t1", status="done", result="x" * 20000)
    assert len(task.result) < 9000 and "truncated" in task.result
