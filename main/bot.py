import asyncio
import os
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

try:
    from .keyboard import (
        get_sale_menu_keyboard, get_stock_menu_keyboard, get_stock_submenu_keyboard,
        find_stock_node, find_stock_path, stock_folder_callback, stock_goods_callback,
        stock_back_callback,
    )
    from .main import StockSalesReport, give_dates_ago, short_product_name, stock_result_message
    from .settings import token_tg, users
    from .miniapp_server import start_miniapp_server, stop_miniapp_server
    from .blocking_tasks import ExecutorBusyError, run_blocking_task
except ImportError:
    from keyboard import (
        get_sale_menu_keyboard, get_stock_menu_keyboard, get_stock_submenu_keyboard,
        find_stock_node, find_stock_path, stock_folder_callback, stock_goods_callback,
        stock_back_callback,
    )
    from main import StockSalesReport, give_dates_ago, short_product_name, stock_result_message
    from settings import token_tg, users
    from miniapp_server import start_miniapp_server, stop_miniapp_server
    from blocking_tasks import ExecutorBusyError, run_blocking_task

bot = Bot(token=token_tg)
dp = Dispatcher()
miniapp_url = os.getenv("MINIAPP_URL", "").strip()
if miniapp_url and not miniapp_url.startswith("https://"):
    miniapp_url = ""
miniapp_runner = None


async def on_startup(bot: Bot) -> None:
    global miniapp_runner
    miniapp_runner = await start_miniapp_server()


async def on_shutdown(bot: Bot) -> None:
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

@dp.message(CommandStart())
async def start(message: types.Message) -> None:
    if message.from_user.id in users:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="Остатки"),
                KeyboardButton(text="Продажи"),
            ]],
            resize_keyboard=True,
        )
        await message.answer("Привет! Чем помочь?", reply_markup=keyboard)
        if miniapp_url:
            app_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть мини-приложение",
                    web_app=WebAppInfo(url=miniapp_url),
                ),
            ]])
            await message.answer("Новый интерфейс остатков:", reply_markup=app_keyboard)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


@dp.message(F.text == 'Остатки')
async def stock_menu(message: types.Message) -> None:
    if message.from_user.id in users:
        try:
            stock_tree = await run_blocking_task(StockSalesReport().get_goods_dict)
        except ExecutorBusyError:
            await message.answer("Сервис занят. Попробуйте ещё раз через несколько секунд.")
            return
        stock_message = stock_result_message(stock_tree)
        if stock_message:
            await message.answer(stock_message)
            return
        stock_menu_keyboard_1 = get_stock_menu_keyboard(stock_tree)
        await message.answer("Какую выгрузку сделать?", reply_markup=stock_menu_keyboard_1)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


@dp.callback_query(F.data.startswith('stock_'))
async def inline_keyboard_stock_buttons(callback: types.CallbackQuery) -> None:
    if callback.from_user.id not in users:
        await callback.answer("Извините, у вас нет доступа.")
        return
    await callback.answer()
    sr = StockSalesReport()
    try:
        stock_dict = await run_blocking_task(sr.get_goods_dict)
    except ExecutorBusyError:
        await callback.message.answer("Сервис занят. Попробуйте ещё раз через несколько секунд.")
        return
    stock_message = stock_result_message(stock_dict)
    if stock_message:
        await callback.message.answer(stock_message)
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


def common_product_prefix(goods: list) -> str:
    names = [good["name"].strip() for good in goods]
    if len(names) < 2:
        return ""

    prefix = os.path.commonprefix(names)
    last_space = prefix.rfind(" ")
    return prefix[:last_space + 1] if last_space >= 0 else ""


def format_stock_item_name(name: str, path: tuple, shared_prefix: str = "") -> str:
    name = name.strip()

    if shared_prefix and name.casefold().startswith(shared_prefix.casefold()):
        name = name[len(shared_prefix):].lstrip(" \t-–—:/")
    else:
        for prefix in reversed(path):
            if (
                name.casefold().startswith(prefix.casefold())
                and (
                    len(name) == len(prefix)
                    or name[len(prefix)] in " \t-–—:/"
                )
            ):
                name = name[len(prefix):].lstrip(" \t-–—:/")
                break

    return short_product_name(name)


def format_stock_goods(path: tuple, goods: list) -> str:
    total = sum(good["stock"] for good in goods)
    group_name = path[-1] if path else "Остатки"
    result = f"📦 {group_name}\nВсего: {total} шт.\n"
    shared_prefix = common_product_prefix(goods)

    for index, good in enumerate(goods):
        if index:
            result += "\n"
        name = format_stock_item_name(good["name"], path, shared_prefix)
        price = f"{good['price']:,}".replace(",", " ")
        result += f"  • {name} — {good['stock']} шт. · {price} ₽\n"

    return result


def format_stock_tree(stock_tree: dict) -> str:
    lines = []

    def sum_node_stock(branch: dict) -> int:
        return sum(
            sum(good["stock"] for good in node["_goods"])
            + sum_node_stock(node["_children"])
            for node in branch.values()
        )

    def visit(branch: dict, path: tuple = (), depth: int = 0) -> None:
        for name, node in sorted(branch.items(), key=lambda item: item[0].casefold()):
            current_path = path + (name,)
            total = sum(good["stock"] for good in node["_goods"])
            total += sum_node_stock(node["_children"])

            if lines:
                lines.append("")

            indent = "  " * depth
            lines.append(f"{indent}{name}: всего {total} шт.")
            shared_prefix = common_product_prefix(node["_goods"])

            for index, good in enumerate(node["_goods"]):
                if index:
                    lines.append("")
                item_name = format_stock_item_name(
                    good["name"], current_path, shared_prefix
                )
                price = f"{good['price']:,}".replace(",", " ")
                lines.append(
                    f"{indent}  • {item_name} — "
                    f"{good['stock']} шт. · {price} ₽"
                )

            visit(node["_children"], current_path, depth + 1)

    visit(stock_tree)
    return "\n".join(lines)


@dp.message(F.text == 'Продажи')
async def sale_menu(message:types.Message) -> None:
    if message.from_user.id in users:
        sale_menu_keyboard = get_sale_menu_keyboard()
        await message.answer('Продажи за какой период выгрузить?', reply_markup=sale_menu_keyboard)
    else:
        await message.answer("Извините, но у вас нет доступа к боту.")


async def build_sales_report(report: StockSalesReport):
    try:
        return await run_blocking_task(report.get_sales_turnover_outcome_report), None
    except ExecutorBusyError:
        return None, "busy"
    except Exception as ex:
        # Do not expose exception text, which can contain request details.
        print(f"Не удалось загрузить отчёт продаж: {type(ex).__name__}")
        return None, "error"


@dp.callback_query(F.data.startswith('sales_'))
async def inline_keyboard_sales_buttons(callback:types.CallbackQuery) -> None:
    if callback.from_user.id not in users:
        await callback.answer("Извините, у вас нет доступа.")
        return
    await callback.answer()
    if callback.data == 'sales_today':
        moment_from, moment_to = give_dates_ago(0)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    elif callback.data == "sales_yesterday":
        moment_from, moment_to = give_dates_ago(1)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    elif callback.data == 'sales_before_yesterday':
        moment_from, moment_to = give_dates_ago(2)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    elif callback.data == 'sales_this_week':
        start_of_week_days_ago = datetime.now(timezone.utc).astimezone().weekday()
        moment_from, moment_to = give_dates_ago(start_of_week_days_ago, curent_period=True)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    elif callback.data == 'sales_this_month':
        start_of_months_days_ago = int(datetime.now(timezone.utc).astimezone().strftime('%d')) - 1
        moment_from, moment_to = give_dates_ago(start_of_months_days_ago, curent_period=True)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    elif callback.data == 'sales_month_before':
        moment_from, moment_to = give_dates_ago(months_ago=1)

        report = StockSalesReport(moment_from=moment_from, moment_to=moment_to)
        result, report_status = await build_sales_report(report)

    if report_status == "busy":
        await callback.message.answer("Сервис занят. Попробуйте ещё раз через несколько секунд.")
        return
    if report_status == "error":
        await callback.message.answer("Не удалось получить отчёт продаж из МоегоСклада. Попробуйте позже.")
        return

    if callback.data in ('sales_this_week', 'sales_this_month', 'sales_month_before'):
        try:
            popular_goods = await run_blocking_task(report.get_popular_goods)
            popular_status = None
        except ExecutorBusyError:
            popular_goods = None
            popular_status = "busy"
        except Exception as ex:
            print(f"Не удалось загрузить популярные товары: {type(ex).__name__}")
            popular_goods = None
            popular_status = "error"
        if popular_status == "busy":
            result += "\n\nСервис занят: популярные товары сейчас загрузить не удалось."
        elif popular_status == "error":
            result += "\n\nНе удалось загрузить популярные товары из МоегоСклада."
        elif popular_goods:
            result += "\n\nПопулярные товары Paradise:\n"
            for good in popular_goods:
                name = short_product_name(good["name"])
                result += (
                    f"  • {name} — "
                    f"{good['quantity']:g} шт. · в чеках: {good['checks']}\n"
                )
        else:
            result += "\n\nЗа этот период продаж в Paradise не было."

    await send_text_in_parts(callback.message, result)

dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


async def main() -> None:
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    asyncio.run(main())
