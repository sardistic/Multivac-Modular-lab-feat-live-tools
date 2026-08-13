import unittest
from datetime import datetime, timedelta, timezone

from services.usage_costs import _day_start_utc, _month_start_utc, _week_start_utc, _year_start_utc

EDT = timezone(timedelta(hours=-4))  # US Eastern in July, without needing tzdata


class ReportBoundaryTests(unittest.TestCase):
    """Report windows must roll over on the LOCAL calendar day, not UTC, so an
    evening report doesn't read 0 just because UTC already ticked to tomorrow.
    Regression for the '0 calls today' bug seen at 10:54pm ET."""

    def test_evening_et_day_includes_that_local_day(self):
        # 02:55 UTC Jul 6 == 10:55pm EDT Jul 5. The local day started Jul 5.
        now_utc = datetime(2026, 7, 6, 2, 55, tzinfo=timezone.utc)
        start = _day_start_utc(now_utc, EDT)
        # Local midnight Jul 5 EDT == 04:00 UTC Jul 5.
        self.assertEqual(start, datetime(2026, 7, 5, 4, 0, tzinfo=timezone.utc))
        # A row recorded at 23:56 UTC Jul 5 (the real data) now falls inside.
        row_ts = datetime(2026, 7, 5, 23, 56, tzinfo=timezone.utc)
        self.assertGreaterEqual(row_ts.isoformat(), start.isoformat())
        # ...whereas the old UTC boundary (Jul 6 00:00 UTC) wrongly excluded it.
        utc_boundary = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertLess(row_ts.isoformat(), utc_boundary.isoformat())

    def test_month_boundary_uses_local_month(self):
        # 02:00 UTC Jul 1 == 10:00pm EDT Jun 30, still the June local month.
        now_utc = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
        start = _month_start_utc(now_utc, EDT)
        self.assertEqual(start, datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc))

    def test_week_boundary_is_local_monday(self):
        # 02:00 UTC Monday is still Sunday evening in EDT, so the current local
        # week began on the prior Monday rather than at UTC midnight.
        now_utc = datetime(2026, 7, 6, 2, 0, tzinfo=timezone.utc)
        start = _week_start_utc(now_utc, EDT)
        self.assertEqual(start, datetime(2026, 6, 29, 4, 0, tzinfo=timezone.utc))

    def test_year_boundary_uses_local_year(self):
        # 02:00 UTC Jan 1 2027 == 9:00pm EST Dec 31 2026, still the 2026 year.
        now_utc = datetime(2027, 1, 1, 2, 0, tzinfo=timezone.utc)
        start = _year_start_utc(now_utc, EDT)
        self.assertEqual(start, datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc))

    def test_utc_tz_matches_plain_utc_midnight(self):
        now_utc = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _day_start_utc(now_utc, timezone.utc),
            datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
