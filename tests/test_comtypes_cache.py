"""Regression tests for the comtypes.gen cache race (issues #357, #412).

When an MCP host launches several server instances at once, each one used to
call ``comtypes.client.GetModule("UIAutomationCore.dll")`` unguarded, so one
process could import a wrapper another was still writing and die with an
ImportError. ``safe_get_module`` serializes generation behind a cross-process
lock and regenerates a corrupted cache. ``GetModule`` is mocked throughout;
the lock itself is real (``msvcrt``) so its cross-process behavior is checked.
"""

import msvcrt
import os
import subprocess
import sys
import textwrap
import time
import types

import comtypes.gen
import pytest

from windows_mcp.uia import comtypes_cache


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = str(tmp_path / "comtypes-gen-test.lock")
    monkeypatch.setattr(comtypes_cache, "_LOCK_PATH", path)
    return path


def _lock_is_held_elsewhere(path: str) -> bool:
    """Return True if a non-blocking lock attempt on ``path`` fails."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return True
    else:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    finally:
        os.close(fd)


def _module(**attrs) -> types.ModuleType:
    module = types.ModuleType("comtypes.gen.UIAutomationClient")
    module.__dict__.update(attrs)
    return module


class TestSafeGetModule:
    def test_get_module_runs_under_the_lock(self, lock_path, monkeypatch):
        good = _module(IUIAutomation=object())
        held: list[bool] = []

        def fake_get_module(tlib):
            held.append(_lock_is_held_elsewhere(lock_path))
            return good

        monkeypatch.setattr(comtypes_cache.comtypes.client, "GetModule", fake_get_module)

        result = comtypes_cache.safe_get_module(
            "UIAutomationCore.dll", required_attr="IUIAutomation"
        )

        assert result is good
        assert held == [True]
        assert not _lock_is_held_elsewhere(lock_path)

    def test_partial_module_import_error_clears_and_regenerates(self, lock_path, monkeypatch):
        # The #412 traceback: a sibling's half-written module fails to import.
        good = _module(IUIAutomation=object())
        calls: list[str] = []
        cleared: list[bool] = []

        def fake_get_module(tlib):
            calls.append(tlib)
            if len(calls) == 1:
                raise ImportError(
                    "cannot import name 'IUIAutomationCacheRequest' from "
                    "'comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0'"
                )
            return good

        monkeypatch.setattr(comtypes_cache.comtypes.client, "GetModule", fake_get_module)
        monkeypatch.setattr(comtypes_cache, "_clear_gen_cache", lambda: cleared.append(True))

        result = comtypes_cache.safe_get_module(
            "UIAutomationCore.dll", required_attr="IUIAutomation"
        )

        assert result is good
        assert calls == ["UIAutomationCore.dll", "UIAutomationCore.dll"]
        assert cleared == [True]

    def test_module_missing_required_attr_is_regenerated(self, lock_path, monkeypatch):
        partial = _module()
        good = _module(IUIAutomation=object())
        results = iter([partial, good])
        cleared: list[bool] = []

        monkeypatch.setattr(comtypes_cache.comtypes.client, "GetModule", lambda tlib: next(results))
        monkeypatch.setattr(comtypes_cache, "_clear_gen_cache", lambda: cleared.append(True))

        result = comtypes_cache.safe_get_module(
            "UIAutomationCore.dll", required_attr="IUIAutomation"
        )

        assert result is good
        assert cleared == [True]

    def test_persistent_corruption_raises_after_one_retry(self, lock_path, monkeypatch):
        calls: list[str] = []

        def fake_get_module(tlib):
            calls.append(tlib)
            raise ImportError("still broken")

        monkeypatch.setattr(comtypes_cache.comtypes.client, "GetModule", fake_get_module)
        monkeypatch.setattr(comtypes_cache, "_clear_gen_cache", lambda: None)

        with pytest.raises(ImportError, match="still broken"):
            comtypes_cache.safe_get_module("UIAutomationCore.dll")

        assert len(calls) == 2
        assert not _lock_is_held_elsewhere(lock_path)


class TestComtypesCacheLock:
    def test_waits_for_lock_held_by_another_process(self, lock_path):
        holder = textwrap.dedent(
            """
            import sys, time
            from windows_mcp.uia import comtypes_cache
            comtypes_cache._LOCK_PATH = sys.argv[1]
            with comtypes_cache.comtypes_cache_lock():
                print("locked", flush=True)
                time.sleep(2.0)
            """
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder, lock_path],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "locked"
            assert _lock_is_held_elsewhere(lock_path)

            start = time.monotonic()
            with comtypes_cache.comtypes_cache_lock(timeout=30):
                waited = time.monotonic() - start
                # The holder releases only when it exits, so it must be gone.
                assert proc.wait(timeout=10) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

        assert waited >= 0.5

    def test_times_out_under_contention(self, lock_path, monkeypatch):
        def contended(fd, mode, nbytes):
            if mode == msvcrt.LK_LOCK:
                raise OSError(comtypes_cache.errno.EDEADLK, "Resource deadlock avoided")

        monkeypatch.setattr(comtypes_cache.msvcrt, "locking", contended)

        with pytest.raises(TimeoutError):
            with comtypes_cache.comtypes_cache_lock(timeout=0):
                pass


class TestClearGenCache:
    def test_removes_generated_modules_but_keeps_package_init(self, tmp_path, monkeypatch):
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "UIAutomationClient.py").write_text("partial = (")
        (tmp_path / "_944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0.py").write_text("")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "UIAutomationClient.cpython-314.pyc").write_bytes(b"")
        monkeypatch.setattr(comtypes.gen, "__file__", str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "comtypes.gen.FakeStale", types.ModuleType("stale"))

        comtypes_cache._clear_gen_cache()

        assert sorted(os.listdir(tmp_path)) == ["__init__.py"]
        assert "comtypes.gen.FakeStale" not in sys.modules
