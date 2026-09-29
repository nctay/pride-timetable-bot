#!/usr/bin/env python3
"""Pride Fitness CLI and Telegram vacancy watcher. Python 3.11+, no packages."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
API_URL = "https://mobifitness.ru/api/v8"
MOSCOW = ZoneInfo("Europe/Moscow")
AGE_IN_TITLE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:(?:[-–—]|до)\s*\d+(?:[.,]\d+)?|\+)?\s*(?:лет|год(?:а|ов)?)",
    re.IGNORECASE,
)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv()
CLUB_ID = int(os.getenv("PRIDE_CLUB_ID", "6214"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
OPENING_CHECK_INTERVAL = int(os.getenv("OPENING_CHECK_INTERVAL", "1800"))
DB_PATH = Path(os.getenv("PRIDE_DB", ROOT / "pride.db"))


def secret(name: str) -> str:
    if value := os.getenv(name):
        return value.strip()
    if filename := os.getenv(f"{name}_FILE"):
        path = Path(filename)
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            return path.read_text().strip()
    return ""


def username_key(username: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", username.lstrip("@").upper()).strip("_")


def account_token(username: str) -> str:
    name = f"MOBI_TOKEN_{username_key(username)}"
    token = secret(name)
    if not token:
        raise ValueError(f"Для @{username.lstrip('@')} не настроен {name}")
    return token


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class MobiFitness:
    def __init__(self, token: str, club_id: int = CLUB_ID):
        self.token = token
        self.club_id = club_id

    def request(self, method: str, path: str, data: dict | None = None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(
            API_URL + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept-Language": "ru",
                "X-Angular-Widget": "true",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                message = json.loads(raw).get("message", raw)
            except json.JSONDecodeError:
                message = raw
            raise ApiError(error.code, str(message)) from error

    def schedule(self, start: date, days: int) -> list[dict]:
        end = start + timedelta(days=days)
        weeks = {(d.isocalendar().year, d.isocalendar().week) for d in (start, end - timedelta(days=1))}
        items: dict[str, dict] = {}
        for year, week in sorted(weeks):
            data = self.request("GET", f"/club/{self.club_id}/schedule.json?year={year}&week={week}")
            for item in data.get("schedule", []):
                day = datetime.fromisoformat(item["datetime"]).date()
                if start <= day < end:
                    items[item["id"]] = item
        return sorted(items.values(), key=lambda item: (item["datetime"], item["activity"]["title"]))

    def item(self, event_id: str) -> dict:
        return self.request("GET", f"/schedule/{event_id}/item.json?clubId={self.club_id}")

    def reserve(self, event_id: str) -> dict:
        self.request("POST", "/account/reserve.json", {"scheduleId": event_id, "clubId": self.club_id})
        return self.item(event_id)

    def cancel(self, event_id: str) -> dict:
        self.request("DELETE", "/account/reserve.json", {"scheduleId": event_id, "clubId": self.club_id})
        return self.item(event_id)


def slots(item: dict) -> int | None:
    value = item.get("availableSlots")
    return None if value is None else int(value)


def item_name(item: dict) -> str:
    return item.get("activity", {}).get("title", item.get("id", "Занятие"))


def class_allowed(title: str) -> bool:
    return "шахмат" not in title.casefold() and not AGE_IN_TITLE.search(title)


def local_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return parsed.astimezone(MOSCOW)


def item_time(item: dict) -> str:
    return local_datetime(item["datetime"]).strftime("%d.%m %H:%M")


def range_dates(mode: str, today: date | None = None) -> tuple[date, int]:
    today = today or datetime.now(MOSCOW).date()
    if mode == "today":
        return today, 1
    if mode == "tomorrow":
        return today + timedelta(days=1), 1
    if mode == "week":
        return today, 7
    raise ValueError(mode)


def selectable_dates(today: date | None = None) -> list[date]:
    today = today or datetime.now(MOSCOW).date()
    return [today + timedelta(days=offset) for offset in range(14)]


def watch_deadline_reached(starts_at: str, now: datetime | None = None) -> bool:
    return (now or datetime.now(MOSCOW)) >= local_datetime(starts_at) - timedelta(minutes=15)


def registration_opened(item: dict, now: datetime | None = None) -> bool:
    return bool(item.get("beginDate")) and (now or datetime.now(MOSCOW)) >= local_datetime(item["beginDate"])


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            event_id TEXT NOT NULL,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            next_check INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (username, event_id)
        );
        CREATE TABLE IF NOT EXISTS filters (
            chat_id INTEGER PRIMARY KEY,
            query TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS opening_watches (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            activity_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            PRIMARY KEY (username, title)
        );
        CREATE TABLE IF NOT EXISTS opening_events (
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            event_id TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            opens_at TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (username, event_id)
        );
        """
    )
    return connection


class Telegram:
    def __init__(self, token: str):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        encoded = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            for key, value in params.items()
            if value is not None
        }
        request = urllib.request.Request(self.url + method, urllib.parse.urlencode(encoded).encode())
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram API error"))
        return result["result"]

    def send(self, chat_id: int, text: str, keyboard: list | None = None):
        markup = {"inline_keyboard": keyboard} if keyboard else None
        return self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)

    def send_main_menu(self, chat_id: int, text: str):
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup={
                "keyboard": [
                    [{"text": "Расписание"}],
                    [{"text": "Следить за открытием записи"}],
                    [{"text": "Мои подписки"}],
                ],
                "resize_keyboard": True,
                "is_persistent": True,
            },
        )


RANGE_KEYBOARD = [
    [{"text": "Сегодня", "callback_data": "range|today|0"}],
    [{"text": "Завтра", "callback_data": "range|tomorrow|0"}],
    [{"text": "Выбрать дату", "callback_data": "dates|0"}],
    [{"text": "Следить за открытием записи", "callback_data": "openings|0"}],
    [{"text": "Мои подписки", "callback_data": "subscriptions|0"}],
]


class PrideBot:
    PAGE_SIZE = 10

    def __init__(self):
        token = secret("TELEGRAM_BOT_TOKEN")
        if not token:
            raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
        self.telegram = Telegram(token)
        self.database = db()
        self.next_opening_check = 0

    def query(self, chat_id: int) -> str:
        row = self.database.execute("SELECT query FROM filters WHERE chat_id=?", (chat_id,)).fetchone()
        return row["query"] if row else ""

    def set_query(self, chat_id: int, query: str) -> None:
        self.database.execute(
            "INSERT INTO filters(chat_id,query) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET query=excluded.query",
            (chat_id, query),
        )
        self.database.commit()

    def send_ranges(self, chat_id: int) -> None:
        query = self.query(chat_id)
        suffix = f" по запросу «{query}»" if query else ""
        self.telegram.send(chat_id, f"Выберите период для занятий{suffix}:", RANGE_KEYBOARD)

    def user(self, payload: dict) -> tuple[int, str]:
        sender = payload.get("from", {})
        username = (sender.get("username") or "").lower()
        chat_id = payload.get("message", {}).get("chat", {}).get("id") or payload.get("chat", {}).get("id")
        if not username:
            raise ValueError("У аккаунта Telegram должен быть username")
        account_token(username)
        return int(chat_id), username

    def send_schedule(self, chat_id: int, username: str, mode: str, page: int) -> None:
        if mode in {"today", "tomorrow", "week"}:
            start, days = range_dates(mode)
            callback_action = "range"
        else:
            start, days = date.fromisoformat(mode), 1
            callback_action = "date"
        items = MobiFitness(account_token(username)).schedule(start, days)
        items = [item for item in items if class_allowed(item_name(item))]
        query = self.query(chat_id).casefold()
        if query:
            items = [item for item in items if query in item_name(item).casefold()]
        pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        shown = items[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]
        keyboard = []
        for item in shown:
            free = slots(item)
            capacity = item.get("totalSlots")
            place = "∞" if free is None else f"{free}/{capacity or '?'}"
            label = f"{item_time(item)} · {item_name(item)} · {place}"[:64]
            keyboard.append([{"text": label, "callback_data": f"watch|{item['id']}"}])
        navigation = []
        if page:
            navigation.append({"text": "←", "callback_data": f"{callback_action}|{mode}|{page - 1}"})
        if page + 1 < pages:
            navigation.append({"text": "→", "callback_data": f"{callback_action}|{mode}|{page + 1}"})
        if navigation:
            keyboard.append(navigation)
        text = f"Найдено занятий: {len(items)}. Страница {page + 1}/{pages}. Выберите занятие:"
        self.telegram.send(chat_id, text if items else "Подходящих занятий нет.", keyboard or None)

    def send_dates(self, chat_id: int) -> None:
        weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        buttons = [
            {"text": f"{weekdays[day.weekday()]} {day:%d.%m}", "callback_data": f"date|{day.isoformat()}|0"}
            for day in selectable_dates()
        ]
        keyboard = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        self.telegram.send(chat_id, "Выберите дату в ближайшие 14 дней:", keyboard)

    def opening_categories(self, username: str) -> list[tuple[str, str]]:
        items = MobiFitness(account_token(username)).schedule(datetime.now(MOSCOW).date(), 7)
        categories: dict[str, tuple[str, str]] = {}
        for item in items:
            activity = item.get("activity", {})
            title = item_name(item)
            if item.get("preEntry") and activity.get("id") and class_allowed(title):
                categories.setdefault(title.casefold(), (activity["id"], title))
        return sorted(categories.values(), key=lambda category: category[1].casefold())

    def send_opening_categories(self, chat_id: int, username: str, page: int) -> None:
        categories = self.opening_categories(username)
        pages = max(1, (len(categories) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        shown = categories[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]
        keyboard = [
            [{"text": title[:64], "callback_data": f"opening|{activity_id}"}]
            for activity_id, title in shown
        ]
        navigation = []
        if page:
            navigation.append({"text": "←", "callback_data": f"openings|{page - 1}"})
        if page + 1 < pages:
            navigation.append({"text": "→", "callback_data": f"openings|{page + 1}"})
        if navigation:
            keyboard.append(navigation)
        text = f"Виды занятий на ближайшие 7 дней: {len(categories)}. Страница {page + 1}/{pages}."
        self.telegram.send(chat_id, text, keyboard or None)

    def sync_opening_events(
        self, username: str, title: str, api: MobiFitness, items: list[dict], suppress_open: bool = False
    ) -> None:
        now = datetime.now(MOSCOW)
        known = {
            row["event_id"]
            for row in self.database.execute("SELECT event_id FROM opening_events WHERE username=?", (username,))
        }
        for item in items:
            if item["id"] in known or item_name(item).casefold() != title.casefold():
                continue
            if local_datetime(item["datetime"]) <= now:
                continue
            detail = api.item(item["id"])
            if not detail.get("beginDate"):
                continue
            self.database.execute(
                """INSERT OR IGNORE INTO opening_events(username,title,event_id,starts_at,opens_at,notified)
                   VALUES(?,?,?,?,?,?)""",
                (
                    username,
                    title,
                    item["id"],
                    item["datetime"],
                    detail["beginDate"],
                    int(suppress_open and registration_opened(detail, now)),
                ),
            )
        self.database.commit()

    def add_opening_watch(self, chat_id: int, username: str, activity_id: str) -> None:
        api = MobiFitness(account_token(username))
        items = api.schedule(datetime.now(MOSCOW).date(), 7)
        selected = next((item for item in items if item.get("activity", {}).get("id") == activity_id), None)
        if not selected:
            raise ValueError("Этот вид занятия больше не найден в расписании.")
        title = item_name(selected)
        self.database.execute(
            """INSERT INTO opening_watches(chat_id,username,activity_id,title,status)
               VALUES(?,?,?,?,'active')
               ON CONFLICT(username,title) DO UPDATE SET
                   chat_id=excluded.chat_id,activity_id=excluded.activity_id,status='active'""",
            (chat_id, username, activity_id, title),
        )
        self.database.commit()
        self.sync_opening_events(username, title, api, items, suppress_open=True)
        self.telegram.send(chat_id, f"Слежу за открытием записи: {title}")

    def add_watch(self, chat_id: int, username: str, item: dict) -> None:
        self.database.execute(
            """INSERT INTO watches(chat_id,username,event_id,title,starts_at,status,next_check)
               VALUES(?,?,?,?,?,'active',0)
               ON CONFLICT(username,event_id) DO UPDATE SET status='active',chat_id=excluded.chat_id,next_check=0""",
            (chat_id, username, item["id"], item_name(item), item["datetime"]),
        )
        self.database.commit()

    def select_event(self, chat_id: int, username: str, event_id: str) -> None:
        api = MobiFitness(account_token(username))
        item = api.item(event_id)
        if item.get("reserved"):
            self.telegram.send(chat_id, f"Вы уже записаны: {item_time(item)} · {item_name(item)}")
            return
        if (slots(item) or 0) > 0:
            result = api.reserve(event_id)
            if result.get("reserved"):
                self.telegram.send(chat_id, f"ОК, записал: {item_time(result)} · {item_name(result)}")
                return
        self.add_watch(chat_id, username, item)
        self.telegram.send(chat_id, f"Слежу раз в {CHECK_INTERVAL} секунд: {item_time(item)} · {item_name(item)}")

    def watches(self, chat_id: int, username: str) -> None:
        rows = self.database.execute(
            "SELECT * FROM watches WHERE username=? AND status='active' ORDER BY starts_at", (username,)
        ).fetchall()
        openings = self.database.execute(
            "SELECT * FROM opening_watches WHERE username=? AND status='active' ORDER BY title", (username,)
        ).fetchall()
        keyboard = [
            [
                {
                    "text": f"❌ Автозапись · {item_time({'datetime': row['starts_at']})} · {row['title']}"[:64],
                    "callback_data": f"stop|{row['event_id']}",
                }
            ]
            for row in rows
        ]
        keyboard += [
            [
                {
                    "text": f"❌ Оповещения · {row['title']}"[:64],
                    "callback_data": f"stopopening|{row['activity_id']}",
                }
            ]
            for row in openings
        ]
        self.telegram.send(
            chat_id,
            "Мои подписки\n"
            f"Ожидание места с автозаписью: {len(rows)}\n"
            f"Оповещения об открытии записи: {len(openings)}\n\n"
            "Нажмите ❌, чтобы отменить подписку.",
            keyboard or None,
        )

    def handle_message(self, message: dict) -> None:
        chat_id, username = self.user({"from": message.get("from"), "message": message})
        text = (message.get("text") or "").strip()
        if text in {"/watches", "Мои подписки"}:
            self.watches(chat_id, username)
        elif text in {"/openings", "Следить за открытием записи"}:
            self.send_opening_categories(chat_id, username, 0)
        elif text == "Расписание":
            self.set_query(chat_id, "")
            self.send_ranges(chat_id)
        elif text in {"/start", "/all"}:
            self.set_query(chat_id, "")
            self.telegram.send_main_menu(
                chat_id,
                f"Аккаунт @{username} подключён. Используйте кнопки меню или напишите часть названия занятия.",
            )
            self.send_ranges(chat_id)
        elif text.startswith("/"):
            self.telegram.send(chat_id, "Команды: /start, /all, /openings, /watches")
        else:
            self.set_query(chat_id, text)
            self.send_ranges(chat_id)

    def handle_callback(self, callback: dict) -> None:
        chat_id, username = self.user({"from": callback.get("from"), "chat": callback.get("message", {}).get("chat")})
        self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        action, value, *rest = callback.get("data", "").split("|")
        if action == "range":
            self.send_schedule(chat_id, username, value, int(rest[0]))
        elif action == "dates":
            self.send_dates(chat_id)
        elif action == "date":
            self.send_schedule(chat_id, username, value, int(rest[0]))
        elif action == "openings":
            self.send_opening_categories(chat_id, username, int(value))
        elif action == "opening":
            self.add_opening_watch(chat_id, username, value)
        elif action == "subscriptions":
            self.watches(chat_id, username)
        elif action == "watch":
            self.select_event(chat_id, username, value)
        elif action == "stop":
            self.database.execute("DELETE FROM watches WHERE username=? AND event_id=?", (username, value))
            self.database.commit()
            self.telegram.send(chat_id, "Отслеживание остановлено.")
        elif action == "stopopening":
            self.database.execute(
                "DELETE FROM opening_watches WHERE username=? AND activity_id=?", (username, value)
            )
            self.database.commit()
            self.telegram.send(chat_id, "Слежение за открытием записи остановлено.")

    def check_watches(self) -> None:
        now = int(time.time())
        rows = self.database.execute(
            "SELECT * FROM watches WHERE status='active' AND next_check<=?", (now,)
        ).fetchall()
        for row in rows:
            if watch_deadline_reached(row["starts_at"]):
                self.database.execute(
                    "UPDATE watches SET status='expired' WHERE username=? AND event_id=?",
                    (row["username"], row["event_id"]),
                )
                self.database.commit()
                self.telegram.send(
                    row["chat_id"],
                    f"Слежение остановлено: до занятия осталось 15 минут · "
                    f"{item_time({'datetime': row['starts_at']})} · {row['title']}",
                )
                continue
            self.database.execute(
                "UPDATE watches SET next_check=? WHERE username=? AND event_id=?",
                (now + CHECK_INTERVAL, row["username"], row["event_id"]),
            )
            self.database.commit()
            try:
                api = MobiFitness(account_token(row["username"]))
                item = api.item(row["event_id"])
                if not item.get("reserved") and (slots(item) or 0) > 0:
                    item = api.reserve(row["event_id"])
                if item.get("reserved"):
                    self.database.execute(
                        "UPDATE watches SET status='booked' WHERE username=? AND event_id=?",
                        (row["username"], row["event_id"]),
                    )
                    self.database.commit()
                    self.telegram.send(row["chat_id"], f"ОК, место появилось и запись выполнена: {item_time(item)} · {item_name(item)}")
            except ApiError as error:
                if error.status == 401:
                    self.database.execute(
                        "UPDATE watches SET status='auth_error' WHERE username=? AND event_id=?",
                        (row["username"], row["event_id"]),
                    )
                    self.database.commit()
                    self.telegram.send(row["chat_id"], "Сессия Pride Fitness истекла. Нужна новая SMS-авторизация.")
                else:
                    print(f"Проверка {row['event_id']}: HTTP {error.status} {error}", file=sys.stderr)
            except Exception as error:
                print(f"Проверка {row['event_id']}: {error}", file=sys.stderr)

    def check_openings(self) -> None:
        now_timestamp = int(time.time())
        if now_timestamp < self.next_opening_check:
            return
        self.next_opening_check = now_timestamp + OPENING_CHECK_INTERVAL
        watches = self.database.execute(
            "SELECT * FROM opening_watches WHERE status='active' ORDER BY username,title"
        ).fetchall()
        for username in sorted({row["username"] for row in watches}):
            user_watches = [row for row in watches if row["username"] == username]
            try:
                api = MobiFitness(account_token(username))
                items = api.schedule(datetime.now(MOSCOW).date(), 7)
                for row in user_watches:
                    self.sync_opening_events(username, row["title"], api, items)
            except ApiError as error:
                if error.status == 401:
                    self.database.execute(
                        "UPDATE opening_watches SET status='auth_error' WHERE username=?", (username,)
                    )
                    self.database.commit()
                    for chat_id in {row["chat_id"] for row in user_watches}:
                        self.telegram.send(chat_id, "Сессия Pride Fitness истекла. Нужна новая SMS-авторизация.")
                else:
                    print(f"Проверка открытий @{username}: HTTP {error.status} {error}", file=sys.stderr)
            except Exception as error:
                print(f"Проверка открытий @{username}: {error}", file=sys.stderr)

        now = datetime.now(MOSCOW)
        due = self.database.execute(
            """SELECT event.*, watch.chat_id
               FROM opening_events AS event
               JOIN opening_watches AS watch
                 ON watch.username=event.username AND watch.title=event.title
               WHERE event.notified=0 AND watch.status='active'"""
        ).fetchall()
        weekdays = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
        for event in due:
            if local_datetime(event["opens_at"]) > now:
                continue
            starts_at = local_datetime(event["starts_at"])
            self.telegram.send(
                event["chat_id"],
                f"Открылась запись: {event['title']} · {starts_at:%d.%m}, {weekdays[starts_at.weekday()]}, {starts_at:%H:%M}",
            )
            self.database.execute(
                "UPDATE opening_events SET notified=1 WHERE username=? AND event_id=?",
                (event["username"], event["event_id"]),
            )
            self.database.commit()

    def run(self) -> None:
        offset = 0
        self.telegram.call(
            "setMyCommands",
            commands=[
                {"command": "start", "description": "Главное меню"},
                {"command": "openings", "description": "Следить за открытием записи"},
                {"command": "watches", "description": "Мои подписки"},
                {"command": "all", "description": "Все занятия"},
            ],
        )
        self.telegram.call("setChatMenuButton", menu_button={"type": "commands"})
        print("Pride bot запущен", flush=True)
        while True:
            self.check_watches()
            self.check_openings()
            try:
                updates = self.telegram.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        if "message" in update:
                            self.handle_message(update["message"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                    except (ValueError, ApiError) as error:
                        chat = update.get("message", {}).get("chat") or update.get("callback_query", {}).get("message", {}).get("chat")
                        if chat:
                            self.telegram.send(chat["id"], str(error))
            except Exception as error:
                print(f"Telegram: {error}", file=sys.stderr)
                time.sleep(5)


def cli_list(username: str, mode: str, query: str) -> None:
    start, days = range_dates(mode)
    for item in MobiFitness(account_token(username)).schedule(start, days):
        if query.casefold() not in item_name(item).casefold():
            continue
        free = slots(item)
        print(f"{item['id']}  {item_time(item)}  {item_name(item)}  {free}/{item.get('totalSlots')}")


def cli_watch(username: str, event_ids: list[str], interval: int) -> None:
    api = MobiFitness(account_token(username))
    pending = set(event_ids)
    while pending:
        for event_id in list(pending):
            item = api.item(event_id)
            if not item.get("reserved") and (slots(item) or 0) > 0:
                item = api.reserve(event_id)
            if item.get("reserved"):
                print(f"ОК: {item_time(item)} · {item_name(item)}")
                pending.remove(event_id)
            else:
                print(f"{datetime.now(MOSCOW):%H:%M:%S}: {item_name(item)} — мест нет", flush=True)
        if pending:
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pride Fitness vacancy watcher")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("bot")
    listing = sub.add_parser("list")
    listing.add_argument("--username", default=os.getenv("PRIDE_USERNAME", "annn_uu"))
    listing.add_argument("--range", choices=["today", "tomorrow", "week"], default="week")
    listing.add_argument("--query", default="")
    watching = sub.add_parser("watch")
    watching.add_argument("event_ids", nargs="+")
    watching.add_argument("--username", default=os.getenv("PRIDE_USERNAME", "annn_uu"))
    watching.add_argument("--interval", type=int, default=CHECK_INTERVAL)
    for command in ("book", "cancel"):
        action = sub.add_parser(command)
        action.add_argument("event_id")
        action.add_argument("--username", default=os.getenv("PRIDE_USERNAME", "annn_uu"))
    args = parser.parse_args()
    if args.command in (None, "bot"):
        PrideBot().run()
    elif args.command == "list":
        cli_list(args.username, args.range, args.query)
    elif args.command == "watch":
        cli_watch(args.username, args.event_ids, args.interval)
    else:
        api = MobiFitness(account_token(args.username))
        item = api.reserve(args.event_id) if args.command == "book" else api.cancel(args.event_id)
        print(json.dumps({"reserved": item.get("reserved"), "availableSlots": item.get("availableSlots")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
