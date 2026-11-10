import asyncio
import base64
import importlib
import json
from functools import partial
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import threading

from aiohttp import web
from PIL import Image
import pytest

from main.blocking_tasks import BoundedExecutor, ExecutorBusyError, MOYSKLAD_EXECUTOR
from main import order_storage


def _miniapp_server():
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        return importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "orders.sqlite3"
    photo_dir = tmp_path / "photos"
    order_storage.initialize_customer_orders_db(db_path)
    return db_path, photo_dir


def _create(db_path, photo_dir, *, user_id=101, photo_data=None):
    return order_storage.create_customer_order(
        db_path,
        photo_dir,
        user_id=user_id,
        product_name="Товар",
        quantity=2,
        customer_name="Покупатель",
        contact="+70000000000",
        note="Примечание",
        created_at="2026-11-03T12:30+03:00",
        photo_data=photo_data,
        photo_type="image/png" if photo_data else None,
        photo_extension=".png" if photo_data else None,
    )


def _png_data():
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (12, 10), "red").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _independent_storage_process(db_path, photo_dir, queue, release_event):
    try:
        owner_lock = order_storage.acquire_storage_owner_lock(Path(db_path))
    except order_storage.StorageAlreadyOwnedError:
        queue.put("busy-before-recovery")
        return
    try:
        queue.put("acquired")
        release_event.wait(timeout=10)
        order_storage.recover_order_transactions(Path(db_path), Path(photo_dir))
        queue.put("recovery-ran")
    finally:
        owner_lock.release()
    queue.put("released")


def _hold_storage_lock_process(db_path, queue, release_event):
    owner_lock = order_storage.acquire_storage_owner_lock(Path(db_path))
    queue.put("acquired")
    release_event.wait(timeout=10)
    owner_lock.release()
    queue.put("released")


def test_create_list_delete_and_user_isolation(store):
    db_path, photo_dir = store
    owned_id = _create(db_path, photo_dir, user_id=101)
    other_id = _create(db_path, photo_dir, user_id=202)

    assert [item["id"] for item in order_storage.list_customer_orders(db_path, 101)] == [owned_id]
    assert order_storage.list_customer_orders(db_path, 101)[0]["has_photo"] is False
    with pytest.raises(order_storage.OrderNotFoundError):
        order_storage.delete_customer_order(db_path, photo_dir, owned_id, 202)
    assert order_storage.delete_customer_order(db_path, photo_dir, owned_id, 101) is True
    assert [item["id"] for item in order_storage.list_customer_orders(db_path, 202)] == [other_id]


def test_storage_owner_lock_blocks_second_spawned_process_before_recovery(store):
    db_path, photo_dir = store
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    first_release = context.Event()
    first = context.Process(
        target=_independent_storage_process,
        args=(str(db_path), str(photo_dir), queue, first_release),
    )
    first.start()
    assert queue.get(timeout=10) == "acquired"

    second_release = context.Event()
    second = context.Process(
        target=_independent_storage_process,
        args=(str(db_path), str(photo_dir), queue, second_release),
    )
    second.start()
    assert queue.get(timeout=10) == "busy-before-recovery"
    second.join(timeout=10)
    assert second.exitcode == 0

    first_release.set()
    assert queue.get(timeout=10) == "recovery-ran"
    assert queue.get(timeout=10) == "released"
    first.join(timeout=10)
    assert first.exitcode == 0

    lock = order_storage.acquire_storage_owner_lock(db_path)
    try:
        assert lock.path.exists()
        assert lock.path.stat().st_mode & 0o777 == 0o600
    finally:
        lock.release()
    assert lock.path.exists(), "released lock files must remain on the shared volume"


def test_storage_owner_lock_is_released_by_os_after_process_termination(store):
    db_path, _photo_dir = store
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    release_event = context.Event()
    process = context.Process(
        target=_hold_storage_lock_process,
        args=(str(db_path), queue, release_event),
    )
    process.start()
    assert queue.get(timeout=10) == "acquired"
    process.terminate()
    process.join(timeout=10)
    assert process.exitcode is not None

    owner_lock = order_storage.acquire_storage_owner_lock(db_path)
    owner_lock.release()


def test_failed_pdf_decode_cleans_transaction_journal_and_temporary_file(store):
    db_path, photo_dir = store
    malformed_png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\ninvalid").decode()
    order_id = _create(db_path, photo_dir, photo_data=malformed_png)

    with pytest.raises(order_storage.OrderPhotoPreparationError):
        order_storage.get_customer_order_photo(db_path, photo_dir, order_id, 101, pdf=True)

    assert not any(path.name.startswith(".order-txn-") for path in photo_dir.iterdir())
    assert not any(path.name.endswith(".tmp") for path in photo_dir.iterdir())


def test_pdf_cleanup_failure_preserves_recoverable_journal_and_unknown_files(store, monkeypatch):
    db_path, photo_dir = store
    malformed_png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\ninvalid").decode()
    order_id = _create(db_path, photo_dir, photo_data=malformed_png)
    unknown = photo_dir / "keep-this-file.bin"
    unknown.write_bytes(b"unknown attachment")
    real_remove = order_storage._remove_journal

    def fail_journal_cleanup(path):
        if path.name.startswith(".order-txn-"):
            raise OSError("simulated journal cleanup failure")
        real_remove(path)

    monkeypatch.setattr(order_storage, "_remove_journal", fail_journal_cleanup)
    with pytest.raises(order_storage.OrderPhotoPreparationError):
        order_storage.get_customer_order_photo(db_path, photo_dir, order_id, 101, pdf=True)
    assert any(path.name.startswith(".order-txn-") for path in photo_dir.iterdir())
    monkeypatch.setattr(order_storage, "_remove_journal", real_remove)

    assert order_storage.recover_order_transactions(db_path, photo_dir) == (1, 0)
    assert order_storage.recover_order_transactions(db_path, photo_dir) == (0, 0)
    assert unknown.read_bytes() == b"unknown attachment"


def test_pdf_thumbnail_is_created_with_restricted_permissions(store):
    db_path, photo_dir = store
    order_id = _create(db_path, photo_dir, photo_data=_png_data())
    previous_umask = os.umask(0o022)
    try:
        order_storage.get_customer_order_photo(db_path, photo_dir, order_id, 101, pdf=True)
    finally:
        os.umask(previous_umask)
    photo_filename = order_storage._row_for_id(db_path, order_id)[6]
    thumbnail = order_storage.pdf_photo_path(photo_dir / photo_filename)
    assert stat.S_IMODE(thumbnail.stat().st_mode) == 0o600


def test_cached_pdf_thumbnail_permissions_are_restricted(store):
    db_path, photo_dir = store
    order_id = _create(db_path, photo_dir, photo_data=_png_data())
    photo_filename = order_storage._row_for_id(db_path, order_id)[6]
    source = photo_dir / photo_filename
    thumbnail = order_storage.make_pdf_photo(source, db_path, photo_dir, order_id, 101)
    if os.name == "posix":
        thumbnail.chmod(0o644)
        cached = order_storage.make_pdf_photo(source, db_path, photo_dir, order_id, 101)
        assert cached == thumbnail
        assert stat.S_IMODE(cached.stat().st_mode) == 0o600


def test_failed_insert_rolls_back_and_removes_created_photo(store):
    db_path, photo_dir = store
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_order BEFORE INSERT ON customer_orders "
            "BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated"):
        _create(db_path, photo_dir, photo_data=_png_data())

    assert order_storage.list_customer_orders(db_path, 101) == []
    assert list(photo_dir.iterdir()) == []


def test_partial_photo_write_is_removed_after_oserror(store, monkeypatch):
    db_path, photo_dir = store
    original_writer = order_storage._write_photo_atomically

    def partial_writer(directory, filename, transaction_id, photo_bytes):
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f".{filename}.{transaction_id}.part"
        partial.write_bytes(photo_bytes[:8])
        raise OSError("simulated disk full")

    monkeypatch.setattr(order_storage, "_write_photo_atomically", partial_writer)
    with pytest.raises(OSError, match="disk full"):
        _create(db_path, photo_dir, photo_data=_png_data())
    monkeypatch.setattr(order_storage, "_write_photo_atomically", original_writer)
    assert order_storage.list_customer_orders(db_path, 101) == []
    assert list(photo_dir.iterdir()) == []


def test_delete_file_error_keeps_journaled_tombstone_for_recovery(store, monkeypatch):
    db_path, photo_dir = store
    order_id = _create(db_path, photo_dir, photo_data=_png_data())
    photo = photo_dir / (order_storage._row_for_id(db_path, order_id)[6])
    original_unlink = Path.unlink

    def fail_tombstone_unlink(path, *args, **kwargs):
        if path.name.startswith(".delete-"):
            raise OSError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_tombstone_unlink)
    assert order_storage.delete_customer_order(db_path, photo_dir, order_id, 101) is True
    assert order_storage._row_for_id(db_path, order_id) is None
    assert not photo.exists()
    assert any(path.name.startswith(".delete-") for path in photo_dir.iterdir())
    monkeypatch.setattr(Path, "unlink", original_unlink)

    assert order_storage.recover_order_transactions(db_path, photo_dir) == (1, 0)
    assert list(photo_dir.iterdir()) == []


def test_pdf_journal_is_recovered_after_thumbnail_creation(store, monkeypatch):
    db_path, photo_dir = store
    order_id = _create(db_path, photo_dir, photo_data=_png_data())
    row = order_storage._row_for_id(db_path, order_id)
    photo_path = photo_dir / row[6]
    real_remove = order_storage._remove_journal

    def leave_pdf_journal(path):
        if path.name.endswith(".json"):
            raise OSError("simulated crash before journal cleanup")
        real_remove(path)

    monkeypatch.setattr(order_storage, "_remove_journal", leave_pdf_journal)
    body, content_type = order_storage.get_customer_order_photo(
        db_path, photo_dir, order_id, 101, pdf=True,
    )
    assert body.startswith(b"\xff\xd8")
    assert content_type == "image/jpeg"
    monkeypatch.setattr(order_storage, "_remove_journal", real_remove)

    assert order_storage.recover_order_transactions(db_path, photo_dir) == (1, 0)
    assert order_storage.pdf_photo_path(photo_path).is_file()
    assert not any(path.name.startswith(".order-txn-") for path in photo_dir.iterdir())


def test_pdf_and_delete_are_serialized_and_pdf_returns_stable_bytes(store, monkeypatch):
    db_path, photo_dir = store
    order_id = _create(db_path, photo_dir, photo_data=_png_data())
    server = _miniapp_server()
    server.CUSTOMER_ORDERS_DB = db_path
    server.CUSTOMER_ORDER_PHOTOS = photo_dir
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", BoundedExecutor(max_workers=1, max_in_flight=2))
    started = threading.Event()
    continue_pdf = threading.Event()
    original = order_storage.get_customer_order_photo

    def delayed_pdf(*args, **kwargs):
        if kwargs.get("pdf"):
            started.set()
            continue_pdf.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "get_customer_order_photo", delayed_pdf)

    async def scenario():
        pdf_task = asyncio.create_task(server.run_order_storage(
            server.get_customer_order_photo,
            db_path, photo_dir, order_id, 101, pdf=True,
        ))
        assert await asyncio.to_thread(started.wait, 1)
        delete_task = asyncio.create_task(server.run_order_storage(
            order_storage.delete_customer_order, db_path, photo_dir, order_id, 101,
        ))
        await asyncio.sleep(0.02)
        assert not delete_task.done()
        continue_pdf.set()
        body, content_type = await pdf_task
        assert body.startswith(b"\xff\xd8")
        assert content_type == "image/jpeg"
        assert await delete_task is True

    try:
        asyncio.run(scenario())
        assert not list(photo_dir.iterdir())
    finally:
        continue_pdf.set()
        asyncio.run(server.ORDER_STORAGE_EXECUTOR.shutdown())


def test_locked_sqlite_does_not_block_event_loop(store):
    db_path, _ = store
    import sqlite3

    locker = sqlite3.connect(db_path, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    loop_heartbeat = threading.Event()
    release_timer = threading.Timer(0.2, locker.rollback)
    release_timer.start()

    async def scenario():
        task = asyncio.create_task(executor.run(order_storage.list_customer_orders, db_path, 101))
        asyncio.get_running_loop().call_later(0.03, loop_heartbeat.set)
        assert await asyncio.to_thread(loop_heartbeat.wait, 0.15)
        assert await task == []

    try:
        asyncio.run(scenario())
    finally:
        release_timer.cancel()
        locker.close()
        asyncio.run(executor.shutdown())


def test_bounded_storage_executor_cancellation_capacity_and_recovery():
    executor = BoundedExecutor(max_workers=1, max_in_flight=2)
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=2)

    async def wait_slots(expected):
        for _ in range(500):
            if executor.in_flight == expected:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"in_flight did not reach {expected}")

    async def scenario():
        first = asyncio.create_task(executor.run(blocked))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(executor.run(lambda: "queued"))
        await wait_slots(2)
        with pytest.raises(ExecutorBusyError):
            await executor.run(lambda: None)
        release.set()
        await second
        await wait_slots(0)
        assert await executor.run(lambda: "recovered") == "recovered"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(executor.shutdown())


def test_executor_shutdown_drains_accepted_work_without_blocking_loop():
    executor = BoundedExecutor(max_workers=1, max_in_flight=2)
    started = threading.Event()
    release = threading.Event()
    heartbeat = asyncio.Event()

    def blocked():
        started.set()
        release.wait(timeout=3)
        return "done"

    async def scenario():
        accepted = asyncio.create_task(executor.run(blocked))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        shutdown = asyncio.create_task(executor.shutdown())
        for _ in range(500):
            if executor._closed:
                break
            await asyncio.sleep(0.001)
        assert executor._closed
        asyncio.get_running_loop().call_later(0.02, heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
        assert not shutdown.done()
        with pytest.raises(ExecutorBusyError):
            await executor.run(lambda: "rejected")
        release.set()
        assert await accepted == "done"
        await shutdown
        await executor.shutdown()

    try:
        asyncio.run(scenario())
        # The completed shutdown is safe to await from another event loop.
        asyncio.run(executor.shutdown())
    finally:
        release.set()
        asyncio.run(executor.shutdown())


def test_cancelled_executor_shutdown_waiter_still_waits_for_workers():
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=3)

    async def scenario():
        accepted = asyncio.create_task(executor.run(blocked))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        shutdown = asyncio.create_task(executor.shutdown())
        for _ in range(500):
            if executor._closed:
                break
            await asyncio.sleep(0.001)
        assert executor._closed
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert not executor.shutdown_complete
        release.set()
        await accepted
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert executor.shutdown_complete

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(executor.shutdown())


def test_application_shutdown_closes_both_pools_before_releasing_owner_lock(tmp_path, monkeypatch):
    server = _miniapp_server()
    import main.blocking_tasks as blocking_tasks

    storage_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    moysklad_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", storage_pool)
    monkeypatch.setattr(blocking_tasks, "MOYSKLAD_EXECUTOR", moysklad_pool)
    owner_lock = order_storage.acquire_storage_owner_lock(tmp_path / "orders.sqlite3")
    monkeypatch.setattr(server, "ORDER_STORAGE_OWNER_LOCK", owner_lock)
    storage_started = threading.Event()
    moysklad_started = threading.Event()
    release_storage = threading.Event()
    release_moysklad = threading.Event()

    def block(started, release):
        started.set()
        release.wait(timeout=3)

    async def scenario():
        storage_task = asyncio.create_task(storage_pool.run(block, storage_started, release_storage))
        moysklad_task = asyncio.create_task(moysklad_pool.run(block, moysklad_started, release_moysklad))
        await asyncio.sleep(0)
        assert storage_started.wait(timeout=1)
        assert moysklad_started.wait(timeout=1)
        stopping = asyncio.create_task(server.stop_miniapp_server(None))
        for _ in range(500):
            if storage_pool._closed and moysklad_pool._closed:
                break
            await asyncio.sleep(0.001)
        assert storage_pool._closed and moysklad_pool._closed
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
        with pytest.raises(order_storage.StorageAlreadyOwnedError):
            order_storage.acquire_storage_owner_lock(tmp_path / "orders.sqlite3")
        with pytest.raises(ExecutorBusyError):
            await storage_pool.run(lambda: None)
        with pytest.raises(ExecutorBusyError):
            await moysklad_pool.run(lambda: None)
        release_storage.set()
        await storage_task
        await asyncio.sleep(0.01)
        assert not stopping.done()
        with pytest.raises(order_storage.StorageAlreadyOwnedError):
            order_storage.acquire_storage_owner_lock(tmp_path / "orders.sqlite3")
        release_moysklad.set()
        await moysklad_task
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert owner_lock.released
        reacquired = order_storage.acquire_storage_owner_lock(tmp_path / "orders.sqlite3")
        assert not reacquired.released
        reacquired.release()

    try:
        asyncio.run(scenario())
    finally:
        release_storage.set()
        release_moysklad.set()
        if not owner_lock.released:
            owner_lock.release()
        asyncio.run(storage_pool.shutdown())
        asyncio.run(moysklad_pool.shutdown())


def test_partial_startup_failure_drains_pools_and_releases_storage_lock(tmp_path, monkeypatch):
    server = _miniapp_server()
    import main.blocking_tasks as blocking_tasks

    db_path = tmp_path / "orders.sqlite3"
    photo_dir = tmp_path / "photos"
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    storage_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    moysklad_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    monkeypatch.setattr(server, "CUSTOMER_ORDERS_DB", db_path)
    monkeypatch.setattr(server, "CUSTOMER_ORDER_PHOTOS", photo_dir)
    monkeypatch.setattr(server, "MINIAPP_ASSETS_DIR", assets_dir)
    monkeypatch.setattr(server, "LOCAL_MINIAPP_DEV", False)
    monkeypatch.setattr(server, "PUBLIC_MINIAPP_URL", "")
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", storage_pool)
    monkeypatch.setattr(server, "ORDER_STORAGE_OWNER_LOCK", None)
    monkeypatch.setattr(blocking_tasks, "MOYSKLAD_EXECUTOR", moysklad_pool)

    class FailingSite:
        async def start(self):
            raise OSError("mock bind failure")

    class FakeRunner:
        async def setup(self):
            return None

        async def cleanup(self):
            return None

    runner = FakeRunner()
    monkeypatch.setattr(server.web, "AppRunner", lambda _app: runner)
    monkeypatch.setattr(server.web, "TCPSite", lambda *_args: FailingSite())

    with pytest.raises(OSError, match="mock bind failure"):
        asyncio.run(server.start_miniapp_server())

    assert server.ORDER_STORAGE_OWNER_LOCK is None
    assert storage_pool._closed and moysklad_pool._closed
    recovered_lock = order_storage.acquire_storage_owner_lock(db_path)
    recovered_lock.release()


def test_concurrent_startup_attempt_does_not_drain_the_winning_startup(tmp_path, monkeypatch):
    server = _miniapp_server()
    import main.blocking_tasks as blocking_tasks

    db_path = tmp_path / "orders.sqlite3"
    photo_dir = tmp_path / "photos"
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    storage_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    moysklad_pool = BoundedExecutor(max_workers=1, max_in_flight=2)
    monkeypatch.setattr(server, "CUSTOMER_ORDERS_DB", db_path)
    monkeypatch.setattr(server, "CUSTOMER_ORDER_PHOTOS", photo_dir)
    monkeypatch.setattr(server, "MINIAPP_ASSETS_DIR", assets_dir)
    monkeypatch.setattr(server, "LOCAL_MINIAPP_DEV", False)
    monkeypatch.setattr(server, "PUBLIC_MINIAPP_URL", "")
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", storage_pool)
    monkeypatch.setattr(server, "ORDER_STORAGE_OWNER_LOCK", None)
    monkeypatch.setattr(blocking_tasks, "MOYSKLAD_EXECUTOR", moysklad_pool)
    entered = threading.Event()
    continue_startup = threading.Event()
    real_acquire = order_storage.acquire_storage_owner_lock

    def delayed_acquire(path):
        entered.set()
        continue_startup.wait(timeout=2)
        return real_acquire(path)

    monkeypatch.setattr(server, "acquire_storage_owner_lock", delayed_acquire)

    class FakeRunner:
        async def setup(self):
            return None

        async def cleanup(self):
            return None

    runner = FakeRunner()

    class FailingSite:
        async def start(self):
            raise OSError("mock bind failure")

    monkeypatch.setattr(server.web, "AppRunner", lambda _app: runner)
    monkeypatch.setattr(server.web, "TCPSite", lambda *_args: FailingSite())

    async def scenario():
        winning_start = asyncio.create_task(server.start_miniapp_server())
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(RuntimeError, match="Запуск Mini App уже выполняется"):
            await server.start_miniapp_server()
        assert not storage_pool._closed and not moysklad_pool._closed
        continue_startup.set()
        with pytest.raises(OSError, match="mock bind failure"):
            await winning_start

    try:
        asyncio.run(scenario())
        assert server.ORDER_STORAGE_OWNER_LOCK is None
        recovered_lock = order_storage.acquire_storage_owner_lock(db_path)
        recovered_lock.release()
    finally:
        continue_startup.set()
        asyncio.run(storage_pool.shutdown())
        asyncio.run(moysklad_pool.shutdown())


def test_cancelled_storage_lock_acquisition_releases_lock_after_worker_finishes(tmp_path, monkeypatch):
    server = _miniapp_server()
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", executor)
    started = threading.Event()
    release = threading.Event()
    db_path = tmp_path / "orders.sqlite3"

    def delayed_acquire(path):
        started.set()
        release.wait(timeout=2)
        return order_storage.acquire_storage_owner_lock(path)

    monkeypatch.setattr(server, "acquire_storage_owner_lock", delayed_acquire)

    async def scenario():
        acquisition = asyncio.create_task(server._acquire_order_storage_owner())
        assert await asyncio.to_thread(started.wait, 1)
        acquisition.cancel()
        await asyncio.sleep(0)
        assert not acquisition.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        acquired = order_storage.acquire_storage_owner_lock(db_path)
        acquired.release()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(executor.shutdown())


def test_cancelled_order_handler_waiter_does_not_leave_partial_storage_work(store, monkeypatch):
    db_path, photo_dir = store
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    writer_started = threading.Event()
    continue_writer = threading.Event()
    original_writer = order_storage._write_photo_atomically

    def delayed_writer(*args):
        writer_started.set()
        continue_writer.wait(timeout=2)
        return original_writer(*args)

    monkeypatch.setattr(order_storage, "_write_photo_atomically", delayed_writer)

    async def scenario():
        task = asyncio.create_task(executor.run(
            partial(_create, db_path, photo_dir, user_id=101, photo_data=_png_data()),
        ))
        assert await asyncio.to_thread(writer_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.in_flight == 1
        continue_writer.set()
        for _ in range(500):
            if executor.in_flight == 0:
                break
            await asyncio.sleep(0.001)
        assert executor.in_flight == 0
        rows = order_storage.list_customer_orders(db_path, 101)
        assert len(rows) == 1
        assert rows[0]["has_photo"] is True
        assert len([path for path in photo_dir.iterdir() if path.suffix == ".png"]) == 1
        assert not any(path.name.startswith(".order-txn-") for path in photo_dir.iterdir())

    try:
        asyncio.run(scenario())
    finally:
        continue_writer.set()
        asyncio.run(executor.shutdown())


def test_storage_executor_is_independent_from_moysklad_pool(monkeypatch):
    server = _miniapp_server()
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", BoundedExecutor(max_workers=1, max_in_flight=4))
    assert server.ORDER_STORAGE_EXECUTOR is not MOYSKLAD_EXECUTOR
    assert server.ORDER_STORAGE_EXECUTOR.max_workers == 1
    assert server.ORDER_STORAGE_EXECUTOR.max_in_flight == 4


def test_order_storage_overload_maps_to_503_with_retry_after(monkeypatch):
    server = _miniapp_server()
    executor = BoundedExecutor(max_workers=1, max_in_flight=1)
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", executor)
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=2)

    async def scenario():
        first = asyncio.create_task(server.run_order_storage(blocked))
        await asyncio.sleep(0)
        assert started.wait(timeout=1)
        with pytest.raises(web.HTTPServiceUnavailable) as error:
            await server.run_order_storage(lambda: None)
        assert error.value.status == 503
        assert error.value.headers["Retry-After"] == "5"
        release.set()
        await first

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(executor.shutdown())


def test_successful_order_http_contracts_use_worker_and_keep_response_shapes(store, monkeypatch):
    db_path, photo_dir = store
    server = _miniapp_server()
    server.CUSTOMER_ORDERS_DB = db_path
    server.CUSTOMER_ORDER_PHOTOS = photo_dir
    monkeypatch.setattr(server, "ORDER_STORAGE_EXECUTOR", BoundedExecutor(max_workers=1, max_in_flight=4))
    monkeypatch.setattr(server, "authorized_user_id", lambda _request: 101)
    monkeypatch.setattr(server, "enforce_user_rate_limit", lambda *_args: None)

    class Request:
        match_info = {}
        query = {}
        async def json(self):
            return {
                "product_name": "Товар",
                "quantity": 1,
                "photo_data": _png_data(),
            }

    async def scenario():
        created = await server.create_customer_order(Request())
        assert created.status == 201
        created_body = json.loads(created.body)
        assert set(created_body) == {"id"}
        Request.match_info = {"order_id": created_body["id"]}
        listed = await server.customer_orders_data(Request())
        listed_body = json.loads(listed.body)
        assert set(listed_body) == {"items"}
        assert len(listed_body["items"]) == 1
        photo = await server.customer_order_photo(Request())
        assert photo.status == 200
        assert photo.content_type == "image/png"
        assert photo.body.startswith(b"\x89PNG\r\n\x1a\n")
        Request.query = {"variant": "pdf"}
        pdf_photo = await server.customer_order_photo(Request())
        assert pdf_photo.status == 200
        assert pdf_photo.content_type == "image/jpeg"
        assert pdf_photo.body.startswith(b"\xff\xd8")
        Request.query = {}
        deleted = await server.delete_customer_order(Request())
        assert json.loads(deleted.body) == {"deleted": True}

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(server.ORDER_STORAGE_EXECUTOR.shutdown())
