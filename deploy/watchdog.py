#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DATABASE_PATH = Path(os.getenv("BOT_DB_PATH", "/opt/reu-rating-bot/data/credentials.sqlite3"))
BACKUP_DIR = Path(os.getenv("BOT_BACKUP_DIR", "/opt/reu-rating-bot/backups"))
STATE_PATH = DATABASE_PATH.parent / "watchdog-state.json"


def _telegram_request(method: str, values: dict[str, str] | None = None) -> dict:
    token = os.environ["BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urlencode(values or {}).encode("utf-8") if values else None
    request = Request(url, data=data, method="POST" if data else "GET")
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _ping_healthchecks(path_suffix: str = "") -> None:
    url = os.getenv("HEALTHCHECKS_PING_URL", "").strip()
    if not url:
        return
    with urlopen(f"{url.rstrip('/')}{path_suffix}", timeout=10):
        pass


def _service_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "reu-rating-bot.service"],
        check=False,
    )
    return result.returncode == 0


def _latest_monitor_state() -> tuple[float | None, int]:
    if not DATABASE_PATH.exists():
        return None, 0
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        row = connection.execute(
            """
            SELECT id, created_at
            FROM bot_events
            WHERE event_type = 'rating_monitor' AND result_status = 'cycle_started'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None, 0
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        failure_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM bot_events
            WHERE id > ?
              AND event_type IN ('rating_monitor', 'schedule_monitor')
              AND result_status IN ('fetch_failed', 'failed', 'parse_failed')
            """,
            (row[0],),
        ).fetchone()[0]
    created_at = datetime.fromisoformat(str(row[1])).replace(tzinfo=timezone.utc)
    return time.time() - created_at.timestamp(), int(failure_count)


def _latest_backup_age_seconds() -> float | None:
    backups = list(BACKUP_DIR.glob("credentials-*.sqlite3"))
    return time.time() - max(path.stat().st_mtime for path in backups) if backups else None


def _read_previous_status() -> str:
    try:
        return str(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("status", ""))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def _save_status(status: str) -> None:
    STATE_PATH.write_text(json.dumps({"status": status}), encoding="utf-8")
    STATE_PATH.chmod(0o600)


def _send_admin(text: str) -> None:
    admin_id = os.getenv("BOT_ADMIN_USER_ID", "").strip()
    if admin_id:
        _telegram_request("sendMessage", {"chat_id": admin_id, "text": text})


def main() -> None:
    failures: list[str] = []
    if not _service_is_active():
        failures.append("polling-сервис не активен")
    try:
        if not _telegram_request("getMe").get("ok"):
            failures.append("Telegram API вернул ошибку")
    except Exception as exc:
        failures.append(f"нет связи с Telegram API: {type(exc).__name__}")

    monitor_age, monitor_failure_count = _latest_monitor_state()
    if monitor_age is None or monitor_age > 3 * 60 * 60:
        failures.append("почасовой монитор давно не завершался")
    if monitor_failure_count >= 2:
        failures.append(
            f"последняя проверка ЛКС завершилась ошибками: {monitor_failure_count}"
        )
    backup_age = _latest_backup_age_seconds()
    if backup_age is None or backup_age > 36 * 60 * 60:
        failures.append("нет свежей резервной копии SQLite")

    status = "\n".join(failures) if failures else "ok"
    previous_status = _read_previous_status()
    if status != previous_status:
        if failures:
            try:
                _send_admin("Watchdog бота обнаружил проблему:\n- " + "\n- ".join(failures))
            except Exception:
                pass
        elif previous_status:
            try:
                _send_admin("Watchdog: работа бота восстановлена.")
            except Exception:
                pass
    _save_status(status)

    try:
        _ping_healthchecks("/fail" if failures else "")
    except Exception:
        pass
    if failures:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
