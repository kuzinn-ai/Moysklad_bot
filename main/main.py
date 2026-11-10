from calendar import monthrange
from urllib.parse import urlencode
from datetime import timedelta, datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import time

import requests

try:
    from .settings import token_ms
except ImportError:
    from settings import token_ms


# Bound the time each synchronous MoySklad request can occupy a worker.
MOYSKLAD_TIMEOUT = (5, 20)
MOYSKLAD_HOST = "api.moysklad.ru"
MOYSKLAD_PORT = 443


class MoySkladAPIError(RuntimeError):
    """Raised when MoySklad returns an unusable response or pagination link."""


def stock_result_message(stock_tree: dict | None) -> str | None:
    if stock_tree is None:
        return "Не удалось загрузить остатки из МоегоСклада. Попробуйте позже."
    if not stock_tree:
        return "В Paradise сейчас нет товаров с остатком."
    return None


def validate_next_href(next_href: str, expected_path: str) -> str:
    """Accept only HTTPS pagination links for this endpoint on MoySklad."""
    if not isinstance(next_href, str) or not next_href:
        raise MoySkladAPIError("МойСклад вернул некорректную ссылку пагинации.")
    try:
        parsed = urlsplit(next_href)
        port = parsed.port
    except ValueError as ex:
        raise MoySkladAPIError("МойСклад вернул некорректную ссылку пагинации.") from ex

    if (
        parsed.scheme != "https"
        or parsed.hostname != MOYSKLAD_HOST
        or port not in (None, MOYSKLAD_PORT)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.fragment
    ):
        raise MoySkladAPIError("МойСклад вернул недопустимую ссылку пагинации.")
    return next_href


def format_report_datetime(value: str) -> str:
    report_datetime = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return report_datetime.strftime("%d-%m-%Y %H:%M:%S")


def short_product_name(name: str, category: str = None) -> str:
    name = name.strip()
    if category:
        category_prefix = category.strip()
        if name.casefold().startswith(category_prefix.casefold()):
            name = name[len(category_prefix):].lstrip(" \t-–—:/")
    normalized = name.replace(" – ", " - ").replace(" — ", " - ")
    return normalized.rsplit(" - ", 1)[-1].strip()


class StockSalesReport:
    _folder_paths_cache = None
    _folder_paths_cache_time = 0

    def __init__(self, filter_url: str = "store=https://api.moysklad.ru/api/remap/1.2/entity/store/26a0c5fd-e09b-11ef-0a80-06b5003c6050;stockMode=nonEmpty", moment_from: str = None, moment_to: str = None) -> None:
        self.headers = {
            "Authorization": f"Bearer {token_ms}",
            "Content-Type": "application/json",
            "charset": "UTF-8"
        }
        self.store_href = "https://api.moysklad.ru/api/remap/1.2/entity/store/26a0c5fd-e09b-11ef-0a80-06b5003c6050"
        self.retail_store_href = "https://api.moysklad.ru/api/remap/1.2/entity/retailstore/27862728-e09b-11ef-0a80-06b5003c6083"
        self.stock_url_without_filter = 'https://api.moysklad.ru/api/remap/1.2/report/stock/all'  # расширенный отчет об остатках
        self.product_folders_url = 'https://api.moysklad.ru/api/remap/1.2/entity/productfolder'
        self.filter = filter_url

        self.sales_url_without_dates = 'https://api.moysklad.ru/api/remap/1.2/report/sales/plotseries'  # показатели продаж
        self.turnover_url_without_dates = 'https://api.moysklad.ru/api/remap/1.2/report/turnover/all'  # обороты
        self.moment_from = moment_from  # формат 2023-04-24 00:00:00
        self.moment_to = moment_to

    def _get(self, url: str, **kwargs):
        """Make a non-redirecting request so Authorization cannot follow redirects."""
        response = requests.get(
            url=url,
            headers=self.headers,
            timeout=MOYSKLAD_TIMEOUT,
            allow_redirects=False,
            **kwargs,
        )
        if 300 <= response.status_code < 400:
            raise MoySkladAPIError("МойСклад вернул неожиданный HTTP-редирект.")
        return response

    @staticmethod
    def _json_page(response) -> dict:
        page = response.json()
        if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
            raise MoySkladAPIError("МойСклад вернул некорректную страницу данных.")
        meta = page.get("meta", {})
        if not isinstance(meta, dict):
            raise MoySkladAPIError("МойСклад вернул некорректные данные пагинации.")
        return page

    @staticmethod
    def _next_page(page: dict, expected_path: str, visited: set) -> str | None:
        next_href = page.get("meta", {}).get("nextHref")
        if not next_href:
            return None
        next_href = validate_next_href(next_href, expected_path)
        if next_href in visited:
            raise MoySkladAPIError("МойСклад повторил ссылку страницы.")
        visited.add(next_href)
        return next_href

    def _get_product_folder_paths(self) -> dict:
        """Return folder ID -> full folder path using MoySklad's catalog tree."""
        if (
            StockSalesReport._folder_paths_cache is not None
            and time.time() - StockSalesReport._folder_paths_cache_time < 60
        ):
            return StockSalesReport._folder_paths_cache

        folders_by_id = {}
        next_url = self.product_folders_url
        visited = {next_url}

        while next_url:
            response = self._get(next_url)
            response.raise_for_status()
            page = self._json_page(response)
            for folder in page.get('rows', []):
                meta = folder.get('meta', {})
                folder_id = folder.get('id')
                if not folder_id and meta.get('href'):
                    folder_id = meta['href'].rstrip('/').split('/')[-1]
                if folder_id:
                    folders_by_id[folder_id] = folder
            next_url = self._next_page(page, "/api/remap/1.2/entity/productfolder", visited)

        paths = {}
        for folder_id, folder in folders_by_id.items():
            parent_path = folder.get('pathName') or ''
            parts = [part.strip() for part in parent_path.split('/') if part.strip()]
            # This is MoySklad's catalog root, not a user-created product group.
            parts = [part for part in parts if part.casefold() != 'товары и услуги']
            if folder.get('name'):
                parts.append(folder['name'].strip())
            paths[folder_id] = tuple(parts)
        StockSalesReport._folder_paths_cache = paths
        StockSalesReport._folder_paths_cache_time = time.time()
        return paths

    @staticmethod
    def _folder_id(folder: dict) -> str:
        folder_id = folder.get('id')
        if not folder_id and folder.get('meta', {}).get('href'):
            folder_id = folder['meta']['href'].rstrip('/').split('/')[-1]
        return folder_id

    def get_goods_dict(self) -> dict:
        """Build a hierarchy containing only goods with stock in the selected store."""
        filter_url = urlencode({"filter": f"{self.filter}"})
        stock_url = f"{self.stock_url_without_filter}?{filter_url}"
        try:
            goods = []
            next_url = stock_url
            visited = {next_url}
            while next_url:
                stock_response = self._get(next_url)
                stock_response.raise_for_status()
                stock_page = self._json_page(stock_response)
                goods.extend(stock_page.get('rows', []))
                next_url = self._next_page(stock_page, "/api/remap/1.2/report/stock/all", visited)

            try:
                folder_paths = self._get_product_folder_paths()
            except Exception as ex:
                # Some tokens may not have folder-list access. The stock report
                # can still provide pathName; fall back to that data if present.
                print(f'Не удалось загрузить дерево групп МоегоСклада: {ex}')
                folder_paths = {}

            result = {}

            for good in goods:
                try:
                    folder = good.get('folder') or {}
                    folder_id = self._folder_id(folder)
                    path = folder_paths.get(folder_id)
                    if not path:
                        path_name = folder.get('pathName') or ''
                        parts = [part.strip() for part in path_name.split('/') if part.strip()]
                        parts = [part for part in parts if part.casefold() != 'товары и услуги']
                        if folder.get('name'):
                            parts.append(folder['name'].strip())
                        path = tuple(parts) or ('Без категории',)

                    current = result
                    for part in path[:-1]:
                        current = current.setdefault(part, {'_goods': [], '_children': {}})['_children']
                    leaf = current.setdefault(path[-1], {'_goods': [], '_children': {}})
                    leaf['_goods'].append({
                        'name': good['name'],
                        'stock': int(good['stock']),
                        'price': int(good['salePrice'] / 100),
                        'buy_price': self._price_in_rubles(good.get('buyPrice')),
                        'purchase_href': (good.get('assortment') or {}).get('meta', {}).get('href')
                            or good.get('meta', {}).get('href'),
                    })
                except Exception as ex:
                    print(f"Для {good['name']} выгрузить не получилось! {ex}")

            return result

        except Exception as ex:
            print(f'Проблема с подключением к API МойСклад: {ex}')

    def get_zero_stock_goods_dict(self) -> dict | None:
        """Find active catalog goods absent from this store's nonzero stock report."""
        stock_tree = self.get_goods_dict()
        if stock_tree is None:
            return None

        stock_by_href = {}

        def identity(href):
            parsed = urlsplit(href)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))

        def collect_stock(branch):
            for node in branch.values():
                for good in node.get('_goods', []):
                    href = good.get('purchase_href')
                    if href:
                        stock_by_href[identity(href)] = good
                collect_stock(node.get('_children', {}))

        collect_stock(stock_tree)
        try:
            try:
                folder_paths = self._get_product_folder_paths()
            except Exception:
                folder_paths = {}
            catalog_url = 'https://api.moysklad.ru/api/remap/1.2/entity/assortment'
            next_url = f"{catalog_url}?{urlencode({'filter': 'archived=false', 'limit': 1000})}"
            visited = {next_url}
            result = {}
            while next_url:
                response = self._get(next_url)
                response.raise_for_status()
                page = self._json_page(response)
                for product in page['rows']:
                    if product.get('archived') or product.get('meta', {}).get('type') not in {'product', 'variant'}:
                        continue
                    href = product.get('meta', {}).get('href')
                    if not href:
                        continue
                    href = identity(href)
                    if stock_by_href.get(href, {}).get('stock', 0) != 0:
                        continue
                    folder = product.get('productFolder') or {}
                    path = folder_paths.get(self._folder_id(folder))
                    if not path:
                        path = tuple(part.strip() for part in (product.get('pathName') or '').split('/')
                                     if part.strip() and part.strip().casefold() != 'товары и услуги')
                    path = path or ('Без категории',)
                    current = result
                    for part in path[:-1]:
                        current = current.setdefault(part, {'_goods': [], '_children': {}})['_children']
                    leaf = current.setdefault(path[-1], {'_goods': [], '_children': {}})
                    existing = stock_by_href.get(href, {})
                    leaf['_goods'].append({
                        'name': product['name'],
                        'stock': 0,
                        'price': existing.get('price') if existing else self._catalog_sale_price(product),
                        'buy_price': existing.get('buy_price') if existing else self._price_in_rubles(product.get('buyPrice')),
                        'purchase_href': href,
                    })
                next_url = self._next_page(page, '/api/remap/1.2/entity/assortment', visited)
            return result
        except Exception as ex:
            print(f'Не удалось загрузить товары с нулевым остатком: {ex}')
            return None

    @staticmethod
    def _price_in_rubles(value):
        """Convert a MoySklad money field (kopecks) to rubles, if present."""
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get('value')
        try:
            return round(float(value) / 100, 2)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _catalog_sale_price(cls, assortment: dict):
        """Use the current default sale price of an expanded MoySklad product."""
        prices = assortment.get("salePrices") or []
        if not isinstance(prices, list):
            return None
        for entry in prices:
            if isinstance(entry, dict) and (entry.get("priceType") or {}).get("name") == "Цена продажи":
                return cls._price_in_rubles(entry.get("value"))
        if len(prices) == 1 and isinstance(prices[0], dict):
            return cls._price_in_rubles(prices[0].get("value"))
        return None

    def _latest_supply_price(self, product_href: str):
        """Return the price from the latest applicable receipt containing this product."""
        if not product_href:
            return None

        # Expanded assortment links can include a query such as
        # ?expand=productFolder. It is not part of the assortment identity and
        # must not be embedded in the filter value.
        parsed_href = urlsplit(product_href)
        product_href = urlunsplit((parsed_href.scheme, parsed_href.netloc, parsed_href.path, "", ""))

        url = "https://api.moysklad.ru/api/remap/1.2/entity/supply"
        params = {
            "filter": f"assortment={product_href};applicable=true",
            "order": "moment,desc",
            "limit": 100,
            "expand": "positions",
        }
        visited = {url}
        while url:
            for attempt in range(4):
                response = self._get(url, params=params)
                if response.status_code != 429 or attempt == 3:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(1, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(min(delay, 30))
            response.raise_for_status()
            page = self._json_page(response)
            for receipt in page.get("rows", []):
                positions = receipt.get("positions", {})
                if isinstance(positions, dict):
                    positions = positions.get("rows", [])
                for position in positions:
                    assortment_href = (position.get("assortment") or {}).get("meta", {}).get("href")
                    if assortment_href:
                        parsed_assortment_href = urlsplit(assortment_href)
                        assortment_href = urlunsplit((parsed_assortment_href.scheme, parsed_assortment_href.netloc, parsed_assortment_href.path, "", ""))
                    if assortment_href == product_href and position.get("price") is not None:
                        return self._price_in_rubles(position["price"])

            url = self._next_page(page, "/api/remap/1.2/entity/supply", visited)
            params = None  # nextHref already contains the query parameters
        return None

    def get_retail_stores(self) -> list:
        """Return retail point names and IDs for a read-only diagnostic."""
        url = "https://api.moysklad.ru/api/remap/1.2/entity/retailstore"
        response = self._get(url)
        response.raise_for_status()

        return [
            {"name": store.get("name"), "id": store.get("id")}
            for store in response.json().get("rows", [])
        ]

    def get_popular_goods(self, top_limit: int = 5) -> list:
        """Return up to ``top_limit`` best sellers for each purchase category."""
        url = "https://api.moysklad.ru/api/remap/1.2/entity/retaildemand"
        sales_filter = (
            f"retailStore={self.retail_store_href};"
            f"moment>={self.moment_from};"
            f"moment<={self.moment_to};"
            "applicable=true"
        )
        params = urlencode({
            "filter": sales_filter,
            "limit": 100,
            "expand": "positions.assortment.productFolder",
        })
        next_url = f"{url}?{params}"
        visited = {next_url}
        goods = {}
        try:
            folder_paths = self._get_product_folder_paths()
        except Exception as ex:
            print(f'Не удалось получить группы товаров для топа закупки: {ex}')
            folder_paths = {}

        def purchase_category_path(assortment, product_name):
            folder = assortment.get("productFolder") or {}
            folder_id = self._folder_id(folder)
            path = folder_paths.get(folder_id, ())
            if not path:
                path_name = folder.get("pathName") or ""
                path = tuple(part.strip() for part in path_name.split("/") if part.strip())
                if folder.get("name"):
                    path += (folder["name"].strip(),)
            normalized_name = product_name.casefold()
            normalized_path = " / ".join(path).casefold()

            # Some items are filed under a neighboring folder in MoySklad.
            # A clear product-name marker should still place them correctly.
            if "жидк" in normalized_name or "liquid" in normalized_name:
                return path, "liquids"

            category_key = None
            if "жидкост" in normalized_path:
                # Do this before checking for ЭСДН: the folder "Жидкости для
                # ЭСДН" contains that acronym but is not a disposable-device group.
                category_key = "liquids"
            elif "табак для кальяна" in normalized_path:
                category_key = "hookah_tobacco"
            elif "одноразов" in normalized_path:
                category_key = "disposable"
            elif "расходники для устройств" in normalized_path and any(
                marker in normalized_path
                for marker in ("испарител", "картридж", "катридж", "coil")
            ):
                # The MoySklad folder is currently misspelled "Катриджы".
                category_key = "cartridges"

            if category_key is None:
                normalized_category_name = " ".join(
                    (normalized_name, normalized_path)
                )
                if any(
                    brand in normalized_category_name
                    for brand in ("pafos", "waka", "lost mary", "funky lands")
                ):
                    category_key = "disposable"
            return path, category_key

        while next_url:
            response = self._get(next_url)
            response.raise_for_status()
            page = self._json_page(response)

            for sale in page.get("rows", []):
                positions = sale.get("positions", {})
                if isinstance(positions, dict):
                    positions = positions.get("rows", [])

                goods_in_this_check = set()

                for position in positions:
                    assortment = position.get("assortment") or {}
                    name = assortment.get("name")
                    quantity = float(position.get("quantity") or 0)

                    if not name or quantity <= 0:
                        continue

                    category_path, category_key = purchase_category_path(
                        assortment, name
                    )
                    if category_key is None:
                        continue

                    meta = assortment.get("meta") or {}
                    assortment_id = assortment.get("id")
                    if not assortment_id and meta.get("href"):
                        assortment_id = meta["href"].rstrip("/").split("/")[-1]
                    product_key = assortment_id or " ".join(name.casefold().split())

                    if product_key not in goods:
                        goods[product_key] = {
                            "name": name,
                            "price": self._catalog_sale_price(assortment),
                            "checks": 0,
                            "quantity": 0,
                            "category": " / ".join(category_path),
                            "category_key": category_key,
                            "buy_price": None,
                            "_product_href": meta.get("href"),
                        }
                    elif not goods[product_key].get("_product_href"):
                        goods[product_key]["_product_href"] = meta.get("href")

                    if goods[product_key].get("price") is None:
                        goods[product_key]["price"] = self._catalog_sale_price(assortment)

                    goods[product_key]["quantity"] += quantity
                    goods_in_this_check.add(product_key)

                for product_key in goods_in_this_check:
                    goods[product_key]["checks"] += 1

            next_url = self._next_page(page, "/api/remap/1.2/entity/retaildemand", visited)

        ranked_goods = sorted(
            goods.values(),
            key=lambda good: (good["checks"], good["quantity"]),
            reverse=True,
        )
        category_limits = {}
        selected = []
        for good in ranked_goods:
            category_key = good["category_key"]
            if category_limits.get(category_key, 0) >= top_limit:
                continue
            category_limits[category_key] = category_limits.get(category_key, 0) + 1
            selected.append(good)
        products_with_href = [good for good in selected if good.get("_product_href")]
        if products_with_href:
            def load_latest_supply_price(good):
                try:
                    return self._latest_supply_price(good["_product_href"])
                except Exception as ex:
                    print(f"Не удалось получить цену из последней приемки для {good['name']}: {ex}")
                    return None

            # Keep receipt lookups in this worker instead of creating nested
            # threads that would bypass the shared bounded MoySklad pool.
            for good in products_with_href:
                good["buy_price"] = load_latest_supply_price(good)
        for good in selected:
            good.pop("_product_href", None)
        return selected
    def get_sales(self) -> str:
        date_url_filter = urlencode({
            "momentFrom": self.moment_from,
            "momentTo": self.moment_to,
            "interval": "day",
            "filter": f"store={self.store_href}",
        })
        sales_url = f"{self.sales_url_without_dates}?{date_url_filter}"
        try:
            sales_response = self._get(sales_url)
            sales_response.raise_for_status()
            sales = sales_response.json()
            series = sales.get('series') if isinstance(sales, dict) else None
            if not isinstance(series, list):
                raise MoySkladAPIError("МойСклад вернул некорректный отчёт продаж.")
            sales_sum = sum(i.get('sum') for i in series) / 100
            sales_quantity = sum(i.get('quantity') for i in series)
            period_from = format_report_datetime(self.moment_from)
            period_to = format_report_datetime(self.moment_to)

            if sales_sum != 0 and sales_quantity != 0:
                return f"За период с {period_from} по {period_to}\nчеков: {sales_quantity} шт., " \
                       f"на сумму {sales_sum} руб."
            else:
                return f"За период с {period_from} по {period_to} продаж не было"

        except Exception:
            raise

    def get_turnover_outcome(self) -> dict:
        turnover_filter = urlencode({
            "momentFrom": self.moment_from,
            "momentTo": self.moment_to,
            "filter": f"store={self.store_href}",
        })
        turnover_url = f"{self.turnover_url_without_dates}?{turnover_filter}"
        try:
            response = self._get(turnover_url)
            response.raise_for_status()
            payload = response.json()
            turnover = payload.get('rows') if isinstance(payload, dict) else None
            if not isinstance(turnover, list):
                raise MoySkladAPIError("МойСклад вернул некорректный отчёт оборотов.")
            result = dict()
            for good in turnover:
                try:
                    if int(good['outcome']['quantity']) > 0:
                        category = good['assortment']['productFolder']['name'].split('/')[1]
                        if category not in result:
                            result[category] = [{"name": good['assortment']['name'], "outcome": int(good['outcome']['quantity'])}]
                        else:
                            result[category].append({"name": good['assortment']['name'], "outcome": int(good['outcome']['quantity'])})

                except Exception as ex:
                    print(f"Для {good['assortment']['name']} выгрузить не получилось! {ex}")

            return result
        except Exception:
            raise

    def get_sales_turnover_outcome_report(self) -> str:
        sales = self.get_sales()
        turnover_outcome = self.get_turnover_outcome()
        if not isinstance(turnover_outcome, dict):
            raise MoySkladAPIError("Не удалось сформировать отчёт продаж.")
        result = f"{sales}\n"

        for key, value in turnover_outcome.items():
            result += f"\n{key}:\n"
            for good in value:
                name = short_product_name(good["name"], category=key)
                result += f"  • {name} — {good['outcome']} шт.\n"
        return result


def give_dates_ago(day_ago=None, curent_period=False, months_ago=0) -> tuple[str, str]:
    date_format = "%Y-%m-%d"
    delta = timedelta(hours=3)
    time_now = datetime.now(timezone.utc) + delta

    if day_ago is None and curent_period is False and months_ago == 0:
        raise KeyError("Не выбран период времени для выгрузки")

    if day_ago is not None and day_ago >= 0:
        time_day = (time_now - timedelta(days=day_ago)).strftime(date_format)
        moment_from = f"{time_day} 00:00:00"

    if day_ago == 0 or curent_period:
        moment_to = time_now.strftime("%Y-%m-%d %H:%M:%S")
    elif months_ago:
        month_index = time_now.year * 12 + time_now.month - 1 - months_ago
        target_year, target_month_index = divmod(month_index, 12)
        target_month = target_month_index + 1
        days_in_month = monthrange(target_year, target_month)[1]
        month_start = time_now.replace(
            year=target_year, month=target_month, day=1
        )
        month_end = month_start.replace(day=days_in_month)
        moment_from = f"{month_start.strftime(date_format)} 00:00:00"
        moment_to = f"{month_end.strftime(date_format)} 22:00:00"

    elif day_ago > 0:
        moment_to = f"{time_day} 22:00:00"

    return moment_from, moment_to


if __name__ == '__main__':
    report = StockSalesReport(
        moment_from="2026-10-23 00:00:00",
        moment_to="2026-10-23 22:00:00",
    )
    print(report.get_popular_goods())
