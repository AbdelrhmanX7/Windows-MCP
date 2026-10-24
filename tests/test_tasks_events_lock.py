"""Task event sinks (JSONL, webhook, history) and the FIFO screen lock."""

import asyncio
import json
import threading
import time

import pytest

from windows_mcp.tasks.events import TaskEventBus
from windows_mcp.tasks.screen_lock import ScreenLock, ScreenLockTimeout
from windows_mcp.tasks.store import TaskStore


def test_seq_ts_history_and_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    bus = TaskEventBus(jsonl_path=path)
    store = TaskStore(bus=bus)
    plan = store.create_plan(["a", "b"])
    store.update(plan.id, "t1", status="running")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in lines] == [1, 2, 3, 4]
    assert all(isinstance(e["ts"], int) and e["ts"] > 1_600_000_000_000 for e in lines)
    assert lines[-1]["type"] == "task_event" and lines[-1]["status"] == "running"
    assert [e["seq"] for e in bus.history(since=2)] == [3, 4]
    assert bus.history(plan_id="nope") == []
    assert bus.last_seq == 4


def test_webhook_posts_in_order_with_token():
    posted = []
    gate = threading.Event()

    def poster(url, *, json, headers, timeout):
        gate.wait(2)
        posted.append((url, json["seq"], headers.get("Authorization"), timeout))

    bus = TaskEventBus(
        webhook_url="https://example.invalid/hook",
        webhook_token="s3cret",
        webhook_timeout=1.5,
        poster=poster,
    )
    store = TaskStore(bus=bus)
    t0 = time.perf_counter()
    store.create_plan(["a", "b", "c"])
    # Publishing never waits for the (blocked) HTTP worker.
    assert time.perf_counter() - t0 < 0.5
    gate.set()
    assert bus.flush_webhook(5)
    assert [p[1] for p in posted] == [1, 2, 3, 4]
    assert posted[0] == ("https://example.invalid/hook", 1, "Bearer s3cret", 1.5)


def test_webhook_failures_are_swallowed():
    calls = []

    def poster(url, **kwargs):
        calls.append(kwargs["json"]["seq"])
        raise ConnectionError("backend down")

    bus = TaskEventBus(webhook_url="http://127.0.0.1:9/x", poster=poster)
    bus.publish({"type": "task_event"})
    bus.publish({"type": "task_event"})
    assert bus.flush_webhook(5) and calls == [1, 2]


def test_event_text_is_clipped():
    bus = TaskEventBus()
    event = bus.publish({"type": "task_event", "result": "y" * 10000})
    assert event["result_truncated"] is True and len(event["result"]) < 2100


class FakeSession:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_log_message(self, *, level, data, logger):
        if self.fail:
            raise BrokenPipeError
        await asyncio.sleep(0)
        self.sent.append((level, logger, data["seq"]))


@pytest.mark.asyncio
async def test_session_push_ordered_from_threads_and_dead_sessions_dropped():
    bus = TaskEventBus()
    good, bad = FakeSession(), FakeSession(fail=True)
    bus.remember(good)
    bus.remember(bad)
    threads = [threading.Thread(target=bus.publish, args=({"type": "x"},)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for _ in range(100):
        if len(good.sent) == 20:
            break
        await asyncio.sleep(0.01)
    assert [s[2] for s in good.sent] == list(range(1, 21))
    assert {s[1] for s in good.sent} == {"windows-mcp.tasks"}
    assert bus.session_count() == 1


@pytest.mark.asyncio
async def test_stateless_http_disables_push():
    bus = TaskEventBus()
    bus.configure_transport("streamable-http", stateless_http=True)
    bus.remember(FakeSession())
    assert bus.session_count() == 0
    bus.configure_transport("stdio")
    assert bus.ttl == float("inf") and bus.push_enabled


# ---------------------------------------------------------------- screen lock


@pytest.mark.asyncio
async def test_lock_is_fifo_fair():
    lock = ScreenLock()
    order = []
    await lock.acquire("holder")

    async def contender(name):
        await lock.acquire(name)
        order.append(name)
        await asyncio.sleep(0.01)
        lock.release()

    tasks = []
    for name in ("a", "b", "c", "d"):
        tasks.append(asyncio.create_task(contender(name)))
        await asyncio.sleep(0.005)  # Fix arrival order.
    assert lock.status()["queued"] == ["a", "b", "c", "d"]
    lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert order == ["a", "b", "c", "d"]
    assert not lock.locked


@pytest.mark.asyncio
async def test_lock_no_barging():
    """A newcomer must queue behind existing waiters even at the instant of release."""
    lock = ScreenLock()
    order = []
    await lock.acquire("holder")
    waiter = asyncio.create_task(lock.acquire("first"))
    await asyncio.sleep(0.01)
    lock.release()
    late = asyncio.create_task(lock.acquire("late"))
    await waiter
    order.append(lock.status()["holder"])
    lock.release()
    await late
    order.append(lock.status()["holder"])
    lock.release()
    assert order == ["first", "late"]


@pytest.mark.asyncio
async def test_lock_timeout_message_and_queue_cleanup():
    lock = ScreenLock()
    await lock.acquire("Type")
    with pytest.raises(ScreenLockTimeout, match="SCREEN_BUSY: Click waited 0.05s.*held by Type"):
        await lock.acquire("Click", timeout=0.05)
    assert lock.status()["queued"] == []
    lock.release()
    await asyncio.wait_for(lock.acquire("Scroll", timeout=0.05), 1)
    lock.release()


@pytest.mark.asyncio
async def test_lock_cancelled_waiter_leaves_queue():
    lock = ScreenLock()
    await lock.acquire("holder")
    waiter = asyncio.create_task(lock.acquire("w"))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert lock.status()["queued"] == []
    lock.release()
    assert not lock.locked


def test_lock_across_threads_and_loops():
    lock = ScreenLock()
    active, peak, done = [0], [0], []
    guard = threading.Lock()

    def worker(i):
        async def run():
            for _ in range(5):
                await lock.acquire(f"w{i}", timeout=10)
                with guard:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                await asyncio.sleep(0.002)
                with guard:
                    active[0] -= 1
                lock.release()
            done.append(i)

        asyncio.run(run())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(done) == [0, 1, 2, 3] and peak[0] == 1
