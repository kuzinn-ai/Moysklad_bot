"""Bounded worker pool for synchronous integrations called by async handlers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Callable, TypeVar


T = TypeVar("T")


class ExecutorBusyError(RuntimeError):
    """Raised when the bounded worker pool has no execution/admission slot."""


class BoundedExecutor:
    """A fixed worker pool with a hard cap on running plus queued work."""

    def __init__(self, max_workers: int = 4, max_in_flight: int = 8) -> None:
        if max_workers < 1 or max_in_flight < max_workers:
            raise ValueError("max_in_flight must be at least max_workers")
        self.max_workers = max_workers
        self.max_in_flight = max_in_flight
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="moysklad-worker",
        )
        self._lock = threading.RLock()
        self._in_flight = 0
        self._closed = False
        self._shutdown_started = False
        self._shutdown_done = threading.Event()
        self._shutdown_thread = None
        self._shutdown_error = None

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def shutdown_complete(self) -> bool:
        return self._shutdown_done.is_set() and self._shutdown_error is None

    def _release_slot(self, _future) -> None:
        # This callback runs only when the underlying thread task has ended or
        # was cancelled before it started, never when its async waiter cancels.
        with self._lock:
            self._in_flight -= 1

    @staticmethod
    def _consume_unawaited_result(future: asyncio.Future) -> None:
        # Cancellation of the caller leaves the shielded worker running. Read
        # a later exception so it does not become an unobserved-future warning.
        if not future.cancelled():
            future.exception()

    def close_admission(self) -> None:
        """Reject future work without waiting for already accepted jobs."""
        with self._lock:
            self._closed = True

    async def run(self, function: Callable[..., T], *args) -> T:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed or self._in_flight >= self.max_in_flight:
                raise ExecutorBusyError("Сервис занят; повторите запрос позже.")
            self._in_flight += 1
            try:
                # Keep admission and submission atomic with shutdown. RLock is
                # needed because add_done_callback may run inline if the task
                # has already finished.
                worker_future = self._executor.submit(function, *args)
                worker_future.add_done_callback(self._release_slot)
            except BaseException:
                self._in_flight -= 1
                raise

        async_future = asyncio.wrap_future(worker_future, loop=loop)
        async_future.add_done_callback(self._consume_unawaited_result)
        return await asyncio.shield(async_future)

    async def shutdown(self) -> None:
        """Stop admission and drain workers without blocking the event loop.

        The join runs on a dedicated temporary thread so repeated calls may be
        awaited from different event loops, and cancelling one waiter cannot
        cancel the underlying shutdown.
        """
        self.close_admission()
        with self._lock:
            if not self._shutdown_started:
                def drain_workers():
                    try:
                        self._executor.shutdown(wait=True)
                    except BaseException as ex:
                        self._shutdown_error = ex
                    finally:
                        self._shutdown_done.set()

                shutdown_thread = threading.Thread(
                    target=drain_workers,
                    name="bounded-executor-shutdown",
                    daemon=False,
                )
                try:
                    shutdown_thread.start()
                except BaseException:
                    self._closed = False
                    raise
                self._shutdown_started = True
                self._shutdown_thread = shutdown_thread

        cancellation_requested = False
        if not self._shutdown_done.is_set():
            wait_task = asyncio.create_task(asyncio.to_thread(self._shutdown_done.wait))
            while not wait_task.done():
                try:
                    await asyncio.shield(wait_task)
                except asyncio.CancelledError:
                    # Keep waiting until the underlying executor shutdown is
                    # complete; a cancelled waiter must not outlive its pool.
                    cancellation_requested = True
            wait_task.result()
        if cancellation_requested:
            raise asyncio.CancelledError
        if self._shutdown_error is not None:
            raise self._shutdown_error


# Four workers with four additional admitted queue positions. Admission is
# checked before submit, so ThreadPoolExecutor's internal queue cannot grow
# beyond this fixed total even under concurrent Telegram and Mini App traffic.
MOYSKLAD_EXECUTOR = BoundedExecutor(max_workers=4, max_in_flight=8)


async def run_blocking_task(function: Callable[..., T], *args) -> T:
    return await MOYSKLAD_EXECUTOR.run(function, *args)
