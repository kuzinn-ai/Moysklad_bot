"""Small, synthetic data set for the isolated test bot. No production data."""

from copy import deepcopy
from datetime import datetime, timedelta


PRODUCTS = (
    ("ТЕСТ — Устройство", "11111111-1111-4111-8111-111111111111", 7, 1200, 700),
    ("ТЕСТ — Расходник", "22222222-2222-4222-8222-222222222222", 0, 300, 180),
    ("ТЕСТ — Дефицит", "33333333-3333-4333-8333-333333333333", -2, 450, 250),
)


def product_href(product):
    return f"https://api.moysklad.ru/api/remap/1.2/entity/product/{product[1]}"


def stock_tree(mode="nonEmpty"):
    if mode == "positiveOnly":
        selected = [product for product in PRODUCTS if product[2] > 0]
    elif mode == "negativeOnly":
        selected = [product for product in PRODUCTS if product[2] < 0]
    elif mode == "zero":
        selected = [product for product in PRODUCTS if product[2] == 0]
    else:
        selected = [product for product in PRODUCTS if product[2] != 0]
    return {"Тестовые товары": {"_goods": [
        {"name": name, "stock": stock, "price": price, "buy_price": buy_price,
         "purchase_href": product_href(product)}
        for product in selected
        for name, _, stock, price, buy_price in (product,)
    ], "_children": {}}}


def purchase_goods(top_limit=5):
    return deepcopy([
        {"name": name, "price": price, "checks": 2, "quantity": 3,
         "category": "Тестовые товары", "category_key": "disposable",
         "buy_price": buy_price}
        for name, _, _, price, buy_price in PRODUCTS[:top_limit]
    ])


def latest_supply_price(href):
    return next((product[4] for product in PRODUCTS if product_href(product) == href), None)


def sales_report(moment_from, moment_to):
    return f"ТЕСТОВЫЕ ДАННЫЕ за период {moment_from} — {moment_to}\nТестовые товары: 3 шт."


def analysis(start, end):
    try:
        from .abc_xyz import calculate_analysis
    except ImportError:
        from abc_xyz import calculate_analysis

    stock_rows = [
        {"name": product[0], "stock": product[2],
         "assortment": {"meta": {"href": product_href(product)}}}
        for product in PRODUCTS
    ]
    documents = []
    day = start
    while day < end:
        for product in PRODUCTS[:2]:
            documents.append(("sale", {
                "moment": datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S"),
                "positions": {"rows": [{"quantity": 1, "price": product[3] * 100,
                    "assortment": {"meta": {"href": product_href(product)}}}]},
            }))
        day += timedelta(days=7)
    return calculate_analysis(start, end, stock_rows, documents)
