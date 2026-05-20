from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values


PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_ENV = PROJECT_DIR / ".env"
RUNTIME_ENV = Path.home() / ".local" / "share" / "reu-rating-bot" / ".env"
DEFAULT_URL = "https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр"
LAUNCH_LABEL = "com.codex.reu-rating-bot"


@dataclass(frozen=True)
class TableGuess:
    table_index: int
    subject_column_index: int
    score_column_index: int
    rows: list[list[str]]
    confidence: float


def clean_text(value: str) -> str:
    return " ".join(value.split())


def is_numeric_like(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if not re.search(r"\d", value):
        return False
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", value))
    digits = len(re.findall(r"\d", value))
    return digits >= letters


def is_subject_like(value: str) -> bool:
    value = value.strip()
    if len(value) < 6:
        return False
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", value))
    digits = len(re.findall(r"\d", value))
    return letters >= 5 and letters > digits


def table_rows(html: str) -> list[list[list[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    tables: list[list[list[str]]] = []
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for row in table.find_all("tr"):
            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def guess_table(tables: list[list[list[str]]]) -> TableGuess | None:
    best: TableGuess | None = None

    for table_index, rows in enumerate(tables):
        width = max((len(row) for row in rows), default=0)
        if width < 2 or len(rows) < 2:
            continue

        subject_scores: list[float] = []
        score_scores: list[float] = []

        for column_index in range(width):
            values = [row[column_index] for row in rows if column_index < len(row)]
            header_text = " ".join(values[:3]).casefold()
            subject_score = sum(1 for value in values if is_subject_like(value))
            numeric_score = sum(1 for value in values if is_numeric_like(value))

            if re.search(r"дисциплин|предмет|наименование|модул", header_text):
                subject_score += 5
            if re.search(r"балл|рейтинг|итог|сумм|оценк", header_text):
                numeric_score += 5

            subject_scores.append(float(subject_score))
            score_scores.append(float(numeric_score))

        subject_column_index = max(range(width), key=lambda idx: subject_scores[idx])
        score_column_index = max(
            (idx for idx in range(width) if idx != subject_column_index),
            key=lambda idx: score_scores[idx],
        )
        confidence = (
            subject_scores[subject_column_index]
            + score_scores[score_column_index]
            + min(len(rows), 20) * 0.25
        )

        if best is None or confidence > best.confidence:
            best = TableGuess(
                table_index=table_index,
                subject_column_index=subject_column_index,
                score_column_index=score_column_index,
                rows=rows,
                confidence=confidence,
            )

    return best


def fetch_html(url: str, cookie_header: str) -> str:
    response = requests.get(
        url,
        headers={
            "Cookie": cookie_header,
            "User-Agent": "personal-rating-telegram-bot/0.1",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.text


def quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for line in existing:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if not match:
            output.append(line)
            continue

        key = match.group(1)
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def print_guess(guess: TableGuess) -> None:
    print(f"Найдена вероятная таблица: RATING_TABLE_INDEX={guess.table_index}")
    print(f"Предмет: SUBJECT_COLUMN_INDEX={guess.subject_column_index}")
    print(f"Баллы: SCORE_COLUMN_INDEX={guess.score_column_index}")
    print()
    print("Примеры строк:")
    for row in guess.rows[:8]:
        subject = row[guess.subject_column_index] if guess.subject_column_index < len(row) else ""
        score = row[guess.score_column_index] if guess.score_column_index < len(row) else ""
        print(f"- {subject} => {score}")


def choose_int(prompt: str, default: int) -> int:
    value = input(f"{prompt} [{default}]: ").strip()
    return default if not value else int(value)


def restart_launch_agent() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCH_LABEL}"],
        check=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Настроить cookie и колонки таблицы рейтинга РЭУ."
    )
    parser.add_argument("--html-file", type=Path, help="HTML страницы рейтинга после входа.")
    parser.add_argument("--cookie", help="Cookie header из авторизованного запроса.")
    parser.add_argument("--yes", action="store_true", help="Принять предложенные индексы.")
    parser.add_argument("--restart", action="store_true", help="Перезапустить LaunchAgent бота.")
    args = parser.parse_args()

    env = dotenv_values(PROJECT_ENV)
    url = env.get("REA_RATING_URL") or DEFAULT_URL

    cookie_header = args.cookie or env.get("REA_COOKIE_HEADER") or ""
    if args.html_file:
        html = args.html_file.read_text(encoding="utf-8")
    else:
        if not cookie_header:
            cookie_header = getpass("Вставьте Cookie из авторизованного запроса: ").strip()
        html = fetch_html(url, cookie_header)

    tables = table_rows(html)
    if not tables:
        lower_html = html.casefold()
        if "login" in lower_html or "парол" in lower_html or "авториз" in lower_html:
            raise SystemExit("Таблицы не найдены. Похоже, cookie не авторизована.")
        raise SystemExit("Таблицы не найдены. Возможно, данные грузятся JavaScript.")

    guess = guess_table(tables)
    if guess is None:
        raise SystemExit("Не удалось определить таблицу рейтинга.")

    print_guess(guess)

    if args.yes:
        table_index = guess.table_index
        subject_column_index = guess.subject_column_index
        score_column_index = guess.score_column_index
    else:
        print()
        table_index = choose_int("RATING_TABLE_INDEX", guess.table_index)
        subject_column_index = choose_int("SUBJECT_COLUMN_INDEX", guess.subject_column_index)
        score_column_index = choose_int("SCORE_COLUMN_INDEX", guess.score_column_index)

    updates = {
        "REA_RATING_URL": quote_env(url),
        "REA_COOKIE_HEADER": quote_env(cookie_header),
        "RATING_TABLE_SELECTOR": "",
        "RATING_TABLE_INDEX": str(table_index),
        "SUBJECT_COLUMN_INDEX": str(subject_column_index),
        "SCORE_COLUMN_INDEX": str(score_column_index),
    }

    update_env_file(PROJECT_ENV, updates)
    if RUNTIME_ENV.exists():
        update_env_file(RUNTIME_ENV, updates)

    if args.restart:
        restart_launch_agent()

    print()
    print("Готово: .env обновлен. Cookie не выводилась в лог.")


if __name__ == "__main__":
    main()
