#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


DATABASE_PATH = Path("/opt/reu-rating-bot/data/credentials.sqlite3")
BACKUP_DIR = Path("/opt/reu-rating-bot/backups")
RETENTION_DAYS = 14


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    backup_path = BACKUP_DIR / f"credentials-{now:%Y-%m-%dT%H-%M-%SZ}.sqlite3"

    with sqlite3.connect(DATABASE_PATH) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    for candidate in BACKUP_DIR.glob("credentials-*.sqlite3"):
        if datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc) < cutoff:
            candidate.unlink()


if __name__ == "__main__":
    main()
