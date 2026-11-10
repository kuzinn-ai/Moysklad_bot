"""The isolated test bot must work without a MoySklad credential."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE_MAIN = Path(__file__).resolve().parents[1] / "main"


def _isolated_package(tmp_path):
    package = tmp_path / "main"
    package.mkdir()
    for name in ("settings.py", "main.py", "offline_test_data.py", "abc_xyz.py"):
        shutil.copy2(SOURCE_MAIN / name, package / name)
    env = os.environ.copy()
    env.pop("MOYSKLAD_TOKEN", None)
    env.pop("PYTHONPATH", None)
    env.update(PARADISE_OFFLINE_TEST_MODE="1", TELEGRAM_BOT_TOKEN="123456:TESTTOKEN")
    return env


def test_offline_mode_uses_only_synthetic_data_and_blocks_network(tmp_path):
    env = _isolated_package(tmp_path)
    script = """
from datetime import date
from unittest.mock import patch
from main.main import StockSalesReport
from main import offline_test_data

with patch('requests.get', side_effect=AssertionError('network request')):
    report = StockSalesReport()
    assert report.get_goods_dict()['Тестовые товары']['_goods'][0]['name'].startswith('ТЕСТ')
    report.filter = 'stockMode=negativeOnly'
    assert report.get_goods_dict()['Тестовые товары']['_goods'][0]['stock'] == -2
    assert report.get_zero_stock_goods_dict()['Тестовые товары']['_goods'][0]['stock'] == 0
    assert report.get_popular_goods()[0]['name'].startswith('ТЕСТ')
    assert 'ТЕСТОВЫЕ' in report.get_sales_turnover_outcome_report()
    assert len(offline_test_data.analysis(date(2026, 8, 1), date(2026, 11, 1))['items']) == 3
    try:
        report._get('https://api.moysklad.ru/api/remap/1.2/entity/product')
    except RuntimeError:
        pass
    else:
        raise AssertionError('MoySklad request was not blocked')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_offline_mode_rejects_moysklad_token(tmp_path):
    env = _isolated_package(tmp_path)
    env["MOYSKLAD_TOKEN"] = "fake-test-token"
    result = subprocess.run(
        [sys.executable, "-c", "import main.settings"], cwd=tmp_path, env=env,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "удалите токен МоегоСклада" in result.stderr
