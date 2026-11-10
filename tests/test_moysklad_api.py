import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest
import requests
from aiohttp import web

from main.main import (
    MoySkladAPIError,
    StockSalesReport,
    stock_result_message,
    validate_next_href,
)


def miniapp_server_module():
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        return importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)


def response(payload=None, status=200, json_error=None):
    result = Mock()
    result.status_code = status
    result.headers = {}
    result.raise_for_status.return_value = None
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    if json_error:
        result.json.side_effect = json_error
    else:
        result.json.return_value = payload
    return result


def test_validate_next_href_accepts_expected_moysklad_page():
    href = "https://api.moysklad.ru/api/remap/1.2/report/stock/all?offset=100&limit=100"
    assert validate_next_href(href, "/api/remap/1.2/report/stock/all") == href


@pytest.mark.parametrize("href", [
    "http://api.moysklad.ru/api/remap/1.2/report/stock/all?offset=100",
    "https://evil.example/api/remap/1.2/report/stock/all?offset=100",
    "https://api.moysklad.ru:444/api/remap/1.2/report/stock/all?offset=100",
    "https://api.moysklad.ru/api/remap/1.2/entity/product?offset=100",
    "https://user@api.moysklad.ru/api/remap/1.2/report/stock/all?offset=100",
])
def test_validate_next_href_rejects_unsafe_url(href):
    with pytest.raises(MoySkladAPIError):
        validate_next_href(href, "/api/remap/1.2/report/stock/all")


def test_folder_pagination_follows_same_origin_href(monkeypatch):
    StockSalesReport._folder_paths_cache = None
    href = "https://api.moysklad.ru/api/remap/1.2/entity/productfolder?offset=100&limit=100"
    get = Mock(side_effect=[
        response({"rows": [], "meta": {"nextHref": href}}),
        response({"rows": [], "meta": {}}),
    ])
    monkeypatch.setattr("main.main.requests.get", get)

    assert StockSalesReport()._get_product_folder_paths() == {}
    assert [call.kwargs["url"] for call in get.call_args_list] == [
        "https://api.moysklad.ru/api/remap/1.2/entity/productfolder", href
    ]
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)


def test_folder_pagination_rejects_foreign_host_before_second_request(monkeypatch):
    StockSalesReport._folder_paths_cache = None
    get = Mock(return_value=response({
        "rows": [],
        "meta": {"nextHref": "https://evil.example/api/remap/1.2/entity/productfolder?offset=100"},
    }))
    monkeypatch.setattr("main.main.requests.get", get)

    with pytest.raises(MoySkladAPIError):
        StockSalesReport()._get_product_folder_paths()
    get.assert_called_once()


def test_pagination_rejects_repeated_page_href():
    href = "https://api.moysklad.ru/api/remap/1.2/report/stock/all?offset=100"
    with pytest.raises(MoySkladAPIError, match="повторил ссылку"):
        StockSalesReport._next_page(
            {"meta": {"nextHref": href}},
            "/api/remap/1.2/report/stock/all",
            {href},
        )


def test_stock_pagination_preserves_empty_success_and_stops_on_forbidden_href(monkeypatch):
    report = StockSalesReport()
    monkeypatch.setattr(report, "_get_product_folder_paths", lambda: {})
    good_href = "https://api.moysklad.ru/api/remap/1.2/report/stock/all?offset=100&limit=100"
    get = Mock(side_effect=[
        response({"rows": [], "meta": {"nextHref": good_href}}),
        response({"rows": [], "meta": {}}),
    ])
    monkeypatch.setattr("main.main.requests.get", get)

    assert report.get_goods_dict() == {}
    assert len(get.call_args_list) == 2
    assert get.call_args_list[0].kwargs["url"].startswith(report.stock_url_without_filter + "?")
    assert get.call_args_list[1].kwargs["url"] == good_href
    assert all(call.kwargs["headers"]["Authorization"].startswith("Bearer ") for call in get.call_args_list)

    get.reset_mock()
    get.return_value = response({
        "rows": [],
        "meta": {"nextHref": "https://evil.example/api/remap/1.2/report/stock/all?offset=100"},
    })
    assert report.get_goods_dict() is None
    get.assert_called_once()


def test_supply_price_pagination_accepts_next_href_and_rejects_foreign_host(monkeypatch):
    report = StockSalesReport()
    href = "https://api.moysklad.ru/api/remap/1.2/entity/supply?offset=100&limit=100"
    get = Mock(side_effect=[
        response({"rows": [], "meta": {"nextHref": href}}),
        response({"rows": [], "meta": {}}),
    ])
    monkeypatch.setattr("main.main.requests.get", get)
    assert report._latest_supply_price("https://api.moysklad.ru/api/remap/1.2/entity/product/abc") is None
    assert get.call_count == 2

    get.reset_mock()
    get.side_effect = None
    get.return_value = response({"rows": [], "meta": {
        "nextHref": "https://evil.example/api/remap/1.2/entity/supply?offset=100"
    }})
    with pytest.raises(MoySkladAPIError):
        report._latest_supply_price("https://api.moysklad.ru/api/remap/1.2/entity/product/abc")
    get.assert_called_once()


def test_popular_goods_pagination_accepts_and_rejects_next_href(monkeypatch):
    report = StockSalesReport(moment_from="2026-01-01 00:00:00", moment_to="2026-01-01 22:00:00")
    monkeypatch.setattr(report, "_get_product_folder_paths", lambda: {})
    href = "https://api.moysklad.ru/api/remap/1.2/entity/retaildemand?offset=100&limit=100"
    get = Mock(side_effect=[
        response({"rows": [], "meta": {"nextHref": href}}),
        response({"rows": [], "meta": {}}),
    ])
    monkeypatch.setattr("main.main.requests.get", get)
    assert report.get_popular_goods() == []
    assert get.call_count == 2

    get.reset_mock()
    get.side_effect = None
    get.return_value = response({"rows": [], "meta": {
        "nextHref": "https://evil.example/api/remap/1.2/entity/retaildemand?offset=100"
    }})
    with pytest.raises(MoySkladAPIError):
        report.get_popular_goods()
    get.assert_called_once()


def test_popular_goods_keeps_catalog_sale_price_without_current_stock(monkeypatch):
    report = StockSalesReport(moment_from="2026-11-01 00:00:00", moment_to="2026-11-05 22:00:00")
    monkeypatch.setattr(report, "_get_product_folder_paths", lambda: {})
    monkeypatch.setattr(report, "_latest_supply_price", lambda href: 220)
    product = {
        "id": "tobacco-1", "name": "Табак для кальяна BLACK BURN ORIGINAL - APPLE SHOCK",
        "meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/tobacco-1"},
        "productFolder": {"name": "Табак для кальяна"},
        "salePrices": [
            {"value": 30000, "priceType": {"name": "Оптовая"}},
            {"value": 26000, "priceType": {"name": "Цена продажи"}},
        ],
    }
    page = {"rows": [{"positions": {"rows": [{"assortment": product, "quantity": 1, "price": 25000}]}}], "meta": {}}
    monkeypatch.setattr(report, "_get", lambda url: response(page))
    goods = report.get_popular_goods()
    assert len(goods) == 1
    assert goods[0]["price"] == 260
    assert goods[0]["buy_price"] == 220


def test_purchase_recommendation_uses_catalog_price_when_stock_report_has_no_item(monkeypatch):
    miniapp_server = miniapp_server_module()
    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    class Report:
        def __init__(self, **kwargs):
            pass

        def get_popular_goods(self, top_limit):
            return [{"name": "Табак", "price": 260, "buy_price": 220, "quantity": 1, "checks": 1}]

        def get_goods_dict(self):
            return {}

    monkeypatch.setattr(miniapp_server, "StockSalesReport", Report)

    async def run_local(function, *args):
        return function()

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_local)
    miniapp_server.PURCHASE_CACHE.clear()
    request = SimpleNamespace(query={"period": "week"}, path="/api/purchase")
    result = asyncio.run(miniapp_server.purchase_data(request))
    assert json.loads(result.text)["items"][0]["price"] == 260


def test_http_redirect_is_not_followed_with_bearer_header(monkeypatch):
    report = StockSalesReport()
    redirect_response = response(status=302)
    redirect_response.headers["Location"] = "https://evil.example/steal"
    get = Mock(return_value=redirect_response)
    monkeypatch.setattr("main.main.requests.get", get)

    with pytest.raises(MoySkladAPIError):
        report._get("https://api.moysklad.ru/api/remap/1.2/report/stock/all")
    get.assert_called_once()
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize("method", ["get_sales", "get_turnover_outcome"])
@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.parametrize("failure", [
    requests.HTTPError("upstream status"),
    requests.Timeout("upstream timeout"),
    requests.exceptions.JSONDecodeError("bad json", "{", 0),
])
def test_sales_methods_propagate_http_timeout_and_json_failures(monkeypatch, method, status, failure):
    report = StockSalesReport(moment_from="2026-11-03 00:00:00", moment_to="2026-11-03 22:00:00")
    if isinstance(failure, requests.HTTPError):
        get = Mock(return_value=response(status=status))
        get.return_value.raise_for_status.side_effect = failure
    else:
        get = Mock(side_effect=failure) if isinstance(failure, requests.Timeout) else Mock(
            return_value=response(json_error=failure)
        )
    monkeypatch.setattr("main.main.requests.get", get)

    with pytest.raises((requests.RequestException, MoySkladAPIError)):
        getattr(report, method)()


def test_report_builder_rejects_none_turnover_instead_of_calling_items():
    report = StockSalesReport()
    report.get_sales = lambda: "unchanged successful sales summary"
    report.get_turnover_outcome = lambda: None
    with pytest.raises(MoySkladAPIError):
        report.get_sales_turnover_outcome_report()


def test_successful_report_text_contract_is_unchanged():
    report = StockSalesReport()
    report.get_sales = lambda: "За период с 03-11-2026 00:00:00 по 03-11-2026 22:00:00\nчеков: 2 шт., на сумму 15 руб."
    report.get_turnover_outcome = lambda: {"Категория": [{"name": "Товар", "outcome": 3}]}
    assert report.get_sales_turnover_outcome_report() == (
        "За период с 03-11-2026 00:00:00 по 03-11-2026 22:00:00\nчеков: 2 шт., на сумму 15 руб.\n"
        "\nКатегория:\n  • Товар — 3 шт.\n"
    )


@pytest.mark.parametrize("status", [401, 429, 503])
def test_miniapp_purchase_maps_upstream_timeout_and_api_errors(monkeypatch, status):
    miniapp_server = miniapp_server_module()

    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)
    request = SimpleNamespace(query={"period": "week"}, path="/api/purchase")

    async def run_timeout(*args):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_timeout)
    miniapp_server.PURCHASE_CACHE.clear()
    with pytest.raises(web.HTTPGatewayTimeout):
        asyncio.run(miniapp_server.purchase_data(request))

    async def run_api_error(*args):
        raise requests.HTTPError(f"upstream status {status}")

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_api_error)
    with pytest.raises(web.HTTPBadGateway):
        asyncio.run(miniapp_server.purchase_data(request))


def test_miniapp_purchase_maps_malformed_json_to_bad_gateway(monkeypatch):
    miniapp_server = miniapp_server_module()
    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    async def run_bad_json(*args):
        raise requests.exceptions.JSONDecodeError("bad json", "{", 0)

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_bad_json)
    miniapp_server.PURCHASE_CACHE.clear()
    request = SimpleNamespace(query={"period": "week"}, path="/api/purchase")
    with pytest.raises(web.HTTPBadGateway):
        asyncio.run(miniapp_server.purchase_data(request))


def test_miniapp_purchase_success_json_contract_is_preserved(monkeypatch):
    miniapp_server = miniapp_server_module()

    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    async def run_success(function, *args):
        return [{"name": "Товар", "quantity": 2, "buy_price": None}]

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_success)
    miniapp_server.PURCHASE_CACHE.clear()
    request = SimpleNamespace(query={"period": "week"}, path="/api/purchase")
    result = asyncio.run(miniapp_server.purchase_data(request))
    assert result.status == 200
    assert json.loads(result.text) == {
        "period": "week",
        "items": [{"name": "Товар", "quantity": 2, "buy_price": None}],
    }


def test_miniapp_purchase_price_keeps_null_success_contract(monkeypatch):
    miniapp_server = miniapp_server_module()

    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    async def run_without_price(*args):
        return None

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", run_without_price)
    request = SimpleNamespace(
        query={"href": "https://api.moysklad.ru/api/remap/1.2/entity/product/00000000-0000-0000-0000-000000000001"},
        path="/api/purchase-price",
    )
    result = asyncio.run(miniapp_server.purchase_price_data(request))
    assert result.status == 200
    assert json.loads(result.text) == {"buy_price": None}


def test_stock_menu_result_distinguishes_failure_from_empty(monkeypatch):
    assert stock_result_message(None) == "Не удалось загрузить остатки из МоегоСклада. Попробуйте позже."
    assert stock_result_message({}) == "В Paradise сейчас нет товаров с остатком."
    assert stock_result_message({"Товары": {"_goods": [], "_children": {}}}) is None


@pytest.mark.parametrize("mode,expected", [
    ("positive", ["Положительный"]),
    ("zero", ["Нулевой"]),
    ("negative", ["Отрицательный"]),
])
def test_stock_api_modes_keep_only_matching_goods(monkeypatch, mode, expected):
    server = miniapp_server_module()
    monkeypatch.setattr(server, "authorized_user_id", lambda request: 101)
    monkeypatch.setattr(server, "enforce_user_rate_limit", lambda request, user_id: None)
    tree = {"Категория": {"_goods": [
        {"name": "Положительный", "stock": 2},
        {"name": "Нулевой", "stock": 0},
        {"name": "Отрицательный", "stock": -3},
    ], "_children": {"Пустая группа": {"_goods": [], "_children": {}}}}}
    filters = []

    async def run_local(function):
        filters.append((function.__name__, function.__self__.filter))
        selected = {"_goods": [good for good in tree["Категория"]["_goods"]
                               if (good["stock"] > 0 if mode == "positive" else good["stock"] == 0 if mode == "zero" else good["stock"] < 0)],
                    "_children": {}}
        return {"Категория": selected}

    monkeypatch.setattr(server, "run_moysklad_call", run_local)
    result = asyncio.run(server.stock_data(SimpleNamespace(query={"mode": mode}, path="/api/stock")))
    goods = json.loads(result.text)["Категория"]["_goods"]
    assert [good["name"] for good in goods] == expected
    assert filters[0][0] == ("get_zero_stock_goods_dict" if mode == "zero" else "get_goods_dict")
    assert ("stockMode=positiveOnly" in filters[0][1]) is (mode == "positive")
    assert ("stockMode=negativeOnly" in filters[0][1]) is (mode == "negative")


def test_stock_api_rejects_unknown_mode_before_moysklad_call(monkeypatch):
    server = miniapp_server_module()
    monkeypatch.setattr(server, "authorized_user_id", lambda request: 101)
    monkeypatch.setattr(server, "enforce_user_rate_limit", lambda request, user_id: None)
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(server.stock_data(SimpleNamespace(query={"mode": "bad"}, path="/api/stock")))


def test_zero_stock_joins_active_catalog_with_store_stock(monkeypatch):
    report = StockSalesReport()
    href = "https://api.moysklad.ru/api/remap/1.2/entity/product/"
    monkeypatch.setattr(report, "get_goods_dict", lambda: {
        "Склад": {"_goods": [{"name": "Есть", "stock": 2, "purchase_href": href + "1?expand=productFolder"}], "_children": {}}
    })
    monkeypatch.setattr(report, "_get_product_folder_paths", lambda: {"folder": ("Склад",)})
    catalog = {"rows": [
        {"name": "Есть", "meta": {"type": "product", "href": href + "1"}, "productFolder": {"id": "folder"}},
        {"name": "Нет", "meta": {"type": "product", "href": href + "2"}, "productFolder": {"id": "folder"},
         "salePrices": [{"value": 25000, "priceType": {"name": "Цена продажи"}}]},
        {"name": "Услуга", "meta": {"type": "service", "href": href + "3"}},
        {"name": "Архив", "archived": True, "meta": {"type": "product", "href": href + "4"}},
    ], "meta": {}}
    get = Mock(return_value=response(catalog))
    monkeypatch.setattr(report, "_get", get)

    result = report.get_zero_stock_goods_dict()
    assert result["Склад"]["_goods"] == [{
        "name": "Нет", "stock": 0, "price": 250.0, "buy_price": None, "purchase_href": href + "2",
    }]
    assert "archived%3Dfalse" in get.call_args.args[0]
