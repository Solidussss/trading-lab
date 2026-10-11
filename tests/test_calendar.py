import unittest
from datetime import UTC, date, datetime, timedelta

from tradinglab.marketdata.calendar import (
    cross_check,
    is_early_close,
    is_trading_day,
    last_completed_session,
    next_trading_day,
    ny_date,
    nyse_holidays,
    previous_trading_day,
    session,
    trading_days,
)

# Published NYSE holiday schedules.
EXPECTED_HOLIDAYS = {
    2024: ["01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04", "09-02", "11-28", "12-25"],
    2025: ["01-01", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"],
    2026: ["01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"],
    2027: ["01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"],
}
EXPECTED_EARLY_CLOSES = {
    2024: ["07-03", "11-29", "12-24"],
    2025: ["07-03", "11-28", "12-24"],
    2026: ["11-27", "12-24"],
    2027: ["11-26"],
}


def d(year, mmdd):
    m, dd = mmdd.split("-")
    return date(year, int(m), int(dd))


class HolidayTests(unittest.TestCase):
    def test_published_holidays(self):
        for year, days in EXPECTED_HOLIDAYS.items():
            with self.subTest(year=year):
                self.assertEqual(sorted(nyse_holidays(year)), sorted(d(year, x) for x in days))

    def test_early_closes(self):
        for year, days in EXPECTED_EARLY_CLOSES.items():
            got = [x for x in trading_days(date(year, 1, 1), date(year, 12, 31)) if is_early_close(x)]
            with self.subTest(year=year):
                self.assertEqual(got, [d(year, x) for x in days])

    def test_saturday_new_year_not_observed_on_friday(self):
        self.assertTrue(is_trading_day(date(2021, 12, 31)))  # Jan 1 2022 was a Saturday

    def test_special_closures(self):
        self.assertFalse(is_trading_day(date(2025, 1, 9)))  # Carter national day of mourning
        self.assertFalse(is_trading_day(date(2018, 12, 5)))
        self.assertFalse(is_trading_day(date(2012, 10, 29)))

    def test_weekends(self):
        self.assertFalse(is_trading_day(date(2026, 10, 10)))  # Saturday
        self.assertTrue(is_trading_day(date(2026, 10, 9)))

    def test_trading_day_counts(self):
        self.assertEqual(len(trading_days(date(2024, 1, 1), date(2024, 12, 31))), 252)
        self.assertEqual(len(trading_days(date(2025, 1, 1), date(2025, 12, 31))), 250)

    def test_next_previous(self):
        self.assertEqual(next_trading_day(date(2026, 4, 2)), date(2026, 4, 6))  # Good Friday
        self.assertEqual(previous_trading_day(date(2026, 1, 20)), date(2026, 1, 16))  # MLK


class SessionTimeTests(unittest.TestCase):
    def test_dst_transitions(self):
        # DST began 2026-03-08: open is 14:30 UTC before, 13:30 UTC after.
        self.assertEqual(session(date(2026, 3, 6)).open_utc.hour, 14)
        self.assertEqual(session(date(2026, 3, 9)).open_utc.hour, 13)
        # DST ended 2026-11-01.
        self.assertEqual(session(date(2026, 10, 30)).close_utc.hour, 20)
        self.assertEqual(session(date(2026, 11, 2)).close_utc.hour, 21)

    def test_early_close_time(self):
        s = session(date(2026, 11, 27))
        self.assertTrue(s.early_close)
        self.assertEqual((s.close_ny.hour, s.close_ny.minute), (13, 0))

    def test_session_on_holiday_raises(self):
        with self.assertRaises(ValueError):
            session(date(2026, 12, 25))

    def test_ny_date(self):
        # A timestamp is mapped to its New York session date.
        self.assertEqual(ny_date(datetime(2026, 10, 9, 4, 0, tzinfo=UTC)), date(2026, 10, 9))
        self.assertEqual(ny_date(datetime(2026, 1, 9, 5, 0, tzinfo=UTC)), date(2026, 1, 9))
        with self.assertRaises(ValueError):
            ny_date(datetime(2026, 1, 9))


class CompletedSessionTests(unittest.TestCase):
    def test_before_and_after_settle(self):
        close = session(date(2026, 10, 9)).close_utc  # Friday
        self.assertEqual(last_completed_session(close + timedelta(minutes=5)), date(2026, 10, 8))
        self.assertEqual(last_completed_session(close + timedelta(minutes=21)), date(2026, 10, 9))

    def test_weekend_and_early_close(self):
        sat = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
        self.assertEqual(last_completed_session(sat), date(2026, 10, 9))
        early = session(date(2026, 11, 27)).close_utc + timedelta(minutes=30)
        self.assertEqual(last_completed_session(early), date(2026, 11, 27))


class CrossCheckTests(unittest.TestCase):
    def test_agreement_and_mismatch(self):
        days = trading_days(date(2026, 12, 1), date(2026, 12, 31))
        self.assertTrue(cross_check(date(2026, 12, 1), date(2026, 12, 31), days).ok)
        broker = [x for x in days if x != date(2026, 12, 24)]
        mm = cross_check(date(2026, 12, 1), date(2026, 12, 31), broker)
        self.assertFalse(mm.ok)
        self.assertEqual(mm.only_in_rules, (date(2026, 12, 24),))


if __name__ == "__main__":
    unittest.main()
