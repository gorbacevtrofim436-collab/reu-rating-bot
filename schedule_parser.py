from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


class ScheduleParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduleLesson:
    pair: str
    start_time: str | None
    end_time: str | None
    subject: str
    lesson_type: str | None = None
    place: str | None = None


@dataclass(frozen=True)
class ScheduleDay:
    title: str
    weekday: str
    date: str | None
    lessons: list[ScheduleLesson]


@dataclass(frozen=True)
class ScheduleWeek:
    week_num: int | None
    days: list[ScheduleDay]


DAY_ORDER = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
}


def parse_schedule_html(html: str) -> ScheduleWeek:
    soup = BeautifulSoup(html, "html.parser")
    week_num = _parse_week_num(soup)
    days: list[ScheduleDay] = []

    for table in soup.select("table"):
        header = table.select_one(".dayh h5") or table.select_one(".dayh")
        if header is None:
            continue

        title = _clean_text(header.get_text(" ", strip=True))
        weekday, date_value = _split_day_title(title)
        lessons: list[ScheduleLesson] = []

        for row in table.select("tr.slot"):
            row_classes = set(row.get("class") or [])
            if "load-empty" in row_classes:
                continue
            if "Занятия отсутствуют" in row.get_text(" ", strip=True):
                continue

            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue

            pair, start_time, end_time = _parse_time_cell(cells[0])
            task_nodes = cells[1].select("a.task")
            if not task_nodes:
                fallback_lesson = _parse_task_text(cells[1].get_text("\n", strip=True))
                if fallback_lesson is not None:
                    lessons.append(
                        ScheduleLesson(
                            pair=pair,
                            start_time=start_time,
                            end_time=end_time,
                            subject=fallback_lesson[0],
                            lesson_type=fallback_lesson[1],
                            place=fallback_lesson[2],
                        )
                    )
                continue

            for task_node in task_nodes:
                parsed = _parse_task_text(task_node.get_text("\n", strip=True))
                if parsed is None:
                    continue
                subject, lesson_type, place = parsed
                lessons.append(
                    ScheduleLesson(
                        pair=pair,
                        start_time=start_time,
                        end_time=end_time,
                        subject=subject,
                        lesson_type=lesson_type,
                        place=place,
                    )
                )

        days.append(ScheduleDay(title=title, weekday=weekday, date=date_value, lessons=lessons))

    if not days:
        raise ScheduleParseError("Не удалось разобрать расписание из ответа сайта.")

    return ScheduleWeek(week_num=week_num, days=days)


def extract_group_candidates_from_rating_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    candidates: list[str] = []

    for line in text.splitlines():
        normalized_line = _clean_text(line)
        if "группа" not in normalized_line.casefold():
            continue
        candidates.extend(_extract_group_like_values(normalized_line))

    candidates.extend(_extract_group_like_values(text))
    return _unique_preserving_order(candidates)[:10]


def find_schedule_day(week: ScheduleWeek, day_query: str) -> ScheduleDay | None:
    normalized_query = _normalize_day(day_query)
    for day in week.days:
        if _normalize_day(day.weekday) == normalized_query:
            return day
    return None


def format_schedule_day(day: ScheduleDay, *, group_name: str | None = None, week_num: int | None = None) -> str:
    header_parts = [day.title.capitalize()]
    if group_name:
        header_parts.append(group_name)
    if week_num is not None:
        header_parts.append(f"{week_num} неделя")

    lines = [" | ".join(header_parts)]
    if not day.lessons:
        lines.append("Занятий нет.")
        return "\n".join(lines)

    for lesson in day.lessons:
        lines.append("")
        time_label = _format_lesson_time(lesson)
        lines.append(time_label)
        lines.append(lesson.subject)
        if lesson.lesson_type:
            lines.append(f"Тип: {lesson.lesson_type}")
        if lesson.place:
            lines.append(f"Место: {lesson.place}")
    return "\n".join(lines)


def format_schedule_week(week: ScheduleWeek, *, group_name: str | None = None) -> str:
    title = "Расписание на текущую неделю"
    if group_name:
        title += f" | {group_name}"
    if week.week_num is not None:
        title += f" | {week.week_num} неделя"

    parts = [title]
    for day in week.days:
        parts.append(format_schedule_day(day))
    return "\n\n".join(parts)


def sort_suggested_days(days: list[ScheduleDay]) -> list[ScheduleDay]:
    return sorted(days, key=lambda day: DAY_ORDER.get(_normalize_day(day.weekday), 99))


def _parse_week_num(soup: BeautifulSoup) -> int | None:
    week_input = soup.select_one("#weekNum")
    if week_input is None:
        return None
    value = str(week_input.get("value") or "").strip()
    return int(value) if value.isdigit() else None


def _split_day_title(title: str) -> tuple[str, str | None]:
    if "," not in title:
        return title, None
    weekday, date_value = title.split(",", 1)
    return _clean_text(weekday), _clean_text(date_value) or None


def _parse_time_cell(cell) -> tuple[str, str | None, str | None]:
    lines = [_clean_text(line) for line in cell.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    pair = lines[0] if lines else "Пара"
    start_time = lines[1] if len(lines) > 1 else None
    end_time = lines[2] if len(lines) > 2 else None
    return pair, start_time, end_time


def _parse_task_text(raw_text: str) -> tuple[str, str | None, str | None] | None:
    lines = [_clean_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None

    subject = lines[0]
    lesson_type = lines[1] if len(lines) > 1 else None
    place = _clean_place(" ".join(lines[2:])) if len(lines) > 2 else None
    return subject, lesson_type, place


def _format_lesson_time(lesson: ScheduleLesson) -> str:
    if lesson.start_time and lesson.end_time:
        return f"{lesson.pair}, {lesson.start_time}-{lesson.end_time}"
    return lesson.pair


def _extract_group_like_values(text: str) -> list[str]:
    patterns = (
        r"\b\d{2}\.\d{2}[A-Za-zА-Яа-яЁё0-9./-]*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9./-]*\b",
        r"\b[А-ЯЁA-Z]{1,8}[- ]?\d{1,3}[А-ЯЁA-Zа-яёa-z0-9./-]*\b",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(re.findall(pattern, text))
    return [_clean_text(value) for value in values if _clean_text(value)]


def _unique_preserving_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _normalize_day(value: str) -> str:
    return _clean_text(value).casefold()


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _clean_place(value: str) -> str | None:
    cleaned = _clean_text(value)
    cleaned = cleaned.replace(" ,", ",").replace("- ,", "-")
    return cleaned or None
