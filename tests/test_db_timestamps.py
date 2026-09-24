# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import db


class DbTimestampTests(unittest.TestCase):
    def test_now_is_utc_offset_aware(self):
        value = db._now()
        parsed = datetime.fromisoformat(value)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))

    def test_age_accepts_legacy_naive_and_aware_timestamps(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch.object(db, "datetime") as datetime_type:
            datetime_type.now.return_value = now
            datetime_type.fromisoformat.side_effect = datetime.fromisoformat
            self.assertAlmostEqual(
                db._timestamp_age_seconds("2025-12-31T23:59:00"),
                60,
            )
            self.assertAlmostEqual(
                db._timestamp_age_seconds("2025-12-31T23:59:00+01:00"),
                3660,
            )

    def test_parse_iso_dt_returns_utc_for_naive_date_filters(self):
        parsed = db._parse_iso_dt("2026-01-01T00:00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
