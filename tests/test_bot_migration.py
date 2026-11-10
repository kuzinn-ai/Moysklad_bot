import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from keyboard import (
    get_sale_menu_keyboard,
    get_stock_menu_keyboard,
    get_stock_submenu_keyboard,
    stock_back_callback,
    stock_folder_callback,
    stock_goods_callback,
)


STOCK = {
    "A": {
        "_goods": [{"name": "x", "stock": 1, "price": 10}],
        "_children": {
            "B": {"_goods": [{"name": "y", "stock": 2, "price": 20}], "_children": {}}
        },
    },
    "C": {"_goods": [], "_children": {}},
}


def message(user_id=101):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())


def callback(data, user_id=101):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        message=message(user_id),
    )


def test_registered_update_types():
    assert set(bot.dp.resolve_used_update_types()) == {"message", "callback_query"}
    assert len(bot.dp.message.handlers) == 3
    assert len(bot.dp.callback_query.handlers) == 2


def test_dispatcher_routes_real_telegram_updates(monkeypatch):
    user = User(id=101, is_bot=False, first_name="Test")
    chat = Chat(id=101, type="private")
    telegram_message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="/start",
    )
    start_handler = bot.dp.message.handlers[0]
    stock_handler = bot.dp.callback_query.handlers[0]
    start_mock = AsyncMock()
    stock_mock = AsyncMock()
    monkeypatch.setattr(start_handler, "callback", start_mock)
    monkeypatch.setattr(stock_handler, "callback", stock_mock)

    asyncio.run(bot.dp.feed_update(bot.bot, Update(update_id=1, message=telegram_message)))
    start_mock.assert_awaited_once()

    stock_callback = CallbackQuery(
        id="test-callback",
        from_user=user,
        chat_instance="test-chat",
        message=telegram_message,
        data="stock_all_stock_button",
    )
    asyncio.run(bot.dp.feed_update(bot.bot, Update(update_id=2, callback_query=stock_callback)))
    stock_mock.assert_awaited_once()


def test_start_allowed_and_denied(monkeypatch):
    monkeypatch.setattr(bot, "users", [101])
    monkeypatch.setattr(bot, "miniapp_url", "https://example.org/app")
    allowed = message()
    asyncio.run(bot.start(allowed))
    assert allowed.answer.await_count == 2
    first_markup = allowed.answer.await_args_list[0].kwargs["reply_markup"]
    assert [[button.text for button in row] for row in first_markup.keyboard] == [["Остатки", "Продажи"]]
    second_markup = allowed.answer.await_args_list[1].kwargs["reply_markup"]
    assert second_markup.inline_keyboard[0][0].web_app.url == "https://example.org/app"

    denied = message(999)
    asyncio.run(bot.start(denied))
    assert denied.answer.await_count == 1
    assert "нет доступа" in denied.answer.await_args.args[0]


def test_menus_and_keyboard_rows(monkeypatch):
    monkeypatch.setattr(bot, "users", [101])
    monkeypatch.setattr(bot, "StockSalesReport", lambda: SimpleNamespace(get_goods_dict=lambda: STOCK))
    monkeypatch.setattr(bot, "run_blocking_task", AsyncMock(return_value=STOCK))
    monkeypatch.setattr(bot, "stock_result_message", lambda stock: "")
    stock_message = message()
    asyncio.run(bot.stock_menu(stock_message))
    stock_markup = stock_message.answer.await_args.kwargs["reply_markup"]
    assert [row[0].text for row in stock_markup.inline_keyboard] == ["Все остатки", "A", "C"]

    sales_message = message()
    asyncio.run(bot.sale_menu(sales_message))
    sales_markup = sales_message.answer.await_args.kwargs["reply_markup"]
    assert [len(row) for row in sales_markup.inline_keyboard] == [2, 2, 2]
    assert [len(row) for row in get_stock_menu_keyboard(STOCK).inline_keyboard] == [1, 1, 1]
    assert [len(row) for row in get_stock_submenu_keyboard(STOCK, ("A",)).inline_keyboard] == [1, 1, 1]
    assert [len(row) for row in get_sale_menu_keyboard().inline_keyboard] == [2, 2, 2]


def test_stock_callback_and_access_check(monkeypatch):
    monkeypatch.setattr(bot, "users", [101])
    monkeypatch.setattr(bot, "StockSalesReport", lambda: SimpleNamespace(get_goods_dict=lambda: STOCK))
    monkeypatch.setattr(bot, "run_blocking_task", AsyncMock(return_value=STOCK))
    monkeypatch.setattr(bot, "stock_result_message", lambda stock: "")
    allowed = callback("stock_all_stock_button")
    asyncio.run(bot.inline_keyboard_stock_buttons(allowed))
    assert allowed.answer.await_count == 1
    assert allowed.message.answer.await_count >= 1

    denied = callback("stock_all_stock_button", 999)
    asyncio.run(bot.inline_keyboard_stock_buttons(denied))
    denied.answer.assert_awaited_once()
    denied.message.answer.assert_not_awaited()


@pytest.mark.parametrize(
    "data,expected",
    [
        (stock_folder_callback(("A",)), "Выберите подгруппу"),
        (stock_goods_callback(("A",)), "send_text_in_parts"),
        (stock_back_callback(()), "Какую выгрузку сделать"),
    ],
)
def test_stock_navigation_callbacks(monkeypatch, data, expected):
    monkeypatch.setattr(bot, "users", [101])
    monkeypatch.setattr(bot, "StockSalesReport", lambda: SimpleNamespace(get_goods_dict=lambda: STOCK))
    monkeypatch.setattr(bot, "run_blocking_task", AsyncMock(return_value=STOCK))
    monkeypatch.setattr(bot, "stock_result_message", lambda stock: "")
    send_text = AsyncMock()
    monkeypatch.setattr(bot, "send_text_in_parts", send_text)
    selected = callback(data)
    asyncio.run(bot.inline_keyboard_stock_buttons(selected))
    if expected == "send_text_in_parts":
        send_text.assert_awaited_once()
        assert "A" in send_text.await_args.args[1]
    else:
        assert expected in selected.message.answer.await_args.args[0]


@pytest.mark.parametrize(
    "data",
    [
        "sales_today",
        "sales_yesterday",
        "sales_before_yesterday",
        "sales_this_week",
        "sales_this_month",
        "sales_month_before",
    ],
)
def test_sales_period_callbacks(monkeypatch, data):
    monkeypatch.setattr(bot, "users", [101])
    monkeypatch.setattr(bot, "give_dates_ago", lambda *args, **kwargs: ("from", "to"))
    monkeypatch.setattr(bot, "StockSalesReport", lambda **kwargs: SimpleNamespace(
        get_sales_turnover_outcome_report=lambda: "Отчёт",
        get_popular_goods=lambda: [],
    ))

    async def run_task(function):
        return function()

    monkeypatch.setattr(bot, "run_blocking_task", run_task)
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    selected = callback(data)
    asyncio.run(bot.inline_keyboard_sales_buttons(selected))
    selected.answer.assert_awaited_once()
    assert "Отчёт" in selected.message.answer.await_args.args[0]


def test_miniapp_lifecycle(monkeypatch):
    runner = object()
    start = AsyncMock(return_value=runner)
    stop = AsyncMock()
    monkeypatch.setattr(bot, "start_miniapp_server", start)
    monkeypatch.setattr(bot, "stop_miniapp_server", stop)
    asyncio.run(bot.dp.emit_startup(bot=bot.bot))
    asyncio.run(bot.dp.emit_shutdown(bot=bot.bot))
    start.assert_awaited_once()
    stop.assert_awaited_once_with(runner)
