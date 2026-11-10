import pytest
from freezegun import freeze_time

from main.main import format_report_datetime, give_dates_ago


@freeze_time("2023-08-15 12:00:00", tz_offset=0)
def test_give_dates_ago():
    assert give_dates_ago(day_ago=1) == ("2023-08-14 00:00:00", "2023-08-14 22:00:00")
    assert give_dates_ago(day_ago=2) == ("2023-08-13 00:00:00", "2023-08-13 22:00:00")


@freeze_time("2023-01-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago_january():
    assert give_dates_ago(months_ago=1) == ("2022-12-01 00:00:00", "2022-12-31 22:00:00")


@freeze_time("2023-05-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago():
    assert give_dates_ago(months_ago=1) == ("2023-04-01 00:00:00", "2023-04-30 22:00:00")


@freeze_time("2023-03-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago_february_non_leap_year():
    assert give_dates_ago(months_ago=1) == ("2023-02-01 00:00:00", "2023-02-28 22:00:00")


@freeze_time("2024-03-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago_february_leap_year():
    assert give_dates_ago(months_ago=1) == ("2024-02-01 00:00:00", "2024-02-29 22:00:00")


@freeze_time("2023-01-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago_multiple_months_across_year():
    assert give_dates_ago(months_ago=2) == ("2022-11-01 00:00:00", "2022-11-30 22:00:00")


@freeze_time("2024-01-15 12:00:00", tz_offset=0)
def test_give_dates_months_ago_more_than_one_year():
    assert give_dates_ago(months_ago=14) == ("2022-11-01 00:00:00", "2022-11-30 22:00:00")


@freeze_time("2024-02-29 22:30:00", tz_offset=0)
def test_give_dates_months_ago_preserves_three_hour_shift_at_month_boundary():
    assert give_dates_ago(months_ago=1) == ("2024-02-01 00:00:00", "2024-02-29 22:00:00")


def test_format_report_datetime_uses_day_month_year_and_preserves_time():
    assert format_report_datetime("2026-11-03 09:07:05") == "03-11-2026 09:07:05"


@freeze_time("2023-08-15 12:00:00", tz_offset=0)
def test_give_dates_ago_no_conditions():
    with pytest.raises(KeyError):
        give_dates_ago()


if __name__ == '__main__':
    pytest.main()
