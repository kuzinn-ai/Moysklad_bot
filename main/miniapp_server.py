import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
from functools import partial
from collections import deque
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
import requests

try:
    from .settings import OFFLINE_TEST_MODE, token_tg, users
    from .blocking_tasks import BoundedExecutor, ExecutorBusyError, run_blocking_task
    from .main import MoySkladAPIError, StockSalesReport, give_dates_ago
    from .abc_xyz import fetch_analysis, three_complete_months
    from . import offline_test_data
    from .abc_xyz_export import build_abc_xyz_xlsx
    from .order_storage import (
        OrderNotFoundError,
        OrderPhotoInputError,
        OrderPhotoPreparationError,
        StorageOwnerLock,
        MAX_ORDER_PHOTO_BYTES,
        ALLOWED_ORDER_PHOTO_TYPES,
        acquire_storage_owner_lock,
        create_customer_order as create_customer_order_in_worker,
        delete_customer_order as delete_customer_order_in_worker,
        get_customer_order_photo,
        initialize_customer_orders_db as initialize_order_db_in_worker,
        list_customer_orders,
        recover_order_transactions,
    )
except ImportError:
    from settings import OFFLINE_TEST_MODE, token_tg, users
    from blocking_tasks import BoundedExecutor, ExecutorBusyError, run_blocking_task
    from main import MoySkladAPIError, StockSalesReport, give_dates_ago
    from abc_xyz import fetch_analysis, three_complete_months
    import offline_test_data
    from abc_xyz_export import build_abc_xyz_xlsx
    from order_storage import (
        OrderNotFoundError,
        OrderPhotoInputError,
        OrderPhotoPreparationError,
        StorageOwnerLock,
        MAX_ORDER_PHOTO_BYTES,
        ALLOWED_ORDER_PHOTO_TYPES,
        acquire_storage_owner_lock,
        create_customer_order as create_customer_order_in_worker,
        delete_customer_order as delete_customer_order_in_worker,
        get_customer_order_photo,
        initialize_customer_orders_db as initialize_order_db_in_worker,
        list_customer_orders,
        recover_order_transactions,
    )


MINIAPP_FILE = Path(__file__).resolve().parent.parent / "miniapp" / "index.html"
MINIAPP_ASSETS_DIR = Path(__file__).resolve().parent.parent / "miniapp" / "assets"
CUSTOMER_ORDERS_DB = Path(os.getenv(
    "CUSTOMER_ORDERS_DB",
    str(Path(__file__).resolve().parent / "customer_orders.sqlite3"),
))
CUSTOMER_ORDER_PHOTOS = CUSTOMER_ORDERS_DB.parent / "customer_order_photos"
ABC_XYZ_SNAPSHOT = CUSTOMER_ORDERS_DB.parent / (
    "abc_xyz_offline_snapshot.json" if OFFLINE_TEST_MODE else "abc_xyz_snapshot.json"
)
ABC_XYZ_TASK = None
ABC_XYZ_TASK_PERIOD = None
MAX_INIT_DATA_AGE_SECONDS = 60 * 60
LOCAL_MINIAPP_DEV = os.getenv("MINIAPP_LOCAL_DEV") == "1"
PUBLIC_MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()
PURCHASE_CACHE_TTL_SECONDS = 300
PURCHASE_CACHE = {}
PURCHASE_PRICE_CACHE_TTL_SECONDS = 300
PURCHASE_PRICE_CACHE = {}
ORDER_STORAGE_EXECUTOR = BoundedExecutor(max_workers=1, max_in_flight=4)
ORDER_STORAGE_OWNER_LOCK: StorageOwnerLock | None = None
_STARTUP_STATE_GUARD = threading.Lock()
_STARTUP_IN_PROGRESS = False
API_RATE_WINDOW_SECONDS = 60
# Telegram API users are explicitly allowlisted, so per-user limits are stable
# and cannot be bypassed by spoofing forwarded-IP headers.
API_RATE_LIMITS = {
    "/api/stock": 12,
    "/api/purchase": 12,
    "/api/purchase-price": 120,
    "/api/abc-xyz": 20,
    "/api/abc-xyz/export": 10,
    "/api/customer-orders": 30,
}
API_REQUEST_TIMES = {}


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org https://t.me https://*.t.me"
    )
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if PUBLIC_MINIAPP_URL.startswith("https://"):
        # TLS is terminated by the hosting provider / reverse proxy.
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def enforce_user_rate_limit(request: web.Request, user_id: int) -> None:
    route_key = "/api/customer-orders" if request.path.startswith("/api/customer-orders/") else request.path
    limit = API_RATE_LIMITS.get(route_key)
    if limit is None:
        return

    now = time.monotonic()
    key = (user_id, route_key)
    timestamps = API_REQUEST_TIMES.setdefault(key, deque())
    cutoff = now - API_RATE_WINDOW_SECONDS
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()
    if len(timestamps) >= limit:
        raise web.HTTPTooManyRequests(
            text="Слишком много запросов. Подождите минуту и попробуйте снова.",
            headers={"Retry-After": str(API_RATE_WINDOW_SECONDS)},
        )
    timestamps.append(now)


async def run_moysklad_call(function, *args):
    """Run a synchronous MoySklad call through the shared bounded worker pool."""
    try:
        return await run_blocking_task(function, *args)
    except ExecutorBusyError:
        raise web.HTTPServiceUnavailable(
            text="Сервис занят. Попробуйте ещё раз через несколько секунд.",
            headers={"Retry-After": "5"},
        )


async def run_order_storage(function, *args, **kwargs):
    """Run one whole SQLite/filesystem unit in the independent bounded pool."""
    if kwargs:
        function = partial(function, *args, **kwargs)
        args = ()
    try:
        return await ORDER_STORAGE_EXECUTOR.run(function, *args)
    except ExecutorBusyError:
        raise web.HTTPServiceUnavailable(
            text="Хранилище заказов занято. Попробуйте ещё раз через несколько секунд.",
            headers={"Retry-After": "5"},
        )


async def _wait_for_task_completion(task: asyncio.Task):
    """Do not abandon a startup/shutdown worker when its caller is cancelled."""
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
    if cancellation_requested:
        if not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass
        raise asyncio.CancelledError
    return task.result()


async def _acquire_order_storage_owner() -> StorageOwnerLock:
    task = asyncio.create_task(
        run_order_storage(acquire_storage_owner_lock, CUSTOMER_ORDERS_DB)
    )
    try:
        return await _wait_for_task_completion(task)
    except asyncio.CancelledError:
        # Acquisition itself runs in a worker. If cancellation arrived after
        # flock succeeded, release that descriptor before startup unwinds.
        if task.done() and not task.cancelled():
            try:
                acquired_lock = task.result()
            except BaseException:
                pass
            else:
                acquired_lock.release()
        raise


async def _drain_executor_pools(owner_lock: StorageOwnerLock | None) -> None:
    failures = []
    try:
        from .blocking_tasks import MOYSKLAD_EXECUTOR
    except ImportError:
        from blocking_tasks import MOYSKLAD_EXECUTOR
    ORDER_STORAGE_EXECUTOR.close_admission()
    MOYSKLAD_EXECUTOR.close_admission()
    try:
        await ORDER_STORAGE_EXECUTOR.shutdown()
    except BaseException as ex:
        failures.append(ex)
    try:
        await MOYSKLAD_EXECUTOR.shutdown()
    except BaseException as ex:
        failures.append(ex)
    finally:
        # Retain exclusive ownership until both pools have drained. The
        # MoySklad pool does not currently touch order files, but shutdown
        # should remain a single-owner lifecycle if that changes later.
        pools_drained = (
            ORDER_STORAGE_EXECUTOR.shutdown_complete
            and MOYSKLAD_EXECUTOR.shutdown_complete
        )
        if owner_lock is not None and pools_drained:
            try:
                owner_lock.release()
            except BaseException as ex:
                failures.append(ex)
        elif owner_lock is not None:
            failures.append(RuntimeError("Не удалось подтвердить остановку всех executor-пулов."))
    if failures:
        raise failures[0]


def is_authorized_request(request):
    if LOCAL_MINIAPP_DEV and request.remote in {"127.0.0.1", "::1"}:
        return True

    authorization = request.headers.get("Authorization", "")
    return (
        authorization.startswith("tma ")
        and _validate_telegram_init_data(authorization[4:]) is not None
    )


def authorized_user_id(request):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("tma "):
        return None
    return _validate_telegram_init_data(authorization[4:])


def initialize_customer_orders_db():
    initialize_order_db_in_worker(CUSTOMER_ORDERS_DB)


async def customer_orders_data(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    items = await run_order_storage(list_customer_orders, CUSTOMER_ORDERS_DB, user_id)
    return web.json_response({"items": items})


async def create_customer_order(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Проверьте данные заказа.")

    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Проверьте данные заказа.")
    text_fields = ("product_name", "customer_name", "contact", "note")
    if any(key in data and not isinstance(data[key], str) for key in text_fields):
        raise web.HTTPBadRequest(text="Проверьте текстовые поля заказа.")
    product_name = data.get("product_name", "").strip()
    customer_name = data.get("customer_name", "").strip()
    contact = data.get("contact", "").strip()
    note = data.get("note", "").strip()
    quantity_value = data.get("quantity", 1)
    if isinstance(quantity_value, bool) or not isinstance(quantity_value, int):
        raise web.HTTPBadRequest(text="Количество должно быть целым числом.")
    quantity = quantity_value
    if not product_name or len(product_name) > 300:
        raise web.HTTPBadRequest(text="Укажите товар (до 300 символов).")
    if quantity < 1 or quantity > 999:
        raise web.HTTPBadRequest(text="Количество должно быть от 1 до 999.")
    if len(customer_name) > 100 or len(contact) > 100 or len(note) > 1000:
        raise web.HTTPBadRequest(text="Слишком длинное поле заказа.")

    photo_data = data.get("photo_data")
    photo_type = None
    photo_extension = None
    if photo_data:
        if not isinstance(photo_data, str) or "," not in photo_data:
            raise web.HTTPBadRequest(text="Не удалось прочитать фотографию.")
        header, encoded = photo_data.split(",", 1)
        if not header.lower().endswith(";base64"):
            raise web.HTTPBadRequest(text="Не удалось прочитать фотографию.")
        if len(encoded) > 4 * ((MAX_ORDER_PHOTO_BYTES + 2) // 3):
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_ORDER_PHOTO_BYTES,
                actual_size=len(encoded) * 3 // 4,
            )
        photo_type = header.removeprefix("data:").split(";", 1)[0].lower()
        photo_extension = ALLOWED_ORDER_PHOTO_TYPES.get(photo_type)
        if not photo_extension:
            raise web.HTTPBadRequest(text="Поддерживаются фото JPG, PNG и WebP.")
    created_at = datetime.now().astimezone().isoformat(timespec="minutes")
    try:
        order_id = await run_order_storage(
            create_customer_order_in_worker,
            CUSTOMER_ORDERS_DB,
            CUSTOMER_ORDER_PHOTOS,
            user_id=user_id,
            product_name=product_name,
            quantity=quantity,
            customer_name=customer_name,
            contact=contact,
            note=note,
            created_at=created_at,
            photo_data=photo_data,
            photo_type=photo_type,
            photo_extension=photo_extension,
        )
    except OrderPhotoInputError as ex:
        if ex.too_large:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_ORDER_PHOTO_BYTES,
                actual_size=ex.actual_size,
            )
        raise web.HTTPBadRequest(text=str(ex))
    return web.json_response({"id": order_id}, status=201)


async def customer_order_photo(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    try:
        body, content_type = await run_order_storage(
            get_customer_order_photo,
            CUSTOMER_ORDERS_DB,
            CUSTOMER_ORDER_PHOTOS,
            request.match_info["order_id"],
            user_id,
            pdf=request.query.get("variant") == "pdf",
        )
    except OrderNotFoundError:
        raise web.HTTPNotFound()
    except OrderPhotoPreparationError:
        raise web.HTTPUnprocessableEntity(text="Не удалось подготовить фото для PDF.")
    return web.Response(body=body, content_type=content_type)


async def delete_customer_order(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    order_id = request.match_info["order_id"]
    try:
        await run_order_storage(
            delete_customer_order_in_worker,
            CUSTOMER_ORDERS_DB,
            CUSTOMER_ORDER_PHOTOS,
            order_id,
            user_id,
        )
    except OrderNotFoundError:
        raise web.HTTPNotFound()
    return web.json_response({"deleted": True})


def _validate_telegram_init_data(init_data: str):
    if not init_data or len(init_data) > 10_000:
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            return None
        received_hash = values.pop("hash", None)
        auth_date = int(values.get("auth_date", "0"))
        now = int(time.time())
        if not received_hash or auth_date <= 0:
            return None
        if now - auth_date > MAX_INIT_DATA_AGE_SECONDS or auth_date - now > 30:
            return None

        data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret_key = hmac.new(b"WebAppData", token_tg.encode("utf-8"), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        user = json.loads(values["user"])
        user_id = int(user["id"])
        if user_id not in users:
            return None
        return user_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def index_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(MINIAPP_FILE)


async def stock_data(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)

    mode = request.query.get("mode", "positive")
    if mode not in {"positive", "zero", "negative"}:
        raise web.HTTPBadRequest(text="Неизвестный режим остатков.")
    report = StockSalesReport()
    if mode in {"positive", "negative"}:
        stock_mode = "positiveOnly" if mode == "positive" else "negativeOnly"
        report.filter = f"store={report.store_href};stockMode={stock_mode}"
    stock_tree = await run_moysklad_call(
        report.get_zero_stock_goods_dict if mode == "zero" else report.get_goods_dict
    )
    if stock_tree is None:
        raise web.HTTPBadGateway(text="МойСклад временно не ответил. Попробуйте ещё раз.")
    return web.json_response(stock_tree)


async def purchase_price_data(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)

    product_href = request.query.get("href", "")
    parsed = urlsplit(product_href)
    parts = parsed.path.strip("/").split("/")
    allowed_entities = {"product", "variant", "service", "bundle"}
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.moysklad.ru"
        or parsed.fragment
        or len(parts) != 6
        or parts[:4] != ["api", "remap", "1.2", "entity"]
        or parts[4] not in allowed_entities
    ):
        raise web.HTTPBadRequest(text="Некорректный товар.")

    # MoySklad entity hrefs are /entity/{type}/{uuid}; validate before using
    # the supplied href in an API request to prevent arbitrary URL requests.
    entity_parts = parsed.path.strip("/").split("/")
    if len(entity_parts) != 6 or entity_parts[4] not in allowed_entities:
        raise web.HTTPBadRequest(text="Некорректный товар.")
    try:
        entity_parts[5] = str(uuid.UUID(entity_parts[5]))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="Некорректный товар.")
    safe_href = f"https://api.moysklad.ru/api/remap/1.2/entity/{entity_parts[4]}/{entity_parts[5]}"

    cached = PURCHASE_PRICE_CACHE.get(safe_href)
    if cached and time.monotonic() - cached[0] < PURCHASE_PRICE_CACHE_TTL_SECONDS:
        return web.json_response({"buy_price": cached[1]})

    try:
        price = await run_moysklad_call(StockSalesReport()._latest_supply_price, safe_href)
    except web.HTTPException:
        raise
    except Exception as ex:
        print(f"Не удалось получить цену последней приёмки: {ex}")
        # Price history is optional: a lookup failure must not interrupt adding
        # the product to the draft. The client will keep the card price, if any.
        return web.json_response({"buy_price": None})

    PURCHASE_PRICE_CACHE[safe_href] = (time.monotonic(), price)
    return web.json_response({"buy_price": price})

async def purchase_data(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)

    period = request.query.get("period")
    now = datetime.now(timezone.utc).astimezone()

    if period == "week":
        this_sunday = now.date() - timedelta(days=(now.weekday() + 1) % 7)
        last_sunday = this_sunday - timedelta(days=7)
        last_saturday = this_sunday - timedelta(days=1)
        moment_from = f"{last_sunday:%Y-%m-%d} 00:00:00"
        moment_to = f"{last_saturday:%Y-%m-%d} 22:00:00"
    elif period == "month":
        moment_from, moment_to = give_dates_ago(
            now.day - 1,
            curent_period=True,
        )
    else:
        raise web.HTTPBadRequest(text="Период должен быть week или month.")

    cache_key = (period, moment_from, moment_to)
    cached = PURCHASE_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < PURCHASE_CACHE_TTL_SECONDS:
        return web.json_response({"period": period, "items": cached[1]})

    def load_data():
        report = StockSalesReport(
            moment_from=moment_from,
            moment_to=moment_to,
        )
        popular_goods = report.get_popular_goods(top_limit=5)
        # Recommendations are a short, explicit top list; the editable draft
        # is maintained separately by the Mini App and must never be replaced.
        stock_tree = report.get_goods_dict()

        if stock_tree is None:
            return None

        stock_by_name = {}

        def collect_stock(branch):
            for node in branch.values():
                for product in node.get("_goods", []):
                    key = " ".join(product["name"].casefold().split())
                    stock_by_name[key] = {
                        "stock": product["stock"],
                        "price": product["price"],
                    }
                collect_stock(node.get("_children", {}))

        collect_stock(stock_tree)

        result = []
        for good in popular_goods:
            key = " ".join(good["name"].casefold().split())
            stock_info = stock_by_name.get(key, {"stock": 0, "price": None, "buy_price": None})
            result.append({**good, **stock_info, "price": stock_info.get("price") if stock_info.get("price") is not None else good.get("price"),
                           "buy_price": good.get("buy_price")})

        return result

    try:
        items = await run_moysklad_call(load_data)
    except web.HTTPException:
        raise
    except requests.Timeout:
        raise web.HTTPGatewayTimeout(
            text="МойСклад отвечает слишком долго. Попробуйте ещё раз."
        )
    except (requests.RequestException, MoySkladAPIError):
        raise web.HTTPBadGateway(
            text="Не удалось получить данные из МоегоСклада. Попробуйте позже."
        )

    if items is None:
        raise web.HTTPBadGateway(text="Не удалось получить данные из МоегоСклада.")

    PURCHASE_CACHE[cache_key] = (time.monotonic(), items)
    return web.json_response({"period": period, "items": items})


def _stored_abc_xyz():
    try:
        data = json.loads(ABC_XYZ_SNAPSHOT.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _calculate_and_store_abc_xyz(start, end):
    data = offline_test_data.analysis(start, end) if OFFLINE_TEST_MODE else fetch_analysis(StockSalesReport(), start, end)
    data["calculated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ABC_XYZ_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".abc_xyz_", dir=ABC_XYZ_SNAPSHOT.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False)
        os.replace(temporary, ABC_XYZ_SNAPSHOT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def _abc_xyz_period(request):
    query = getattr(request, "query", {})
    start_text, end_text = query.get("from"), query.get("to")
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    if not start_text and not end_text:
        return three_complete_months(today)
    if not start_text or not end_text:
        raise web.HTTPBadRequest(text="Укажите начало и конец периода.")
    try:
        start, last_day = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Проверьте даты периода.")
    if start > last_day or last_day > today or (last_day - start).days >= 366:
        raise web.HTTPBadRequest(text="Выберите период до одного года, не позднее сегодняшнего дня.")
    return start, last_day + timedelta(days=1)


async def abc_xyz_data(request: web.Request) -> web.Response:
    global ABC_XYZ_TASK, ABC_XYZ_TASK_PERIOD
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    start, end = _abc_xyz_period(request)
    stored = _stored_abc_xyz()
    current = stored and stored.get("period_from") == start.isoformat() and stored.get("period_to") == (end - timedelta(days=1)).isoformat()

    if ABC_XYZ_TASK is not None and ABC_XYZ_TASK.done():
        try:
            ABC_XYZ_TASK.result()
        except Exception as ex:
            print(f"Не удалось рассчитать ABC-XYZ: {type(ex).__name__}")
            ABC_XYZ_TASK = None
            ABC_XYZ_TASK_PERIOD = None
            return web.json_response({"status": "error", "message": "Не удалось получить данные МоегоСклада. Повторите расчёт."}, status=502)
        else:
            ABC_XYZ_TASK = None
            ABC_XYZ_TASK_PERIOD = None
            stored = _stored_abc_xyz()
            current = stored and stored.get("period_from") == start.isoformat() and stored.get("period_to") == (end - timedelta(days=1)).isoformat()

    if ABC_XYZ_TASK is None and (request.method == "POST" or not current):
        ABC_XYZ_TASK = asyncio.create_task(run_moysklad_call(_calculate_and_store_abc_xyz, start, end))
        ABC_XYZ_TASK_PERIOD = (start, end)
        ABC_XYZ_TASK.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    if ABC_XYZ_TASK is not None:
        return web.json_response({"status": "calculating", "snapshot": stored if current else None}, status=202)
    return web.json_response({"status": "ready", "snapshot": stored})


async def abc_xyz_export(request: web.Request) -> web.Response:
    user_id = authorized_user_id(request)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ.")
    enforce_user_rate_limit(request, user_id)
    start, end = _abc_xyz_period(request)
    stored = _stored_abc_xyz()
    if not stored or stored.get("period_from") != start.isoformat() or stored.get("period_to") != (end - timedelta(days=1)).isoformat():
        raise web.HTTPConflict(text="Сначала рассчитайте выбранный период.")
    contents = build_abc_xyz_xlsx(stored)
    filename = f"Paradise_ABC-XYZ_{start:%d.%m.%Y}_{(end - timedelta(days=1)):%d.%m.%Y}.xlsx"
    return web.Response(body=contents, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def start_miniapp_server():
    global ORDER_STORAGE_OWNER_LOCK
    if LOCAL_MINIAPP_DEV and PUBLIC_MINIAPP_URL.startswith("https://"):
        raise RuntimeError("MINIAPP_LOCAL_DEV нельзя включать при заданном публичном MINIAPP_URL.")

    global _STARTUP_IN_PROGRESS
    with _STARTUP_STATE_GUARD:
        if _STARTUP_IN_PROGRESS:
            raise RuntimeError("Запуск Mini App уже выполняется в этом процессе.")
        if ORDER_STORAGE_OWNER_LOCK is not None and not ORDER_STORAGE_OWNER_LOCK.released:
            raise RuntimeError("Хранилище заказов уже запущено в этом процессе.")
        _STARTUP_IN_PROGRESS = True

    owner_lock = None
    runner = None
    try:
        owner_lock = await _acquire_order_storage_owner()
        ORDER_STORAGE_OWNER_LOCK = owner_lock

        def initialize_and_recover_orders():
            initialize_customer_orders_db()
            return recover_order_transactions(CUSTOMER_ORDERS_DB, CUSTOMER_ORDER_PHOTOS)

        recovery_task = asyncio.create_task(
            run_order_storage(initialize_and_recover_orders)
        )
        _recovered, unresolved = await _wait_for_task_completion(recovery_task)
        if unresolved:
            # Preserve ambiguous files and journals for manual inspection.
            print(f"Заказы: оставлено незавершённых операций для проверки: {unresolved}")
        app = web.Application(
            client_max_size=8 * 1024 * 1024,
            middlewares=[security_headers],
        )
        app.router.add_get("/", index_page)
        app.router.add_static("/assets/", MINIAPP_ASSETS_DIR, show_index=False)
        app.router.add_get("/api/stock", stock_data)
        app.router.add_get("/api/purchase-price", purchase_price_data)
        app.router.add_get("/api/purchase", purchase_data)
        app.router.add_get("/api/abc-xyz", abc_xyz_data)
        app.router.add_post("/api/abc-xyz", abc_xyz_data)
        app.router.add_get("/api/abc-xyz/export", abc_xyz_export)
        app.router.add_get("/api/customer-orders", customer_orders_data)
        app.router.add_post("/api/customer-orders", create_customer_order)
        app.router.add_get("/api/customer-orders/{order_id}/photo", customer_order_photo)
        app.router.add_delete("/api/customer-orders/{order_id}", delete_customer_order)

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "8000"))
        host = "127.0.0.1" if LOCAL_MINIAPP_DEV else "0.0.0.0"
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"Mini App server started on port {port}")
        return runner
    except BaseException:
        cleanup_task = asyncio.create_task(_cleanup_failed_startup(runner, owner_lock))
        try:
            await _wait_for_task_completion(cleanup_task)
        except BaseException:
            # Preserve the startup exception while ensuring cleanup has run.
            pass
        if ORDER_STORAGE_OWNER_LOCK is owner_lock:
            ORDER_STORAGE_OWNER_LOCK = None
        raise
    finally:
        with _STARTUP_STATE_GUARD:
            _STARTUP_IN_PROGRESS = False


async def _cleanup_failed_startup(runner, owner_lock) -> None:
    if runner is not None:
        try:
            await runner.cleanup()
        except BaseException:
            pass
    await _drain_executor_pools(owner_lock)


async def stop_miniapp_server(runner) -> None:
    global ORDER_STORAGE_OWNER_LOCK
    owner_lock = ORDER_STORAGE_OWNER_LOCK

    async def stop_resources():
        failure = None
        if runner is not None:
            try:
                await runner.cleanup()
            except BaseException as ex:
                failure = ex
        try:
            await _drain_executor_pools(owner_lock)
        except BaseException as ex:
            if failure is None:
                failure = ex
        if failure is not None:
            raise failure

    stop_task = asyncio.create_task(stop_resources())
    try:
        await _wait_for_task_completion(stop_task)
    finally:
        if owner_lock is None or owner_lock.released:
            ORDER_STORAGE_OWNER_LOCK = None
