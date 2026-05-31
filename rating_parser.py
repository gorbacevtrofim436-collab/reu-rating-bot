from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from bs4 import BeautifulSoup


class RatingParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class RatingItem:
    subject: str
    total: str
    attendance: str | None = None
    control: str | None = None
    creative: str | None = None
    intermediate: str | None = None

    @property
    def score(self) -> str:
        return self.total


SUBJECT_ALIASES = {
    "алгоритмы": "Алгоритмы и структуры данных",
    "введение": "Введение в профессию",
    "англ": "Иностранный язык",
    "история": "История России",
    "мат логика": "Математическая логика и теория алгоритмов",
    "матан": "Математический анализ",
    "овп": "Основы военной подготовки",
    "физра": "Физическая культура и спорт",
    "философия": "Философия",
    "элективка": "Элективные дисциплины по физической культуре и спорту",
}


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_rating_html(
    html: str,
    *,
    table_selector: str | None,
    table_index: int | None,
    subject_column_index: int | None,
    score_column_index: int | None,
) -> list[RatingItem]:
    component_items = parse_rea_rating_component(html)
    if component_items:
        return component_items

    if subject_column_index is None or score_column_index is None:
        raise RatingParseError(
            "Не заданы SUBJECT_COLUMN_INDEX и SCORE_COLUMN_INDEX. "
            "Нужен HTML страницы после входа или индексы колонок таблицы."
        )

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select(table_selector) if table_selector else soup.find_all("table")

    if not tables:
        raise RatingParseError(
            "Не найдены таблицы с рейтингом. Нужен HTML страницы после входа "
            "или корректный RATING_TABLE_SELECTOR."
        )

    if table_index is not None:
        if table_index < 0 or table_index >= len(tables):
            raise RatingParseError(
                f"RATING_TABLE_INDEX={table_index} вне диапазона. "
                f"Найдено таблиц: {len(tables)}."
            )
        tables = [tables[table_index]]

    max_index = max(subject_column_index, score_column_index)
    items: list[RatingItem] = []

    for table in tables:
        for row in table.find_all("tr"):
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"])
            ]
            if len(cells) <= max_index:
                continue

            subject = cells[subject_column_index].strip()
            score = cells[score_column_index].strip()
            if subject and score:
                items.append(RatingItem(subject=subject, total=score))

    if not items:
        raise RatingParseError(
            "Таблица найдена, но строки с предметами не извлечены. "
            "Проверьте индексы колонок."
        )

    return items


def parse_rea_rating_component(html: str) -> list[RatingItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[RatingItem] = []

    for row in soup.select(".es-rating__line-parent"):
        subject_node = row.select_one(".es-rating__discipline")
        total_node = row.select_one(".es-rating__total")
        if subject_node is None or total_node is None:
            continue

        subject = subject_node.get_text(" ", strip=True)
        total = total_node.get_text(" ", strip=True)
        if subject and total:
            items.append(
                RatingItem(
                    subject=subject,
                    total=total,
                    attendance=get_rating_cell_text(row, ".es-rating__attendance"),
                    control=get_rating_cell_text(row, ".es-rating__control"),
                    creative=get_rating_cell_text(row, ".es-rating__creative"),
                    intermediate=get_rating_cell_text(row, ".es-rating__form"),
                )
            )

    return items


def get_rating_cell_text(row, selector: str) -> str | None:
    node = row.select_one(selector)
    if node is None:
        return None
    value = node.get_text(" ", strip=True)
    return value or None


def find_subject_score(items: Iterable[RatingItem], query: str) -> RatingItem | None:
    normalized_query = normalize_text(query)
    normalized_query = normalize_text(
        SUBJECT_ALIASES.get(normalized_query, normalized_query)
    )
    candidates = list(items)

    for item in candidates:
        if normalize_text(item.subject) == normalized_query:
            return item

    contains_matches = [
        item
        for item in candidates
        if normalized_query in normalize_text(item.subject)
        or normalize_text(item.subject) in normalized_query
    ]
    if len(contains_matches) == 1:
        return contains_matches[0]

    ranked: list[tuple[float, RatingItem]] = []
    for item in candidates:
        ratio = SequenceMatcher(
            None,
            normalized_query,
            normalize_text(item.subject),
        ).ratio()
        ranked.append((ratio, item))

    ranked.sort(key=lambda value: value[0], reverse=True)
    if not ranked or ranked[0][0] < 0.65:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]
