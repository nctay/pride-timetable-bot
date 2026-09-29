import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pride_bot


class PrideBotTest(unittest.TestCase):
    def test_account_mapping_and_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("anna-token")
            os.environ["MOBI_TOKEN_ANNN_UU_FILE"] = str(token_file)
            self.assertEqual(pride_bot.account_token("@annn_uu"), "anna-token")
        today = date(2026, 9, 29)
        self.assertEqual(pride_bot.range_dates("today", today), (today, 1))
        self.assertEqual(pride_bot.range_dates("tomorrow", today), (date(2026, 9, 30), 1))
        self.assertEqual(pride_bot.range_dates("week", today), (today, 7))
        starts_at = "2026-09-30T20:00:00+03:00"
        self.assertFalse(pride_bot.watch_deadline_reached(starts_at, datetime.fromisoformat("2026-09-30T19:44:59+03:00")))
        self.assertTrue(pride_bot.watch_deadline_reached(starts_at, datetime.fromisoformat("2026-09-30T19:45:00+03:00")))
        item = {"beginDate": "2026-09-29T09:00:00+03:00"}
        self.assertFalse(pride_bot.registration_opened(item, datetime.fromisoformat("2026-09-29T08:59:59+03:00")))
        self.assertTrue(pride_bot.registration_opened(item, datetime.fromisoformat("2026-09-29T09:00:00+03:00")))


if __name__ == "__main__":
    unittest.main()
