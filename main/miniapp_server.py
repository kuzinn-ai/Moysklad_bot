import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from settings import token_tg, users

try:
    from .main import StockSalesReport
except ImportError:
    from main import StockSalesReport


MINIAPP_FILE = Path(__file__).resolve().parent.parent / "miniapp" / "index.html"
MAX_INIT_DATA_AGE_SECONDS = 60 * 60


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
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("tma "):
        raise web.HTTPUnauthorized(text="Откройте приложение через Telegram.")

    user_id = _validate_telegram_init_data(authorization[4:])
    if user_id is None:
        raise web.HTTPUnauthorized(text="Не удалось подтвердить доступ Telegram.")

    loop = asyncio.get_running_loop()
    stock_tree = await loop.run_in_executor(None, StockSalesReport().get_goods_dict)
    if stock_tree is None:
        raise web.HTTPBadGateway(text="МойСклад временно не ответил. Попробуйте ещё раз.")
    return web.json_response(stock_tree)


async def start_miniapp_server():
    app = web.Application()
    app.router.add_get("/", index_page)
    app.router.add_get("/api/stock", stock_data)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Mini App server started on port {port}")
    return runner


async def stop_miniapp_server(runner) -> None:
    if runner is not None:
        await runner.cleanup()
