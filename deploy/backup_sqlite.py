#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet


DATABASE_PATH = Path(os.getenv("BOT_DB_PATH", "/opt/reu-rating-bot/data/credentials.sqlite3"))
BACKUP_DIR = Path(os.getenv("BOT_BACKUP_DIR", "/opt/reu-rating-bot/backups"))
OFFSITE_DIR = Path(os.environ["BOT_BACKUP_OFFSITE_DIR"]) if os.getenv("BOT_BACKUP_OFFSITE_DIR") else None
RETENTION_DAYS = int(os.getenv("BOT_BACKUP_RETENTION_DAYS", "14"))


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    backup_path = BACKUP_DIR / f"credentials-{now:%Y-%m-%dT%H-%M-%SZ}.sqlite3"

    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        backup_path.chmod(0o600)
        with closing(sqlite3.connect(backup_path)) as backup:
            result = backup.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    if result != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(f"SQLite backup integrity check failed: {result}")

    if OFFSITE_DIR:
        backup_key = os.getenv("BOT_BACKUP_KEY", "").strip()
        if not backup_key:
            raise RuntimeError("BOT_BACKUP_KEY is required when BOT_BACKUP_OFFSITE_DIR is configured")
        OFFSITE_DIR.mkdir(parents=True, exist_ok=True)
        offsite_path = OFFSITE_DIR / f"{backup_path.name}.fernet"
        offsite_path.write_bytes(Fernet(backup_key.encode("utf-8")).encrypt(backup_path.read_bytes()))
        offsite_path.chmod(0o600)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    for candidate in BACKUP_DIR.glob("credentials-*.sqlite3"):
        if datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc) < cutoff:
            candidate.unlink()


if __name__ == "__main__":
    main()
