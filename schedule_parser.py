from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    teacher: str | None = None


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
    group_name: str | None = None
    updated_at: str | None = None


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
    if soup.select_one("table.table_lessons") is not None:
        return parse_student_lessons_html(soup)
    raise ScheduleParseError("Не найдена таблица расписания ЛКС.")


def parse_student_lessons_html(soup: BeautifulSoup) -> ScheduleWeek:
    table = soup.select_one("table.table_lessons")
    if table is None:
        raise ScheduleParseError("Не найдена таблица расписания ЛКС.")

    group_name, updated_at = _parse_student_heading(soup)
    week_num: int | None = None
    days: list[ScheduleDay] = []
    current_day: ScheduleDay | None = None

    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue

        first_text = _clean_text(cells[0].get_text(" ", strip=True))
        second_text = _clean_text(cells[1].get_text(" ", strip=True))

        if "неделя" in first_text.casefold():
            match = re.search(r"\d+", first_text)
            if match:
                week_num = int(match.group(0))

        if _looks_like_day_title(second_text) and not cells[1].select_one(".lesson__block"):
            weekday, date_value = _split_day_title(second_text)
            current_day = ScheduleDay(
                title=second_text,
                weekday=weekday,
                date=date_value,
                lessons=[],
            )
            days.append(current_day)
            continue

        if current_day is None or not second_text or not cells[1].select_one(".lesson__block"):
            continue

        pair, start_time, end_time = _parse_time_cell(cells[0])
        lesson = _parse_student_lesson_cell(cells[1], pair, start_time, end_time)
        if lesson is not None:
            current_day.lessons.append(lesson)

    if not days:
        raise ScheduleParseError("Не удалось разобрать расписание из ЛКС.")

    return ScheduleWeek(
        week_num=week_num,
        days=sort_suggested_days(days),
        group_name=group_name,
        updated_at=updated_at,
    )


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
        lines.append(_format_lesson_time(lesson))
        lines.append(lesson.subject)
        if lesson.lesson_type:
            lines.append(f"Тип: {lesson.lesson_type}")
        if lesson.place:
            lines.append(f"Место: {lesson.place}")
        if lesson.teacher:
            lines.append(f"Преподаватель: {lesson.teacher}")
    return "\n".join(lines)


def format_schedule_week(week: ScheduleWeek, *, group_name: str | None = None) -> str:
    title = "Расписание на текущую неделю"
    effective_group_name = group_name or week.group_name
    if effective_group_name:
        title += f" | {effective_group_name}"
    if week.week_num is not None:
        title += f" | {week.week_num} неделя"

    parts = [title]
    for day in week.days:
        parts.append(format_schedule_day(day))
    return "\n\n".join(parts)


def schedule_snapshot_text(week: ScheduleWeek) -> str:
    lines: list[str] = []
    for day in week.days:
        for lesson in day.lessons:
            lines.append(format_schedule_snapshot_line(day, lesson))
    return "\n".join(lines)


def schedule_week_key(week: ScheduleWeek) -> str:
    parsed_dates = [_parse_date(day.date) for day in week.days if day.date]
    parsed_dates = [value for value in parsed_dates if value is not None]
    if parsed_dates:
        first_date = min(parsed_dates)
        monday = first_date - timedelta(days=first_date.weekday())
        return f"monday:{monday:%Y-%m-%d}"
    if week.week_num is not None:
        return f"week:{week.week_num}"
    return "|".join(day.title for day in week.days)


def format_schedule_snapshot_line(day: ScheduleDay, lesson: ScheduleLesson) -> str:
    parts = [
        day.title.capitalize(),
        _format_lesson_time(lesson),
        lesson.subject,
        lesson.lesson_type or "-",
        lesson.place or "-",
        lesson.teacher or "-",
    ]
    return " | ".join(_clean_text(part) for part in parts)


def sort_suggested_days(days: list[ScheduleDay]) -> list[ScheduleDay]:
    return sorted(days, key=lambda day: DAY_ORDER.get(_normalize_day(day.weekday), 99))


def _parse_student_heading(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    headings = [_clean_text(heading.get_text(" ", strip=True)) for heading in soup.find_all("h1")]
    text = next((heading for heading in headings if "Расписание для группы" in heading), "")
    if not text:
        return None, None
    group_name = None
    updated_at = None

    group_match = re.search(r"Расписание\s+для\s+группы:\s*(.+?)(?:\s*\(|$)", text, flags=re.IGNORECASE)
    if group_match:
        group_name = _clean_text(group_match.group(1))

    updated_match = re.search(r"обновлено\s+([^)]+)", text, flags=re.IGNORECASE)
    if updated_match:
        updated_at = _clean_text(updated_match.group(1))

    return group_name, updated_at


def _parse_student_lesson_cell(cell, pair: str, start_time: str | None, end_time: str | None) -> ScheduleLesson | None:
    summary = cell.select_one(".lesson__block span")
    if summary is None:
        return None

    summary_lines = _split_lines(summary.get_text("\n", strip=True))
    if not summary_lines:
        return None

    subject = summary_lines[0]
    place = _clean_place(summary_lines[1]) if len(summary_lines) > 1 else None
    lesson_type = summary_lines[2] if len(summary_lines) > 2 else None

    popup = cell.select_one(".lesson_popup")
    teacher = _extract_teacher(popup.get_text("\n", strip=True) if popup is not None else "")
    popup_place = _extract_label_value(popup.get_text("\n", strip=True), "Аудитория") if popup is not None else None

    return ScheduleLesson(
        pair=pair,
        start_time=start_time,
        end_time=end_time,
        subject=subject,
        lesson_type=lesson_type,
        place=popup_place or place,
        teacher=teacher,
    )


def _extract_teacher(text: str) -> str | None:
    teachers: list[str] = []
    for line in _split_lines(text):
        if not line.casefold().startswith("преподаватель:"):
            continue
        value = _clean_text(line.split(":", 1)[1])
        if value and value not in teachers:
            teachers.append(value)
    return ", ".join(teachers) if teachers else None


def _extract_label_value(text: str, label: str) -> str | None:
    label_prefix = f"{label}:".casefold()
    for line in _split_lines(text):
        if line.casefold().startswith(label_prefix):
            value = _clean_text(line.split(":", 1)[1])
            return value or None
    return None


def _split_day_title(title: str) -> tuple[str, str | None]:
    if "," not in title:
        return title, None
    weekday, date_value = title.split(",", 1)
    return _clean_text(weekday), _clean_text(date_value) or None


def _looks_like_day_title(value: str) -> bool:
    if "," not in value:
        return False
    weekday, _ = _split_day_title(value)
    return _normalize_day(weekday) in DAY_ORDER


def _parse_time_cell(cell) -> tuple[str, str | None, str | None]:
    lines = _split_lines(cell.get_text("\n", strip=True))
    pair = lines[0] if lines else "Пара"

    start_time = None
    end_time = None
    if len(lines) > 1:
        time_value = lines[1]
        if "-" in time_value:
            start_time, end_time = [_clean_text(part) for part in time_value.split("-", 1)]
        else:
            start_time = time_value
            end_time = lines[2] if len(lines) > 2 else None
    return pair, start_time, end_time


def _format_lesson_time(lesson: ScheduleLesson) -> str:
    if lesson.start_time and lesson.end_time:
        return f"{lesson.pair}, {lesson.start_time}-{lesson.end_time}"
    return lesson.pair


def _normalize_day(value: str) -> str:
    return _clean_text(value).casefold()


def _split_lines(value: str) -> list[str]:
    return [_clean_text(line) for line in value.splitlines() if _clean_text(line)]


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _clean_place(value: str) -> str | None:
    cleaned = _clean_text(value)
    cleaned = cleaned.replace(" ,", ",").replace("- ,", "-")
    if re.match(r"^\d+\s+корпус\s+", cleaned, flags=re.IGNORECASE):
        cleaned = re.sub(r"^(\d+\s+корпус)\s+", r"\1 - ", cleaned, flags=re.IGNORECASE)
    return cleaned or None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None
