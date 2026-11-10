"""ABC-XYZ analysis of completed retail sales in the Paradise store."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import sqrt
from urllib.parse import urlencode, urlsplit


def three_complete_months(today: date) -> tuple[date, date]:
    end = today.replace(day=1)
    month = end.month - 3
    year = end.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), end


def _number(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _assortment_key(assortment: dict) -> str | None:
    href = (assortment.get("meta") or {}).get("href", "")
    parsed = urlsplit(href)
    if parsed.hostname != "api.moysklad.ru" or not parsed.path.startswith("/api/remap/1.2/entity/"):
        return None
    return parsed.path


def calculate_analysis(start: date, end: date, stock_rows: list, documents: list) -> dict:
    """Classify stock and sold assortments; amounts are in MoySklad kopecks."""
    products = {}
    first_monday = start - timedelta(days=start.weekday())
    week_count = ((end - first_monday).days + 6) // 7

    def product_for(key, name):
        return products.setdefault(key, {
            "id": key, "name": name or "Без названия", "stock": 0,
            "revenue_kopecks": Decimal(0), "quantity": Decimal(0),
            "weeks": [Decimal(0) for _ in range(week_count)], "sales": 0,
        })

    for row in stock_rows:
        key = _assortment_key(row.get("assortment") or row)
        if not key:
            continue
        item = product_for(key, row.get("name"))
        item["name"] = row.get("name") or item["name"]
        item["stock"] = float(_number(row.get("stock")))

    for kind, document in documents:
        try:
            moment = datetime.strptime(document["moment"][:19], "%Y-%m-%d %H:%M:%S").date()
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= moment < end:
            continue
        week = (moment - first_monday).days // 7
        sign = -1 if kind == "return" else 1
        for position in (document.get("positions") or {}).get("rows", []):
            assortment = position.get("assortment") or {}
            key = _assortment_key(assortment)
            if not key or (assortment.get("meta") or {}).get("type") == "service":
                continue
            quantity = _number(position.get("quantity"))
            if quantity <= 0:
                continue
            item = product_for(key, assortment.get("name"))
            gross = quantity * _number(position.get("price"))
            revenue = gross * (Decimal(1) - _number(position.get("discount")) / 100)
            item["quantity"] += sign * quantity
            item["revenue_kopecks"] += sign * revenue
            item["weeks"][week] += sign * quantity
            if sign > 0:
                item["sales"] += 1

    ranked = sorted(products.values(), key=lambda item: (-max(Decimal(0), item["revenue_kopecks"]), item["name"].casefold()))
    total = sum((max(Decimal(0), item["revenue_kopecks"]) for item in ranked), Decimal(0))
    preceding = Decimal(0)
    counts = {f"{a}{x}": 0 for a in "ABC" for x in "XYZ"}
    output = []
    for item in ranked:
        positive_revenue = max(Decimal(0), item["revenue_kopecks"])
        if positive_revenue <= 0 or total <= 0:
            abc = "C"
        else:
            share_before = preceding / total
            abc = "A" if share_before < Decimal("0.80") else "B" if share_before < Decimal("0.95") else "C"
        preceding += positive_revenue

        rates = []
        for index, quantity in enumerate(item["weeks"]):
            monday = first_monday + timedelta(days=7 * index)
            covered_days = (min(end, monday + timedelta(days=7)) - max(start, monday)).days
            rates.append(max(0.0, float(quantity)) * 7 / covered_days)
        mean = sum(rates) / len(rates) if rates else 0
        variance = sum((rate - mean) ** 2 for rate in rates) / len(rates) if rates else 0
        cv = sqrt(variance) / mean if mean > 0 else None
        active_weeks = sum(rate > 0 for rate in rates)
        xyz = "Z" if cv is None or active_weeks < 3 or cv > 1 else "X" if cv <= 0.5 else "Y"
        classification = abc + xyz
        counts[classification] += 1
        output.append({
            "id": item["id"], "name": item["name"], "class": classification,
            "revenue": round(float(item["revenue_kopecks"] / 100), 2),
            "quantity": round(float(item["quantity"]), 3),
            "stock": item["stock"], "weekly_average": round(mean, 2),
            "cv": round(cv, 2) if cv is not None else None,
            "active_weeks": active_weeks, "sales": item["sales"],
            "note": "Без продаж" if item["sales"] == 0 else "Мало данных" if active_weeks < 3 else "",
        })
    return {"period_from": start.isoformat(), "period_to": (end - timedelta(days=1)).isoformat(),
            "total_revenue": round(float(total / 100), 2), "counts": counts, "items": output}


def fetch_analysis(report, start: date, end: date) -> dict:
    """Read full paginated store stock, retail sales and returns from MoySklad."""
    def pages(base_url, params, path):
        url = f"{base_url}?{urlencode(params)}"
        visited = {url}
        while url:
            response = report._get(url)
            response.raise_for_status()
            page = report._json_page(response)
            yield page
            url = report._next_page(page, path, visited)

    stock = []
    for page in pages(report.stock_url_without_filter, {
        "filter": f"store={report.store_href};stockMode=nonEmpty", "limit": 100,
    }, "/api/remap/1.2/report/stock/all"):
        stock.extend(page["rows"])

    documents = []
    for entity, kind in (("retaildemand", "sale"), ("retailsalesreturn", "return")):
        path = f"/api/remap/1.2/entity/{entity}"
        for page in pages("https://api.moysklad.ru" + path, {
            "filter": (f"retailStore={report.retail_store_href};"
                       f"moment>={start:%Y-%m-%d} 00:00:00;"
                       f"moment<{end:%Y-%m-%d} 00:00:00;applicable=true"),
            "limit": 100, "expand": "positions.assortment",
        }, path):
            for document in page["rows"]:
                positions = document.get("positions") or {}
                if positions.get("meta", {}).get("nextHref"):
                    position_path = f"{path}/{document['id']}/positions"
                    position_visited = set()
                    position_url = report._next_page(positions, position_path, position_visited)
                    while position_url:
                        response = report._get(position_url, params={"expand": "assortment"})
                        response.raise_for_status()
                        position_page = report._json_page(response)
                        positions["rows"].extend(position_page["rows"])
                        position_url = report._next_page(position_page, position_path, position_visited)
                documents.append((kind, document))
    return calculate_analysis(start, end, stock, documents)
