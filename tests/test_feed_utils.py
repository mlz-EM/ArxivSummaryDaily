"""Tests for shared feed formatting utilities."""

import unittest
from datetime import datetime, timezone
from unittest import mock

from src.feed_utils import EASTERN_TIME, eastern_generated_at


class TestEasternGeneratedAt(unittest.TestCase):
    def test_uses_daylight_time_offset_in_summer(self):
        summer = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        with mock.patch("src.feed_utils.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = summer.astimezone(EASTERN_TIME)
            generated_at = eastern_generated_at()

        self.assertEqual(generated_at, "2026-08-28T08:00:00-04:00")
        mocked_datetime.now.assert_called_once_with(EASTERN_TIME)

    def test_uses_standard_time_offset_in_winter(self):
        winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
        with mock.patch("src.feed_utils.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = winter.astimezone(EASTERN_TIME)
            generated_at = eastern_generated_at()

        self.assertEqual(generated_at, "2026-12-01T07:00:00-05:00")
        mocked_datetime.now.assert_called_once_with(EASTERN_TIME)


if __name__ == "__main__":
    unittest.main()
