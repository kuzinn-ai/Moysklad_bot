from calendar import monthrange
from urllib.parse import urlencode
from datetime import timedelta, datetime, timezone
import time

import requests

try:
    from .settings import token_ms
except ImportError:
    from settings import token_ms


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
        self.stock_url_without_filter = 'https://api.moysklad.ru/api/remap/1.2/report/stock/all'  # расширенный отчет об остатках
        self.product_folders_url = 'https://api.moysklad.ru/api/remap/1.2/entity/productfolder'
        self.filter = filter_url

        self.sales_url_without_dates = 'https://api.moysklad.ru/api/remap/1.2/report/sales/plotseries'  # показатели продаж
        self.turnover_url_without_dates = 'https://api.moysklad.ru/api/remap/1.2/report/turnover/all'  # обороты
        self.moment_from = moment_from  # формат 2023-04-24 00:00:00
        self.moment_to = moment_to

    def _get_product_folder_paths(self) -> dict:
        """Return folder ID -> full folder path using MoySklad's catalog tree."""
        if (
            StockSalesReport._folder_paths_cache is not None
            and time.time() - StockSalesReport._folder_paths_cache_time < 60
        ):
            return StockSalesReport._folder_paths_cache

        folders_by_id = {}
        next_url = self.product_folders_url

        while next_url:
            response = requests.get(url=next_url, headers=self.headers)
            response.raise_for_status()
            page = response.json()
            for folder in page.get('rows', []):
                meta = folder.get('meta', {})
                folder_id = folder.get('id')
                if not folder_id and meta.get('href'):
                    folder_id = meta['href'].rstrip('/').split('/')[-1]
                if folder_id:
                    folders_by_id[folder_id] = folder
            next_url = page.get('meta', {}).get('nextHref')

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
            stock_response = requests.get(url=stock_url, headers=self.headers)
            stock_response.raise_for_status()
            goods = stock_response.json()['rows']
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
                    })
                except Exception as ex:
                    print(f"Для {good['name']} выгрузить не получилось! {ex}")

            return result

        except Exception as ex:
            print(f'Проблема с подключением к API МойСклад: {ex}')

    def get_sales(self) -> str:
        date_url_filter = urlencode({
            "momentFrom": self.moment_from,
            "momentTo": self.moment_to,
            "interval": "day",
            "filter": f"store={self.store_href}",
        })
        sales_url = f"{self.sales_url_without_dates}?{date_url_filter}"
        try:
            sales_response = requests.get(url=sales_url, headers=self.headers)
            sales = sales_response.json()
            sales_sum = sum(i.get('sum') for i in sales.get('series')) / 100
            sales_quantity = sum(i.get('quantity') for i in sales.get('series'))
            period_from = datetime.strptime(
                self.moment_from, "%Y-%m-%d %H:%M:%S"
            ).strftime("%d.%m.%Y %H:%M:%S")
            period_to = datetime.strptime(
                self.moment_to, "%Y-%m-%d %H:%M:%S"
            ).strftime("%d.%m.%Y %H:%M:%S")

            if sales_sum != 0 and sales_quantity != 0:
                return f"За период с {period_from} по {period_to}\nчеков: {sales_quantity} шт., " \
                       f"на сумму {sales_sum} руб."
            else:
                return f"За период с {period_from} по {period_to} продаж не было"

        except Exception as ex:
            print(f'Проблема с подключением к API МойСклад: {ex}')

    def get_turnover_outcome(self) -> dict:
        turnover_filter = urlencode({
            "momentFrom": self.moment_from,
            "momentTo": self.moment_to,
            "filter": f"store={self.store_href}",
        })
        turnover_url = f"{self.turnover_url_without_dates}?{turnover_filter}"
        try:
            response = requests.get(url=turnover_url, headers=self.headers)
            turnover = response.json()['rows']
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
        except Exception as ex:
            print(f'Проблема с подключением к API МойСклад: {ex}')

    def get_sales_turnover_outcome_report(self) -> str:
        sales = self.get_sales()
        turnover_outcome = self.get_turnover_outcome()
        result = f"{sales}\n"

        for key, value in turnover_outcome.items():
            result += f"\n{key}:\n"
            for good in value:
                result += f"{good['name']} - {good['outcome']} шт.\n"
        return result


def give_dates_ago(day_ago=None, curent_period=False, months_ago=0) -> tuple[str, str]:
    date_format = "%Y-%m-%d"
    delta = timedelta(hours=3)
    time_now = datetime.now(timezone.utc) + delta

    if day_ago is None and curent_period is False and months_ago == 0:
        raise KeyError("Не выбран период времени для выгрузки")

    if day_ago and day_ago >= 0:
        time_day = (time_now - timedelta(days=day_ago)).strftime(date_format)
        moment_from = f"{time_day} 00:00:00"

    if day_ago == 0 or curent_period:
        moment_to = time_now.strftime("%Y-%m-%d %H:%M:%S")
    elif months_ago:
        cur_y = int(time_now.strftime('%Y'))
        cur_m = int(time_now.strftime('%m'))
        if cur_m - months_ago <= 0:
            new_cur_m = 12 + cur_m - months_ago
            month_before = time_now.replace(year=cur_y - 1, month=12)
            days_in_month = monthrange(cur_y - 1, new_cur_m)[1]
            moment_from = f"{month_before.replace(day=1).strftime(date_format)} 00:00:00"
            moment_to = f"{month_before.replace(day=days_in_month).strftime(date_format)} 22:00:00"
        else:
            month_before = time_now.replace(month=cur_m - 1)
            days_in_month = monthrange(cur_y, cur_m)[1]
            moment_from = f"{month_before.replace(day=1).strftime(date_format)} 00:00:00"
            moment_to = f"{month_before.replace(day=days_in_month).strftime(date_format)} 22:00:00"

    elif day_ago > 0:
        moment_to = f"{time_day} 22:00:00"

    return moment_from, moment_to


if __name__ == '__main__':
    report = StockSalesReport(moment_from='2023-04-25 00:00:01', moment_to='2023-04-25 20:25:45')
    print(report.get_sales_turnover_outcome_report())
