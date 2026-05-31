from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken


class UserStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReaCredentials:
    login: str
    password: str


@dataclass(frozen=True)
class StoredCredentials:
    telegram_user_id: int
    login: str
    password: str


@dataclass(frozen=True)
class PendingLogin:
    telegram_user_id: int
    login: str
    pending_action: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class RatingSnapshot:
    subject: str
    total: str
    attendance: str | None = None
    control: str | None = None
    creative: str | None = None
    intermediate: str | None = None


@dataclass(frozen=True)
class ScheduleSnapshot:
    telegram_user_id: int
    schedule_hash: str
    schedule_text: str
    schedule_key: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class NotificationOutboxItem:
    id: int
    telegram_user_id: int
    event_type: str
    message_text: str
    attempts: int


class UserStore:
    """SQLite persistence for the VPS deployment.

    User-facing cache snapshots and notification baselines intentionally live in
    separate tables. A manual cache refresh must never consume a notification.
    """

    def __init__(self) -> None:
        self.database_url = ""
        self.use_postgres = False
        self.db_path = Path(os.getenv("BOT_DB_PATH", "credentials.sqlite3"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        key = os.getenv("BOT_CREDENTIAL_KEY", "").strip()
        if not key:
            raise UserStoreError("BOT_CREDENTIAL_KEY не задан в .env")
        self.cipher = Fernet(key.encode("utf-8"))

        if not _env_bool("BOT_SKIP_DB_INIT", False):
            self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15.0)
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if 1 not in applied:
                self._migration_1_existing_schema(connection)
                connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
            if 2 not in applied:
                self._migration_2_reliable_notifications(connection)
                connection.execute("INSERT INTO schema_migrations (version) VALUES (2)")

    def _migration_1_existing_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rea_credentials (
                telegram_user_id INTEGER PRIMARY KEY,
                rea_login TEXT NOT NULL,
                rea_password_encrypted BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER,
                event_type TEXT NOT NULL,
                message_text TEXT,
                subject_query TEXT,
                subject_matched TEXT,
                result_status TEXT,
                response_text TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_bot_events_created_at
                ON bot_events (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bot_events_telegram_user_id
                ON bot_events (telegram_user_id);
            CREATE TABLE IF NOT EXISTS rating_snapshots (
                telegram_user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                total TEXT NOT NULL,
                attendance TEXT,
                control TEXT,
                creative TEXT,
                intermediate TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_user_id, subject)
            );
            CREATE INDEX IF NOT EXISTS idx_rating_snapshots_user_id
                ON rating_snapshots (telegram_user_id);
            CREATE TABLE IF NOT EXISTS user_settings (
                telegram_user_id INTEGER PRIMARY KEY,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pending_logins (
                telegram_user_id INTEGER PRIMARY KEY,
                rea_login TEXT NOT NULL,
                pending_action TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS schedule_snapshots (
                telegram_user_id INTEGER PRIMARY KEY,
                schedule_key TEXT,
                schedule_hash TEXT NOT NULL,
                schedule_text TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_subscribers (
                telegram_user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO bot_subscribers (telegram_user_id)
                SELECT telegram_user_id FROM rea_credentials;
            """
        )
        self._ensure_column(connection, "schedule_snapshots", "schedule_key", "TEXT")

    def _migration_2_reliable_notifications(self, connection: sqlite3.Connection) -> None:
        self._ensure_column(connection, "bot_subscribers", "announcements_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "bot_subscribers", "active", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(connection, "bot_subscribers", "last_error", "TEXT")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rating_notification_snapshots (
                telegram_user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                total TEXT NOT NULL,
                attendance TEXT,
                control TEXT,
                creative TEXT,
                intermediate TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_user_id, subject)
            );
            CREATE TABLE IF NOT EXISTS schedule_notification_snapshots (
                telegram_user_id INTEGER PRIMARY KEY,
                schedule_key TEXT,
                schedule_hash TEXT NOT NULL,
                schedule_text TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                message_text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
                ON notification_outbox (sent_at, next_attempt_at);
            CREATE TABLE IF NOT EXISTS rea_sync_jobs (
                telegram_user_id INTEGER PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_rea_sync_jobs_due
                ON rea_sync_jobs (next_attempt_at);
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO rating_notification_snapshots (
                telegram_user_id, subject, total, attendance, control, creative, intermediate
            )
            SELECT telegram_user_id, subject, total, attendance, control, creative, intermediate
            FROM rating_snapshots
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO schedule_notification_snapshots (
                telegram_user_id, schedule_key, schedule_hash, schedule_text
            )
            SELECT telegram_user_id, schedule_key, schedule_hash, schedule_text
            FROM schedule_snapshots
            """
        )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def subscribe_user(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_subscribers (telegram_user_id)
                VALUES (?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id,),
            )

    def unsubscribe_user(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_subscribers WHERE telegram_user_id = ?", (telegram_user_id,))

    def announcements_enabled(self, telegram_user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT announcements_enabled FROM bot_subscribers WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return bool(row[0]) if row else True

    def set_announcements_enabled(self, telegram_user_id: int, enabled: bool) -> None:
        self.subscribe_user(telegram_user_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE bot_subscribers
                SET announcements_enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ?
                """,
                (int(enabled), telegram_user_id),
            )

    def deactivate_subscriber(self, telegram_user_id: int, error_message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE bot_subscribers
                SET active = 0, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ?
                """,
                (_limit_text(error_message), telegram_user_id),
            )

    def list_subscriber_ids(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_user_id
                FROM bot_subscribers
                WHERE active = 1 AND announcements_enabled = 1
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [int(row[0]) for row in rows]

    def log_event(
        self,
        *,
        telegram_user_id: int | None,
        event_type: str,
        message_text: str | None = None,
        subject_query: str | None = None,
        subject_matched: str | None = None,
        result_status: str | None = None,
        response_text: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_events (
                    telegram_user_id, event_type, message_text, subject_query,
                    subject_matched, result_status, response_text, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    event_type,
                    _limit_text(message_text),
                    _limit_text(subject_query),
                    _limit_text(subject_matched),
                    _limit_text(result_status, 100),
                    _limit_text(response_text),
                    _limit_text(error_message),
                ),
            )

    def prune_old_events(self, retention_days: int = 90) -> None:
        cutoff = _timestamp(datetime.now(timezone.utc) - timedelta(days=max(retention_days, 1)))
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_events WHERE created_at < ?", (cutoff,))

    def get_credentials(self, telegram_user_id: int) -> ReaCredentials | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT rea_login, rea_password_encrypted
                FROM rea_credentials
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            password = self.cipher.decrypt(bytes(row[1])).decode("utf-8")
        except InvalidToken:
            self.delete_credentials(telegram_user_id)
            return None
        return ReaCredentials(login=str(row[0]), password=password)

    def list_credentials(self) -> list[StoredCredentials]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_user_id, rea_login, rea_password_encrypted
                FROM rea_credentials
                ORDER BY updated_at ASC
                """
            ).fetchall()
        return self._decrypt_credentials(rows)

    def list_notification_credentials(self) -> list[StoredCredentials]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.telegram_user_id, c.rea_login, c.rea_password_encrypted
                FROM rea_credentials AS c
                LEFT JOIN user_settings AS s ON s.telegram_user_id = c.telegram_user_id
                WHERE COALESCE(s.notifications_enabled, 1) = 1
                ORDER BY c.updated_at ASC
                """
            ).fetchall()
        return self._decrypt_credentials(rows)

    def _decrypt_credentials(self, rows) -> list[StoredCredentials]:
        credentials: list[StoredCredentials] = []
        invalid_user_ids: list[int] = []
        for telegram_user_id, login, encrypted_password in rows:
            try:
                password = self.cipher.decrypt(bytes(encrypted_password)).decode("utf-8")
            except InvalidToken:
                invalid_user_ids.append(int(telegram_user_id))
                continue
            credentials.append(StoredCredentials(int(telegram_user_id), str(login), password))
        for telegram_user_id in invalid_user_ids:
            self.delete_credentials(telegram_user_id)
        return credentials

    def save_credentials(self, *, telegram_user_id: int, rea_login: str, rea_password: str) -> None:
        encrypted_password = self.cipher.encrypt(rea_password.encode("utf-8"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rea_credentials (telegram_user_id, rea_login, rea_password_encrypted)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    rea_login = excluded.rea_login,
                    rea_password_encrypted = excluded.rea_password_encrypted,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id, rea_login, encrypted_password),
            )
            for table in (
                "rating_snapshots",
                "schedule_snapshots",
                "rating_notification_snapshots",
                "schedule_notification_snapshots",
                "notification_outbox",
            ):
                connection.execute(f"DELETE FROM {table} WHERE telegram_user_id = ?", (telegram_user_id,))
            connection.execute(
                """
                INSERT INTO rea_sync_jobs (telegram_user_id)
                VALUES (?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    attempts = 0,
                    next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id,),
            )

    def notifications_enabled(self, telegram_user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT notifications_enabled FROM user_settings WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return bool(row[0]) if row else True

    def list_notification_settings(self) -> dict[int, bool]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT telegram_user_id, notifications_enabled FROM user_settings"
            ).fetchall()
        return {int(user_id): bool(enabled) for user_id, enabled in rows}

    def set_notifications_enabled(self, telegram_user_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_settings (telegram_user_id, notifications_enabled)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    notifications_enabled = excluded.notifications_enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id, int(enabled)),
            )

    def save_pending_login(
        self,
        *,
        telegram_user_id: int,
        rea_login: str,
        pending_action: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_logins (telegram_user_id, rea_login, pending_action)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    rea_login = excluded.rea_login,
                    pending_action = excluded.pending_action,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id, rea_login, pending_action),
            )

    def get_pending_login(self, telegram_user_id: int) -> PendingLogin | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT rea_login, pending_action, updated_at
                FROM pending_logins
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        ttl_minutes = max(_env_int("PENDING_LOGIN_TTL_MINUTES", 30), 1)
        if _parse_timestamp(str(row[2])) < datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes):
            self.delete_pending_login(telegram_user_id)
            return None
        return PendingLogin(telegram_user_id, str(row[0]), row[1], str(row[2]))

    def delete_pending_login(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM pending_logins WHERE telegram_user_id = ?", (telegram_user_id,))

    def get_rating_snapshots(self, telegram_user_id: int) -> dict[str, RatingSnapshot]:
        return self._get_rating_snapshots_from("rating_snapshots", telegram_user_id)

    def get_rating_notification_snapshots(self, telegram_user_id: int) -> dict[str, RatingSnapshot]:
        return self._get_rating_snapshots_from("rating_notification_snapshots", telegram_user_id)

    def _get_rating_snapshots_from(self, table: str, telegram_user_id: int) -> dict[str, RatingSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT subject, total, attendance, control, creative, intermediate
                FROM {table}
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchall()
        return {
            str(row[0]): RatingSnapshot(str(row[0]), str(row[1]), row[2], row[3], row[4], row[5])
            for row in rows
        }

    def list_rating_snapshots(self) -> dict[int, dict[str, RatingSnapshot]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_user_id, subject, total, attendance, control, creative, intermediate
                FROM rating_snapshots
                ORDER BY telegram_user_id, subject
                """
            ).fetchall()
        snapshots: dict[int, dict[str, RatingSnapshot]] = {}
        for user_id, subject, total, attendance, control, creative, intermediate in rows:
            snapshots.setdefault(int(user_id), {})[str(subject)] = RatingSnapshot(
                str(subject), str(total), attendance, control, creative, intermediate
            )
        return snapshots

    def replace_rating_snapshots(self, *, telegram_user_id: int, snapshots: list[RatingSnapshot]) -> None:
        with self._connect() as connection:
            self._replace_rating_rows(connection, "rating_snapshots", telegram_user_id, snapshots)

    def replace_rating_notification_snapshots(
        self,
        *,
        telegram_user_id: int,
        snapshots: list[RatingSnapshot],
        event_type: str | None = None,
        message_text: str | None = None,
        dedupe_key: str | None = None,
    ) -> None:
        with self._connect() as connection:
            self._replace_rating_rows(connection, "rating_notification_snapshots", telegram_user_id, snapshots)
            if event_type and message_text and dedupe_key:
                self._enqueue_notification(connection, telegram_user_id, event_type, message_text, dedupe_key)

    @staticmethod
    def _replace_rating_rows(
        connection: sqlite3.Connection,
        table: str,
        telegram_user_id: int,
        snapshots: list[RatingSnapshot],
    ) -> None:
        connection.execute(f"DELETE FROM {table} WHERE telegram_user_id = ?", (telegram_user_id,))
        connection.executemany(
            f"""
            INSERT INTO {table} (
                telegram_user_id, subject, total, attendance, control, creative, intermediate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    telegram_user_id,
                    item.subject,
                    item.total,
                    item.attendance,
                    item.control,
                    item.creative,
                    item.intermediate,
                )
                for item in snapshots
            ],
        )

    def get_rating_updated_at(self, telegram_user_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(updated_at) FROM rating_snapshots WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def get_schedule_snapshot(self, telegram_user_id: int) -> ScheduleSnapshot | None:
        return self._get_schedule_snapshot_from("schedule_snapshots", telegram_user_id)

    def get_schedule_notification_snapshot(self, telegram_user_id: int) -> ScheduleSnapshot | None:
        return self._get_schedule_snapshot_from("schedule_notification_snapshots", telegram_user_id)

    def _get_schedule_snapshot_from(self, table: str, telegram_user_id: int) -> ScheduleSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT schedule_hash, schedule_text, schedule_key, updated_at
                FROM {table}
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        return ScheduleSnapshot(telegram_user_id, str(row[0]), str(row[1]), row[2], str(row[3]))

    def list_schedule_snapshots(self) -> dict[int, ScheduleSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_user_id, schedule_hash, schedule_text, schedule_key, updated_at
                FROM schedule_snapshots
                """
            ).fetchall()
        return {
            int(user_id): ScheduleSnapshot(int(user_id), str(digest), str(text), key, str(updated_at))
            for user_id, digest, text, key, updated_at in rows
        }

    def save_schedule_snapshot(
        self,
        *,
        telegram_user_id: int,
        schedule_hash: str,
        schedule_text: str,
        schedule_key: str | None = None,
    ) -> None:
        with self._connect() as connection:
            self._save_schedule_row(
                connection, "schedule_snapshots", telegram_user_id, schedule_hash, schedule_text, schedule_key
            )

    def save_schedule_notification_snapshot(
        self,
        *,
        telegram_user_id: int,
        schedule_hash: str,
        schedule_text: str,
        schedule_key: str | None = None,
        event_type: str | None = None,
        message_text: str | None = None,
        dedupe_key: str | None = None,
    ) -> None:
        with self._connect() as connection:
            self._save_schedule_row(
                connection,
                "schedule_notification_snapshots",
                telegram_user_id,
                schedule_hash,
                schedule_text,
                schedule_key,
            )
            if event_type and message_text and dedupe_key:
                self._enqueue_notification(connection, telegram_user_id, event_type, message_text, dedupe_key)

    @staticmethod
    def _save_schedule_row(
        connection: sqlite3.Connection,
        table: str,
        telegram_user_id: int,
        schedule_hash: str,
        schedule_text: str,
        schedule_key: str | None,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {table} (telegram_user_id, schedule_key, schedule_hash, schedule_text)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                schedule_key = excluded.schedule_key,
                schedule_hash = excluded.schedule_hash,
                schedule_text = excluded.schedule_text,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_user_id, schedule_key, schedule_hash, schedule_text),
        )

    @staticmethod
    def _enqueue_notification(
        connection: sqlite3.Connection,
        telegram_user_id: int,
        event_type: str,
        message_text: str,
        dedupe_key: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox (
                telegram_user_id, event_type, dedupe_key, message_text
            )
            VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, event_type, dedupe_key, message_text),
        )

    def list_due_notifications(self, limit: int = 100) -> list[NotificationOutboxItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, telegram_user_id, event_type, message_text, attempts
                FROM notification_outbox
                WHERE sent_at IS NULL AND next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [NotificationOutboxItem(int(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4])) for r in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (notification_id,),
            )

    def mark_notification_retry(self, notification_id: int, error_message: str, delay_seconds: int) -> None:
        next_attempt = _timestamp(datetime.now(timezone.utc) + timedelta(seconds=max(delay_seconds, 1)))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_attempt, _limit_text(error_message), notification_id),
            )

    def schedule_sync_retry(self, telegram_user_id: int, error_message: str | None = None) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM rea_sync_jobs WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            delay_seconds = min(300 * (2 ** min(attempts - 1, 4)), 3600)
            next_attempt = _timestamp(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
            connection.execute(
                """
                INSERT INTO rea_sync_jobs (telegram_user_id, attempts, next_attempt_at, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    attempts = excluded.attempts,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_user_id, attempts, next_attempt, _limit_text(error_message)),
            )

    def mark_sync_completed(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM rea_sync_jobs WHERE telegram_user_id = ?", (telegram_user_id,))

    def list_due_sync_credentials(self, limit: int = 20) -> list[StoredCredentials]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.telegram_user_id, c.rea_login, c.rea_password_encrypted
                FROM rea_sync_jobs AS j
                JOIN rea_credentials AS c ON c.telegram_user_id = j.telegram_user_id
                WHERE j.next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY j.next_attempt_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._decrypt_credentials(rows)

    def delete_credentials(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            for table in (
                "rea_credentials",
                "rating_snapshots",
                "schedule_snapshots",
                "rating_notification_snapshots",
                "schedule_notification_snapshots",
                "user_settings",
                "pending_logins",
                "notification_outbox",
                "rea_sync_jobs",
            ):
                connection.execute(f"DELETE FROM {table} WHERE telegram_user_id = ?", (telegram_user_id,))

    def delete_all_user_data(self, telegram_user_id: int) -> None:
        self.delete_credentials(telegram_user_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_subscribers WHERE telegram_user_id = ?", (telegram_user_id,))
            connection.execute("DELETE FROM bot_events WHERE telegram_user_id = ?", (telegram_user_id,))

    def admin_stats(self) -> dict[str, int | str | None]:
        with self._connect() as connection:
            values: dict[str, int | str | None] = {
                "authorized_users": int(connection.execute("SELECT COUNT(*) FROM rea_credentials").fetchone()[0]),
                "active_subscribers": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM bot_subscribers WHERE active = 1 AND announcements_enabled = 1"
                    ).fetchone()[0]
                ),
                "pending_sync_jobs": int(connection.execute("SELECT COUNT(*) FROM rea_sync_jobs").fetchone()[0]),
                "pending_notifications": int(
                    connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE sent_at IS NULL").fetchone()[0]
                ),
                "latest_monitor_at": None,
                "latest_monitor_status": None,
            }
            row = connection.execute(
                """
                SELECT created_at, result_status
                FROM bot_events
                WHERE event_type = 'rating_monitor'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                values["latest_monitor_at"] = str(row[0])
                values["latest_monitor_status"] = str(row[1])
        return values


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _limit_text(value: str | None, limit: int = 2000) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else f"{value[:limit]}..."
