from hashlib import sha1

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def _path_key(path: tuple) -> str:
    # Telegram callback_data is limited to 64 bytes; hash supports long/Cyrillic paths.
    value = "\x1f".join(path)
    return sha1(value.encode("utf-8")).hexdigest()[:16]


def stock_folder_callback(path: tuple) -> str:
    return f"stock_folder_{_path_key(path)}"


def stock_goods_callback(path: tuple) -> str:
    return f"stock_goods_{_path_key(path)}"


def stock_back_callback(path: tuple) -> str:
    return f"stock_back_{_path_key(path)}"


def find_stock_node(stock_tree: dict, path: tuple):
    children = stock_tree
    node = None
    for part in path:
        node = children.get(part)
        if node is None:
            return None
        children = node["_children"]
    return node


def find_stock_path(stock_tree: dict, callback_data: str, callback_builder):
    if callback_builder(()) == callback_data:
        return ()

    def walk(branch: dict, parent_path: tuple = ()):
        for name, node in branch.items():
            path = parent_path + (name,)
            if callback_builder(path) == callback_data:
                return path
            found = walk(node["_children"], path)
            if found:
                return found
        return None

    return walk(stock_tree)


def _folder_button(keyboard: InlineKeyboardMarkup, name: str, path: tuple) -> None:
    keyboard.insert(InlineKeyboardButton(text=name, callback_data=stock_folder_callback(path)))


def get_stock_menu_keyboard(stock_tree: dict) -> InlineKeyboardMarkup:
    # One button per row keeps long group names readable on Telegram.
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.insert(InlineKeyboardButton(text="Все остатки", callback_data="stock_all_stock_button"))
    for name in sorted(stock_tree, key=str.casefold):
        _folder_button(keyboard, name, (name,))
    return keyboard


def get_stock_submenu_keyboard(stock_tree: dict, path: tuple) -> InlineKeyboardMarkup:
    node = find_stock_node(stock_tree, path)
    keyboard = InlineKeyboardMarkup(row_width=1)
    if node is None:
        return keyboard

    for name in sorted(node["_children"], key=str.casefold):
        _folder_button(keyboard, name, path + (name,))

    if node["_goods"] and node["_children"]:
        keyboard.insert(InlineKeyboardButton(
            text=f"Товары группы «{path[-1]}»",
            callback_data=stock_goods_callback(path),
        ))

    parent_path = path[:-1]
    keyboard.insert(InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=stock_back_callback(parent_path),
    ))
    return keyboard


def get_sale_menu_keyboard() -> InlineKeyboardMarkup:
    sale_menu_keyboard = InlineKeyboardMarkup(row_width=2)
    sale_menu_keyboard.insert(InlineKeyboardButton(text="Сегодня", callback_data="sales_today"))
    sale_menu_keyboard.insert(InlineKeyboardButton(text="Вчера", callback_data="sales_yesterday"))
    sale_menu_keyboard.insert(InlineKeyboardButton(text="Позавчера", callback_data="sales_before_yesterday"))
    sale_menu_keyboard.insert(InlineKeyboardButton(text="За эту неделю", callback_data="sales_this_week"))
    sale_menu_keyboard.insert(InlineKeyboardButton(text="За этот месяц", callback_data="sales_this_month"))
    sale_menu_keyboard.insert(InlineKeyboardButton(text="За прошлый месяц", callback_data="sales_month_before"))
    return sale_menu_keyboard
