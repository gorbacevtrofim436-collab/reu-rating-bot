from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from rating_parser import RatingItem, find_subject_score, parse_rating_html
from schedule_parser import parse_schedule_html, schedule_snapshot_text, schedule_week_key
from user_store import RatingSnapshot, UserStore


RATING_HTML = """
<div class="es-rating__line-parent">
  <div class="es-rating__discipline">Математический анализ</div>
  <div class="es-rating__attendance">10</div>
  <div class="es-rating__control">20</div>
  <div class="es-rating__creative">3</div>
  <div class="es-rating__form">0</div>
  <div class="es-rating__total">33</div>
</div>
<div class="es-rating__line-parent">
  <div class="es-rating__discipline">Иностранный язык</div>
  <div class="es-rating__attendance">12</div>
  <div class="es-rating__control">18</div>
  <div class="es-rating__creative">2</div>
  <div class="es-rating__form">0</div>
  <div class="es-rating__total">32</div>
</div>
"""


SCHEDULE_HTML = """
<h1>Расписание для группы: ТЕСТ-01 (обновлено сегодня)</h1>
<table class="table_lessons">
  <tr><td>22 неделя</td><td>Понедельник, 01.06.2026</td></tr>
  <tr>
    <td>1 пара<br>8:30-10:00</td>
    <td>
      <div class="lesson__block"><span>Математический анализ
      3 корпус 101
      Практическое занятие</span></div>
      <div class="lesson_popup">Преподаватель: Тестовый Преподаватель
      Аудитория: 101</div>
    </td>
  </tr>
  <tr><td></td><td>Вторник, 02.06.2026</td></tr>
</table>
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bot.sqlite3"
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        os.environ["BOT_CREDENTIAL_KEY"] = Fernet.generate_key().decode()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_migrates_old_subscribers_and_separates_notification_baseline(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE bot_subscribers (
                    telegram_user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute("INSERT INTO bot_subscribers (telegram_user_id) VALUES (1)")
            connection.commit()

        store = UserStore()
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(bot_subscribers)")
            }
        self.assertIn("announcements_enabled", columns)
        self.assertIn("active", columns)

        store.replace_rating_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "20")],
        )
        store.replace_rating_notification_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "19")],
        )
        store.replace_rating_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "21")],
        )
        self.assertEqual(
            store.get_rating_notification_snapshots(1)["Математический анализ"].total,
            "19",
        )

    def test_outbox_retries_and_marks_delivery(self) -> None:
        store = UserStore()
        store.replace_rating_notification_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "21")],
            event_type="rating_change",
            message_text="Изменились баллы",
            dedupe_key="unique",
        )
        due = store.list_due_notifications()
        self.assertEqual(len(due), 1)
        store.mark_notification_retry(due[0].id, "temporary", 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE notification_outbox SET next_attempt_at = CURRENT_TIMESTAMP"
            )
            connection.commit()
        due = store.list_due_notifications()
        self.assertEqual(due[0].attempts, 1)
        store.mark_notification_sent(due[0].id)
        self.assertEqual(store.list_due_notifications(), [])

    def test_pending_login_expires(self) -> None:
        os.environ["PENDING_LOGIN_TTL_MINUTES"] = "1"
        store = UserStore()
        store.save_pending_login(telegram_user_id=1, rea_login="Test.User")
        expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE pending_logins SET updated_at = ?", (expired,))
            connection.commit()
        self.assertIsNone(store.get_pending_login(1))

    def test_notification_setting_is_persisted(self) -> None:
        store = UserStore()
        self.assertTrue(store.notifications_enabled(1))
        store.set_notifications_enabled(1, False)
        self.assertFalse(UserStore().notifications_enabled(1))


class ParserTestCase(unittest.TestCase):
    def test_rating_component_and_alias(self) -> None:
        items = parse_rating_html(
            RATING_HTML,
            table_selector=None,
            table_index=None,
            subject_column_index=None,
            score_column_index=None,
        )
        self.assertEqual(find_subject_score(items, "МАТАН").total, "33")
        self.assertEqual(find_subject_score(items, "англ").subject, "Иностранный язык")

    def test_schedule_key_uses_monday(self) -> None:
        week = parse_schedule_html(SCHEDULE_HTML)
        self.assertEqual(schedule_week_key(week), "monday:2026-06-01")
        self.assertIn("Математический анализ", schedule_snapshot_text(week))


class BotUtilityTestCase(unittest.TestCase):
    def test_long_text_is_split_under_telegram_limit(self) -> None:
        from bot import split_telegram_text

        chunks = split_telegram_text("x" * 9000, limit=3900)
        self.assertEqual("".join(chunks), "x" * 9000)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))

    def test_rating_snapshot_validation_rejects_partial_page(self) -> None:
        from bot import validate_rating_items
        from rating_parser import RatingParseError

        previous = {f"Предмет {index}": RatingSnapshot(f"Предмет {index}", "1") for index in range(10)}
        with self.assertRaises(RatingParseError):
            validate_rating_items([RatingItem("Предмет 1", "2")], previous)

    def test_monitor_queues_repeated_rating_changes_without_losing_cache(self) -> None:
        import bot

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        os.environ["BOT_DB_PATH"] = str(Path(temp_dir.name) / "bot.sqlite3")
        os.environ["BOT_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
        bot.store = UserStore()
        bot.store.replace_rating_notification_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "33", "10", "20", "3", "0")],
        )

        async def apply_total(total: str) -> None:
            await bot.process_user_rating_changes(None, 1, RATING_HTML.replace(">33<", f">{total}<"))

        asyncio.run(apply_total("34"))
        first = bot.store.list_due_notifications()
        self.assertEqual(len(first), 1)
        self.assertEqual(bot.store.get_rating_snapshots(1)["Математический анализ"].total, "34")
        bot.store.mark_notification_sent(first[0].id)

        asyncio.run(apply_total("33"))
        second = bot.store.list_due_notifications()
        self.assertEqual(len(second), 1)
        bot.store.mark_notification_sent(second[0].id)

        asyncio.run(apply_total("34"))
        self.assertEqual(len(bot.store.list_due_notifications()), 1)

    def test_rating_menu_answers_from_sqlite_without_site_request(self) -> None:
        import bot

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
        os.environ["BOT_DB_PATH"] = str(Path(temp_dir.name) / "bot.sqlite3")
        os.environ["BOT_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
        bot.store = UserStore()
        bot.credentials_cache.clear()
        bot.rating_items_cache.clear()
        bot.store.save_credentials(
            telegram_user_id=1,
            rea_login="Test.User",
            rea_password="secret",
        )
        bot.store.replace_rating_snapshots(
            telegram_user_id=1,
            snapshots=[RatingSnapshot("Математический анализ", "33", "10", "20", "3", "0")],
        )

        class FakeMessage:
            from_user = SimpleNamespace(id=1)
            text = "Баллы"

            def __init__(self) -> None:
                self.answers: list[str] = []

            async def answer(self, text: str, **_kwargs) -> None:
                self.answers.append(text)

        class FakeState:
            def __init__(self) -> None:
                self.state = None

            async def set_state(self, state) -> None:
                self.state = state

        async def noop_log(*_args, **_kwargs) -> None:
            return None

        message = FakeMessage()
        state = FakeState()
        with (
            patch.object(bot, "_log_event", noop_log),
            patch.object(bot, "fetch_rating_and_schedule_html_once") as site_request,
        ):
            started_at = time.perf_counter()
            asyncio.run(bot.start_rating_flow(message, state))
            elapsed = time.perf_counter() - started_at

        site_request.assert_not_called()
        self.assertLess(elapsed, 0.5)
        self.assertIn("Выберите предмет:", message.answers[0])
        self.assertEqual(state.state, bot.RatingStates.waiting_for_subject)


if __name__ == "__main__":
    unittest.main()
