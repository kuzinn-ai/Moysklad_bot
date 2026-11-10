import asyncio
import importlib
from pathlib import Path
import sys
import threading

import pytest
from aiohttp import web

from main.blocking_tasks import BoundedExecutor, ExecutorBusyError, MOYSKLAD_EXECUTOR


def miniapp_server_module():
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        return importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)


def wait_for_in_flight(executor, expected):
    async def wait():
        for _ in range(1000):
            if executor.in_flight == expected:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"in_flight did not reach {expected}")

    return wait()


def test_blocking_worker_does_not_block_async_event_loop():
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    started = threading.Event()
    release = threading.Event()
    heartbeat = threading.Event()
    released_by_timer = threading.Event()

    def slow_call():
        started.set()
        release.wait(timeout=2)
        return "complete"

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(executor.run(slow_call))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        loop.call_later(0.02, heartbeat.set)

        def release_later():
            release.set()
            released_by_timer.set()

        timer = threading.Timer(0.2, release_later)
        timer.start()
        try:
            await asyncio.wait_for(asyncio.to_thread(heartbeat.wait), timeout=0.12)
            assert not released_by_timer.is_set()
            assert await task == "complete"
        finally:
            release.set()
            timer.cancel()

    asyncio.run(scenario())


def test_bounded_executor_rejects_when_running_and_queued_capacity_is_full():
    executor = BoundedExecutor(max_workers=1, max_in_flight=2)
    release = threading.Event()
    started = threading.Event()

    def blocked_call():
        started.set()
        release.wait(timeout=2)

    async def scenario():
        first = asyncio.create_task(executor.run(blocked_call))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        second = asyncio.create_task(executor.run(blocked_call))
        await wait_for_in_flight(executor, 2)
        with pytest.raises(ExecutorBusyError):
            await executor.run(lambda: "must not be submitted")
        assert executor.in_flight == 2

        release.set()
        await asyncio.gather(first, second)
        await wait_for_in_flight(executor, 0)
        assert await executor.run(lambda: "slot restored") == "slot restored"

    asyncio.run(scenario())


def test_cancelled_waiter_keeps_capacity_until_worker_really_finishes():
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    started = threading.Event()
    release = threading.Event()

    def blocked_call():
        started.set()
        release.wait(timeout=2)
        return "finished after cancellation"

    async def scenario():
        task = asyncio.create_task(executor.run(blocked_call))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert executor.in_flight == 1
        with pytest.raises(ExecutorBusyError):
            await executor.run(lambda: "must stay queued out")

        release.set()
        await wait_for_in_flight(executor, 0)
        assert await executor.run(lambda: "recovered") == "recovered"

    asyncio.run(scenario())


def test_worker_exception_also_releases_capacity():
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)

    def fail():
        raise ValueError("worker failure")

    async def scenario():
        with pytest.raises(ValueError, match="worker failure"):
            await executor.run(fail)
        await wait_for_in_flight(executor, 0)
        assert await executor.run(lambda: "recovered") == "recovered"

    asyncio.run(scenario())


def test_telegram_and_miniapp_share_capacity_and_miniapp_returns_503(monkeypatch):
    miniapp_server = miniapp_server_module()
    monkeypatch.setattr(miniapp_server, "run_blocking_task", MOYSKLAD_EXECUTOR.run)
    release = threading.Event()
    started = threading.Event()

    def blocked_call():
        started.set()
        release.wait(timeout=3)

    async def scenario():
        # These model Telegram-originated jobs using the common runner.
        jobs = [asyncio.create_task(MOYSKLAD_EXECUTOR.run(blocked_call)) for _ in range(8)]
        await wait_for_in_flight(MOYSKLAD_EXECUTOR, 8)
        assert started.is_set()
        with pytest.raises(web.HTTPServiceUnavailable) as error:
            await miniapp_server.run_moysklad_call(blocked_call)
        assert error.value.headers["Retry-After"] == "5"
        assert MOYSKLAD_EXECUTOR.in_flight == 8

        release.set()
        await asyncio.gather(*jobs)
        await wait_for_in_flight(MOYSKLAD_EXECUTOR, 0)

    asyncio.run(scenario())


def test_miniapp_worker_does_not_block_second_coroutine():
    miniapp_server = miniapp_server_module()
    started = threading.Event()
    release = threading.Event()
    heartbeat = asyncio.Event()
    release_timer_done = threading.Event()

    def slow_call():
        started.set()
        release.wait(timeout=2)

    async def scenario():
        loop = asyncio.get_running_loop()
        first_request = asyncio.create_task(miniapp_server.run_moysklad_call(slow_call))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        loop.call_later(0.02, heartbeat.set)

        def release_later():
            release.set()
            release_timer_done.set()

        timer = threading.Timer(0.2, release_later)
        timer.start()
        try:
            await asyncio.wait_for(heartbeat.wait(), timeout=0.12)
            assert not release_timer_done.is_set()
            await first_request
        finally:
            release.set()
            timer.cancel()

    asyncio.run(scenario())
