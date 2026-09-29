import os
import tempfile
import unittest
from datetime import date
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


if __name__ == "__main__":
    unittest.main()
