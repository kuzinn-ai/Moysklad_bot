from datetime import date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
import asyncio
import json
import importlib
import sys
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import pytest
from aiohttp import web

from main.abc_xyz import calculate_analysis, three_complete_months
from main.abc_xyz_export import build_abc_xyz_xlsx


def assortment(number, name):
    return {"name": name, "meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/product/{number}"}}


def document(moment, product, quantity, price, discount=0):
    return {"moment": moment, "positions": {"rows": [{
        "assortment": product, "quantity": quantity, "price": price, "discount": discount,
    }]}}


def test_three_complete_months_crosses_year_boundary():
    assert three_complete_months(date(2026, 1, 15)) == (date(2025, 10, 1), date(2026, 1, 1))
    assert three_complete_months(date(2026, 11, 5)) == (date(2026, 8, 1), date(2026, 11, 1))


def test_returns_reduce_revenue_and_units_while_sparse_seller_is_classified():
    popular = assortment("popular", "Популярный")
    rare = assortment("rare", "Редкий")
    unsold = assortment("unsold", "Без продаж")
    rows = [{"assortment": item, "name": item["name"], "stock": 1} for item in (popular, rare, unsold)]
    docs = [("sale", document("2026-08-05 12:00:00", popular, 10, 10000)),
            ("sale", document("2026-09-05 12:00:00", popular, 10, 10000)),
            ("return", document("2026-09-06 12:00:00", popular, 2, 10000)),
            ("sale", document("2026-10-10 12:00:00", rare, 1, 5000, 20))]
    result = calculate_analysis(date(2026, 8, 1), date(2026, 11, 1), rows, docs)
    by_name = {item["name"]: item for item in result["items"]}
    assert result["total_revenue"] == 1840
    assert by_name["Популярный"]["revenue"] == 1800
    assert by_name["Популярный"]["quantity"] == 18
    assert by_name["Редкий"]["class"].endswith("Z")
    assert by_name["Без продаж"]["class"] == "CZ"
    assert by_name["Без продаж"]["note"] == "Без продаж"


def test_sold_out_item_remains_in_analysis():
    item = assortment("sold-out", "Продан без остатка")
    result = calculate_analysis(date(2026, 8, 1), date(2026, 11, 1), [], [
        ("sale", document("2026-08-10 12:00:00", item, 1, 15000))
    ])
    assert len(result["items"]) == 1
    assert result["items"][0]["stock"] == 0
    assert result["items"][0]["class"] == "AZ"


def test_saved_current_snapshot_is_reused_without_new_moysklad_call(monkeypatch, tmp_path):
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        miniapp_server = importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)

    start, end = three_complete_months(datetime.now(ZoneInfo("Europe/Moscow")).date())
    snapshot = {"period_from": start.isoformat(), "period_to": (end - timedelta(days=1)).isoformat(),
                "items": [], "counts": {}, "total_revenue": 0}
    path = tmp_path / "abc_xyz_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(miniapp_server, "ABC_XYZ_SNAPSHOT", path)
    monkeypatch.setattr(miniapp_server, "ABC_XYZ_TASK", None)
    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    async def unexpected_call(*args):
        raise AssertionError("Повторный запрос к МоемуСкладу")

    monkeypatch.setattr(miniapp_server, "run_moysklad_call", unexpected_call)
    result = asyncio.run(miniapp_server.abc_xyz_data(SimpleNamespace(method="GET", path="/api/abc-xyz")))
    assert json.loads(result.text) == {"status": "ready", "snapshot": snapshot}


def test_failed_refresh_returns_error_without_immediate_retry(monkeypatch, tmp_path):
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        miniapp_server = importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)
    monkeypatch.setattr(miniapp_server, "ABC_XYZ_SNAPSHOT", tmp_path / "missing.json")
    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)

    async def check():
        failed = asyncio.get_running_loop().create_future()
        failed.set_exception(RuntimeError("upstream error"))
        monkeypatch.setattr(miniapp_server, "ABC_XYZ_TASK", failed)
        result = await miniapp_server.abc_xyz_data(SimpleNamespace(method="GET", path="/api/abc-xyz"))
        assert result.status == 502
        assert miniapp_server.ABC_XYZ_TASK is None

    asyncio.run(check())


def test_excel_contains_complete_snapshot_and_treats_product_names_as_text():
    snapshot = {"period_from": "2026-08-01", "period_to": "2026-10-31", "calculated_at": "2026-11-05T09:00:00Z",
                "total_revenue": 200, "counts": {"AZ": 2}, "items": [
                    {"name": "=1+1", "class": "AZ", "revenue": 100, "quantity": 2, "weekly_average": 0.1,
                     "stock": 3, "active_weeks": 1, "cv": 2.1, "note": "Мало данных"},
                    {"name": "Товар", "class": "AZ", "revenue": 100, "quantity": 1, "weekly_average": 0.2,
                     "stock": 0.5, "active_weeks": 2, "cv": None, "note": ""},
                ]}
    with ZipFile(BytesIO(build_abc_xyz_xlsx(snapshot))) as book:
        assert book.testzip() is None
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        summary = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        goods = ET.fromstring(book.read("xl/worksheets/sheet2.xml"))
        assert "Текущий на момент расчёта" in ET.tostring(summary, encoding="unicode")
        assert goods.find("s:autoFilter", ns).attrib["ref"] == "A1:I3"
        assert goods.find("s:sheetViews/s:sheetView/s:pane", ns).attrib["state"] == "frozen"
        assert goods.find("s:sheetData/s:row[@r='2']/s:c[@r='A2']", ns).attrib["t"] == "inlineStr"
        assert goods.find("s:sheetData/s:row[@r='2']/s:c[@r='A2']/s:is/s:t", ns).text == "=1+1"
        assert goods.find(".//s:f", ns) is None
        assert goods.find("s:sheetData/s:row[@r='3']/s:c[@r='C3']/s:v", ns).text == "100"
        assert goods.find("s:sheetData/s:row[@r='2']/s:c[@r='C2']", ns).attrib["s"] == "2"
        assert "s" not in goods.find("s:sheetData/s:row[@r='2']/s:c[@r='F2']", ns).attrib
        assert goods.find("s:sheetData/s:row[@r='2']/s:c[@r='F2']/s:v", ns).text == "3"
        assert goods.find("s:sheetData/s:row[@r='3']/s:c[@r='F3']/s:v", ns).text == "0.5"


def test_custom_period_validation_and_excel_export_uses_exact_snapshot(monkeypatch, tmp_path):
    main_dir = str(Path(__file__).resolve().parents[1] / "main")
    sys.path.insert(0, main_dir)
    try:
        miniapp_server = importlib.import_module("main.miniapp_server")
    finally:
        sys.path.remove(main_dir)
    snapshot = {"period_from": "2026-08-01", "period_to": "2026-10-31", "items": [], "counts": {}, "total_revenue": 0}
    path = tmp_path / "abc_xyz_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(miniapp_server, "ABC_XYZ_SNAPSHOT", path)
    monkeypatch.setattr(miniapp_server, "authorized_user_id", lambda request: 1)
    monkeypatch.setattr(miniapp_server, "enforce_user_rate_limit", lambda request, user_id: None)
    request = SimpleNamespace(path="/api/abc-xyz/export", query={"from": "2026-08-01", "to": "2026-10-31"})
    response = asyncio.run(miniapp_server.abc_xyz_export(request))
    assert response.status == 200
    assert response.body[:2] == b"PK"
    assert "01.08.2026_31.10.2026" in response.headers["Content-Disposition"]
    request.query["to"] = "2026-09-30"
    with pytest.raises(web.HTTPConflict):
        asyncio.run(miniapp_server.abc_xyz_export(request))
    assert miniapp_server._abc_xyz_period(request) == (date(2026, 8, 1), date(2026, 10, 1))
    request.query["to"] = "2027-09-30"
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(miniapp_server.abc_xyz_export(request))
