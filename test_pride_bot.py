import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

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
        dates = pride_bot.selectable_dates(today)
        self.assertEqual((len(dates), dates[0], dates[-1]), (14, today, date(2026, 10, 12)))
        starts_at = "2026-09-30T20:00:00+03:00"
        self.assertFalse(pride_bot.watch_deadline_reached(starts_at, datetime.fromisoformat("2026-09-30T19:44:59+03:00")))
        self.assertTrue(pride_bot.watch_deadline_reached(starts_at, datetime.fromisoformat("2026-09-30T19:45:00+03:00")))
        item = {"beginDate": "2026-09-29T09:00:00+03:00"}
        self.assertFalse(pride_bot.registration_opened(item, datetime.fromisoformat("2026-09-29T08:59:59+03:00")))
        self.assertTrue(pride_bot.registration_opened(item, datetime.fromisoformat("2026-09-29T09:00:00+03:00")))
        excluded = (
            "Физкультура (3-4 года)",
            "Функциональная тренировка (10–15 лет)",
            "Шаг вперед 4-6 лет",
            "Коррекционная гимнастика (от 3 лет)",
            "Подготовка от 3 до 6 лет",
            "Шахматный клуб",
            "ШАХМАТЫ",
        )
        self.assertTrue(all(not pride_bot.class_allowed(title) for title in excluded))
        self.assertTrue(pride_bot.class_allowed("ЙОГА (90 мин)"))

    def test_opening_notification_does_not_wait_for_schedule_scan(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pride_bot, "DB_PATH", Path(directory) / "test.db"):
            bot = object.__new__(pride_bot.PrideBot)
            bot.database = pride_bot.db()
            bot.telegram = Mock()
            bot.next_opening_check = int(time.time()) + 1800
            now = datetime.now(pride_bot.MOSCOW)
            bot.database.execute(
                "INSERT INTO opening_watches(chat_id,username,activity_id,title) VALUES(?,?,?,?)",
                (123, "annn_uu", "activity", "ЙОГА"),
            )
            bot.database.execute(
                """INSERT INTO opening_events(username,title,event_id,starts_at,opens_at)
                   VALUES(?,?,?,?,?)""",
                (
                    "annn_uu", "ЙОГА", "event",
                    (now + timedelta(days=1)).isoformat(),
                    (now + timedelta(minutes=1)).isoformat(),
                ),
            )
            bot.database.execute(
                """INSERT INTO opening_events(username,title,event_id,starts_at,opens_at)
                   VALUES(?,?,?,?,?)""",
                (
                    "annn_uu", "ЙОГА", "later",
                    (now + timedelta(days=1)).isoformat(),
                    (now + timedelta(minutes=3)).isoformat(),
                ),
            )
            bot.database.commit()
            with patch.object(pride_bot.MobiFitness, "schedule") as schedule:
                bot.check_openings()
                bot.check_openings()
                schedule.assert_not_called()
            bot.telegram.send.assert_called_once()
            self.assertIn("Запись откроется в", bot.telegram.send.call_args.args[1])
            self.assertEqual(
                dict(bot.database.execute("SELECT event_id,notified FROM opening_events")),
                {"event": 1, "later": 0},
            )
            bot.database.execute(
                "UPDATE opening_events SET opens_at=? WHERE event_id='later'",
                ((now - timedelta(minutes=1)).isoformat(),),
            )
            bot.database.commit()
            bot.check_openings()
            self.assertEqual(bot.telegram.send.call_count, 2)
            self.assertIn("Открылась запись", bot.telegram.send.call_args.args[1])
            bot.database.close()

    def test_booking_waits_for_selected_class_opening(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pride_bot, "DB_PATH", Path(directory) / "test.db"):
            bot = object.__new__(pride_bot.PrideBot)
            bot.database = pride_bot.db()
            bot.telegram = Mock()
            now = datetime.now(pride_bot.MOSCOW)
            opening = now + timedelta(minutes=10)
            item = {
                "id": "chosen-event",
                "datetime": (now + timedelta(days=1)).isoformat(),
                "beginDate": opening.isoformat(),
                "activity": {"title": "ЙОГА"},
                "preEntry": True,
                "bookingOpened": True,
                "availableSlots": "5",
                "reserved": False,
            }
            api = Mock()
            api.item.return_value = item
            api.schedule.return_value = [item]
            api.reserve.return_value = {**item, "reserved": True}
            with patch.object(pride_bot, "account_token", return_value="token"), patch.object(
                pride_bot, "MobiFitness", return_value=api
            ):
                bot.send_dates(123, book_at_open=True)
                self.assertTrue(bot.telegram.send.call_args.args[2][0][0]["callback_data"].startswith("bookdate|"))
                day = pride_bot.local_datetime(item["datetime"]).date()
                bot.send_schedule(123, "annn_uu", day.isoformat(), 0, book_at_open=True)
                api.schedule.assert_called_once_with(day, 1)
                self.assertEqual(bot.telegram.send.call_args.args[2][0][0]["callback_data"], "bookopen|chosen-event")
                bot.select_event(123, "annn_uu", "chosen-event", book_at_open=True)
                api.reserve.assert_not_called()
                row = bot.database.execute("SELECT status,next_check FROM watches").fetchone()
                self.assertEqual((row["status"], row["next_check"]), ("waiting_open", int(opening.timestamp())))
                bot.watches(123, "annn_uu")
                self.assertEqual(bot.telegram.send.call_args.args[2][0][0]["callback_data"], "stop|chosen-event")
                bot.check_watches()
                api.reserve.assert_not_called()
                item["beginDate"] = (now - timedelta(minutes=1)).isoformat()
                bot.database.execute("UPDATE watches SET next_check=0")
                bot.database.commit()
                bot.check_watches()
                api.reserve.assert_called_once_with("chosen-event")
                self.assertEqual(bot.database.execute("SELECT status FROM watches").fetchone()[0], "booked")
                api.reserve.reset_mock()
                item["availableSlots"] = "0"
                item["beginDate"] = (now - timedelta(seconds=10)).isoformat()
                bot.database.execute("UPDATE watches SET status='waiting_open',next_check=0")
                bot.database.commit()
                bot.check_watches()
                api.reserve.assert_not_called()
                row = bot.database.execute("SELECT status,next_check FROM watches").fetchone()
                self.assertEqual(row["status"], "active")
                self.assertLessEqual(row["next_check"] - int(time.time()), 10)
            bot.database.close()


if __name__ == "__main__":
    unittest.main()
