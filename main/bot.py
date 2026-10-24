import asyncio
import os
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types, executor
from aiogram.dispatcher.filters import Text
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from keyboard import (
    get_sale_menu_keyboard,
    get_stock_menu_keyboard,
    get_stock_submenu_keyboard,
    find_stock_node,
    find_stock_path,
    stock_folder_callback,
    stock_goods_callback,
    stock_back_callback,
)
from main import StockSalesReport, give_dates_ago
from settings import token_tg, users
from miniapp_server import start_miniapp_server, stop_miniapp_server

bot = Bot(token=token_tg)
dp = Dispatcher(bot)
miniapp_url = os.getenv("MINIAPP_URL", "").strip()
if miniapp_url and not miniapp_url.startswith("https://"):
    miniapp_url = ""
miniapp_runner = None


async def on_startup(dispatcher: Dispatcher) -> None:
    global miniapp_runner
    miniapp_runner = await start_miniapp_server()


async def on_shutdown(dispatcher: Dispatcher) -> None:
    await stop_miniapp_server(miniapp_runner)


async def send_text_in_parts(message: types.Message, text: str) -> None:
    limit = 3500
    chunk = ""

    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > limit:
            await message.answer(chunk)
            await asyncio.sleep(1.1)
            chunk = ""
        chunk += line + "\n"

    if chunk:
        await message.answer(chunk)
        await asyncio.sleep(1.1)

@dp.message_handler(commands='start')
async def start(message: types.Message) -> None:
    if message.from_user.id in users:
        start_buttons = ['Остатки', "Продажи"]
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(*start_buttons)
        await message.answer("Привет! Чем помочь?", reply_markup=keyboard)
        if miniapp_url:
            app_keyboard = InlineKeyboardMarkup()
            app_keyboard.add(InlineKeyboardButton(
                text="Открыть мини-приложение",
                web_app=WebAppInfo(url=miniapp_url),
            ))
            await message.answer("Новый интерфейс остатков:", reply_markup=app_keyboard)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


@dp.message_handler(Text(equals='Остатки'))
async def stock_menu(message: types.Message) -> None:
    if message.from_user.id in users:
        stock_tree = StockSalesReport().get_goods_dict()
        if not stock_tree:
            await message.answer("В Paradise сейчас нет товаров с остатком.")
            return
        stock_menu_keyboard_1 = get_stock_menu_keyboard(stock_tree)
        await message.answer("Какую выгрузку сделать?", reply_markup=stock_menu_keyboard_1)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


@dp.callback_query_handler(lambda callback_query: callback_query.data.startswith('stock_'))
async def inline_keyboard_stock_buttons(callback: types.CallbackQuery) -> None:
    if callback.from_user.id not in users:
        await callback.answer("Извините, у вас нет доступа.")
        return
    await callback.answer()
    sr = StockSalesReport()
    stock_dict = sr.get_goods_dict()
    if not stock_dict:
        await callback.message.answer("Не удалось получить товары с остатками в Paradise. Проверьте соединение и токен МоегоСклада.")
        return

    if callback.data == "stock_all_stock_button":
        await send_text_in_parts(callback.message, format_stock_tree(stock_dict))
        return

    if callback.data.startswith("stock_back_"):
        path = find_stock_path(stock_dict, callback.data, stock_back_callback)
        if path is None:
            await callback.message.answer("Группа обновилась. Откройте «Остатки» ещё раз.")
        elif path:
            await callback.message.answer(
                f"Группа: {' / '.join(path)}",
                reply_markup=get_stock_submenu_keyboard(stock_dict, path),
            )
        else:
            await callback.message.answer(
                "Какую выгрузку сделать?",
                reply_markup=get_stock_menu_keyboard(stock_dict),
            )
        return

    if callback.data.startswith("stock_goods_"):
        path = find_stock_path(stock_dict, callback.data, stock_goods_callback)
        node = find_stock_node(stock_dict, path) if path is not None else None
        if node:
            await send_text_in_parts(callback.message, format_stock_goods(path, node["_goods"]))
        else:
            await callback.message.answer("Группа обновилась. Откройте «Остатки» ещё раз.")
        return

    if callback.data.startswith("stock_folder_"):
        path = find_stock_path(stock_dict, callback.data, stock_folder_callback)
        node = find_stock_node(stock_dict, path) if path is not None else None
        if node is None:
            await callback.message.answer("Группа обновилась. Откройте «Остатки» ещё раз.")
        elif node["_children"]:
            await callback.message.answer(
                f"Выберите подгруппу: {' / '.join(path)}",
                reply_markup=get_stock_submenu_keyboard(stock_dict, path),
            )
        else:
            await send_text_in_parts(callback.message, format_stock_goods(path, node["_goods"]))


def format_stock_goods(path: tuple, goods: list) -> str:
    total = sum(good["stock"] for good in goods)
    result = f"{' / '.join(path)}: всего {total} шт.\n"
    for good in goods:
        result += f"  {good['name']}: остаток {good['stock']} шт., цена {good['price']} руб.\n"
    return result


def format_stock_tree(stock_tree: dict) -> str:
    lines = []

    def visit(branch: dict, path: tuple = ()) -> None:
        for name, node in sorted(branch.items(), key=lambda item: item[0].casefold()):
            current_path = path + (name,)
            total = sum(good["stock"] for good in node["_goods"])
            total += sum_node_stock(node["_children"])
            indent = "  " * len(path)
            lines.append(f"{indent}{name}: всего {total} шт.")
            for good in node["_goods"]:
                lines.append(f"{indent}  {good['name']}: остаток {good['stock']} шт., цена {good['price']} руб.")
            visit(node["_children"], current_path)

    def sum_node_stock(branch: dict) -> int:
        return sum(
            sum(good["stock"] for good in node["_goods"]) + sum_node_stock(node["_children"])
            for node in branch.values()
        )

    visit(stock_tree)
    return "\n".join(lines)


@dp.message_handler(Text(equals='Продажи'))
async def sale_menu(message:types.Message) -> None:
    if message.from_user.id in users:
        sale_menu_keyboard = get_sale_menu_keyboard()
        await message.answer('Продажи за какой период выгрузить?', reply_markup=sale_menu_keyboard)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


@dp.callback_query_handler(lambda callback_query: callback_query.data.startswith('sales_'))
async def inline_keyboard_sales_buttons(callback:types.CallbackQuery) -> None:
    if callback.from_user.id not in users:
        await callback.answer("Извините, у вас нет доступа.")
        return
    await callback.answer()
    date_format = "%Y-%m-%d"

    if callback.data == 'sales_today':
        moment_from, moment_to = give_dates_ago(0)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    elif callback.data == "sales_yesterday":
        moment_from, moment_to = give_dates_ago(1)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    elif callback.data == 'sales_before_yesterday':
        moment_from, moment_to = give_dates_ago(2)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    elif callback.data == 'sales_this_week':
        start_of_week_days_ago = datetime.now(timezone.utc).astimezone().weekday()
        moment_from, moment_to = give_dates_ago(start_of_week_days_ago, curent_period=True)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    elif callback.data == 'sales_this_month':
        start_of_months_days_ago = int(datetime.now(timezone.utc).astimezone().strftime('%d')) - 1
        moment_from, moment_to = give_dates_ago(start_of_months_days_ago, curent_period=True)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    elif callback.data == 'sales_month_before':
        time_now = datetime.now(timezone.utc).astimezone()
        moment_from, moment_to = give_dates_ago(months_ago=1)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result = report.get_sales_turnover_outcome_report()

    await callback.message.answer(result)


if __name__ == '__main__':
    executor.start_polling(dispatcher=dp, on_startup=on_startup, on_shutdown=on_shutdown)
