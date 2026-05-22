from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import random
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from rating_client import RatingClient, RatingFetchError
from rating_parser import RatingItem, RatingParseError, find_subject_score, parse_rating_html
from schedule_client import ScheduleClient, ScheduleFetchError, ScheduleSuggestion
from schedule_parser import (
    ScheduleParseError,
    extract_group_candidates_from_rating_html,
    find_schedule_day,
    format_schedule_day,
    format_schedule_week,
    parse_schedule_html,
)
from user_store import RatingSnapshot, StoredCredentials, UserStore, UserStoreError


router = Router()


class RatingStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_subject = State()
    waiting_for_schedule_group = State()
    waiting_for_schedule_group_choice = State()
    waiting_for_schedule_day = State()


store: UserStore | None = None

RATING_FIELDS = (
    ("attendance", "Работа на занятиях"),
    ("control", "Текущий и рубежный контроль"),
    ("creative", "Творческий рейтинг"),
    ("intermediate", "Промежуточная аттестация"),
    ("total", "Итого"),
)

SCHEDULE_TRIGGER_TEXTS = {"расписание", "расписание пар", "schedule"}
SCHEDULE_DAY_BUTTONS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Полное расписание",
)
SCHEDULE_FULL_TEXT = "полное расписание"


def _allowed_user_id() -> int | None:
    value = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    return int(value) if value else None


def _env_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RatingParseError(f"{name} должен быть целым числом") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


async def _is_allowed(message: Message) -> bool:
    allowed_user_id = _allowed_user_id()
    if allowed_user_id is None:
        return True
    return bool(message.from_user and message.from_user.id == allowed_user_id)


def _store() -> UserStore:
    if store is None:
        raise UserStoreError("Хранилище пользователей не инициализировано.")
    return store


def _telegram_user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _store_backend_label() -> str:
    current_store = _store()
    if current_store.use_postgres:
        host = urlparse(current_store.database_url).hostname or "unknown"
        return f"postgres:{host}"
    return f"sqlite:{current_store.db_path}"


def _normalize_message_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _is_schedule_request(value: str | None) -> bool:
    return _normalize_message_text(value).lstrip("/") in SCHEDULE_TRIGGER_TEXTS


def _is_full_schedule_request(value: str | None) -> bool:
    return _normalize_message_text(value) == SCHEDULE_FULL_TEXT


def _schedule_day_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Понедельник"), KeyboardButton(text="Вторник")],
            [KeyboardButton(text="Среда"), KeyboardButton(text="Четверг")],
            [KeyboardButton(text="Пятница"), KeyboardButton(text="Суббота")],
            [KeyboardButton(text="Полное расписание")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _schedule_group_keyboard(suggestions: list[ScheduleSuggestion]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=suggestion.name)] for suggestion in suggestions],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _is_known_schedule_day(value: str | None) -> bool:
    normalized = _normalize_message_text(value)
    return normalized in {_normalize_message_text(day) for day in SCHEDULE_DAY_BUTTONS}


def _looks_like_group_suggestion(suggestion: ScheduleSuggestion) -> bool:
    haystack = f"{suggestion.name} {suggestion.key} {suggestion.metadata or ''}".casefold()
    return any(marker in haystack for marker in ("курс", "бакалавр", "магистр", "специалист", "факультет"))


async def _delete_sensitive_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logging.info("Could not delete sensitive message from user_id=%s", _telegram_user_id(message))


async def _log_event(
    message: Message,
    event_type: str,
    *,
    message_text: str | None = None,
    subject_query: str | None = None,
    subject_matched: str | None = None,
    result_status: str | None = None,
    response_text: str | None = None,
    error_message: str | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            _store().log_event,
            telegram_user_id=_telegram_user_id(message),
            event_type=event_type,
            message_text=message_text,
            subject_query=subject_query,
            subject_matched=subject_matched,
            result_status=result_status,
            response_text=response_text,
            error_message=error_message,
        )
    except Exception:
        logging.exception("Could not write bot event for user_id=%s", _telegram_user_id(message))


async def _log_store_event(
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
    try:
        await asyncio.to_thread(
            _store().log_event,
            telegram_user_id=telegram_user_id,
            event_type=event_type,
            message_text=message_text,
            subject_query=subject_query,
            subject_matched=subject_matched,
            result_status=result_status,
            response_text=response_text,
            error_message=error_message,
        )
    except Exception:
        logging.exception("Could not write bot event for user_id=%s", telegram_user_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    logging.info("Received /start from user_id=%s", message.from_user.id if message.from_user else None)
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "command", message_text="/start", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "command", message_text="/start", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    await state.clear()
    credentials = _store().get_credentials(user_id)
    logging.info(
        "Start credential check user_id=%s credentials_present=%s store=%s",
        user_id,
        credentials is not None,
        _store_backend_label(),
    )
    if credentials is not None:
        await state.set_state(RatingStates.waiting_for_subject)
        response = "Баллы по какому предмету вас интересуют?"
        await _log_event(message, "command", message_text="/start", result_status="credentials_found", response_text=response)
        await message.answer(response)
        return

    await state.set_state(RatingStates.waiting_for_login)
    response = (
        "Введите логин от личного кабинета РЭУ.\n"
        "Команды: /login — сменить данные, /logout — удалить данные."
    )
    await _log_event(message, "command", message_text="/start", result_status="login_required", response_text=response)
    await message.answer(response)


@router.message(Command("login"))
async def login(message: Message, state: FSMContext) -> None:
    logging.info("Received /login from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "command", message_text="/login", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    await state.clear()
    await state.set_state(RatingStates.waiting_for_login)
    response = "Введите логин от личного кабинета РЭУ."
    await _log_event(message, "command", message_text="/login", result_status="login_required", response_text=response)
    await message.answer(response)


@router.message(Command("logout"))
async def logout(message: Message, state: FSMContext) -> None:
    logging.info("Received /logout from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "command", message_text="/logout", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is not None:
        _store().delete_credentials(user_id)
    await state.clear()
    response = "Данные входа удалены. Для повторной настройки отправьте /start."
    await _log_event(message, "command", message_text="/logout", result_status="credentials_deleted", response_text=response)
    await message.answer(response, reply_markup=ReplyKeyboardRemove())


@router.message(Command("schedule"))
async def schedule_command(message: Message, state: FSMContext) -> None:
    logging.info("Received /schedule from user_id=%s", _telegram_user_id(message))
    await start_schedule_flow(message, state)


@router.message(Command("schedule_reset"))
async def schedule_reset(message: Message, state: FSMContext) -> None:
    logging.info("Received /schedule_reset from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "schedule_reset", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "schedule_reset", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return
    if _store().get_credentials(user_id) is None:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_reset", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return

    _store().delete_schedule_preference(user_id)
    await state.set_state(RatingStates.waiting_for_schedule_group)
    response = "Группа для расписания сброшена. Введите номер своей группы."
    await _log_event(message, "schedule_reset", result_status="deleted", response_text=response)
    await message.answer(response, reply_markup=ReplyKeyboardRemove())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    logging.info("Received /cancel from user_id=%s", _telegram_user_id(message))
    await state.clear()
    response = "Действие отменено."
    await _log_event(message, "command", message_text="/cancel", result_status="cancelled", response_text=response)
    await message.answer(response, reply_markup=ReplyKeyboardRemove())


@router.message(RatingStates.waiting_for_login, F.text)
async def handle_login(message: Message, state: FSMContext) -> None:
    logging.info("Received REA login from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "rea_login", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    login_value = message.text.strip()
    if not login_value or login_value.startswith("/"):
        response = "Введите логин от личного кабинета РЭУ."
        await _log_event(message, "rea_login", message_text=login_value, result_status="invalid", response_text=response)
        await message.answer(response)
        return

    await state.update_data(rea_login=login_value)
    await state.set_state(RatingStates.waiting_for_password)
    response = "Теперь введите пароль от личного кабинета РЭУ."
    await _log_event(message, "rea_login", message_text=login_value, result_status="accepted", response_text=response)
    await message.answer(response)


@router.message(RatingStates.waiting_for_password, F.text)
async def handle_password(message: Message, state: FSMContext) -> None:
    logging.info("Received REA password from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "rea_password", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "rea_password", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    data = await state.get_data()
    login_value = str(data.get("rea_login", "")).strip()
    password_value = message.text.strip()
    await _delete_sensitive_message(message)

    if not login_value:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Логин не найден. Введите логин заново."
        await _log_event(message, "rea_password", result_status="missing_login", response_text=response)
        await message.answer(response)
        return
    if not password_value or password_value.startswith("/"):
        response = "Введите пароль от личного кабинета РЭУ."
        await _log_event(message, "rea_password", result_status="invalid_password_message", response_text=response)
        await message.answer(response)
        return

    await _log_event(message, "rea_password", result_status="check_started", response_text="Проверяю вход в личный кабинет...")
    await message.answer("Проверяю вход в личный кабинет...")

    try:
        html = await asyncio.to_thread(
            RatingClient(login=login_value, password=password_value).fetch_html
        )
        items = parse_rating_html(
            html,
            table_selector=os.getenv("RATING_TABLE_SELECTOR") or None,
            table_index=_env_int("RATING_TABLE_INDEX"),
            subject_column_index=_env_int("SUBJECT_COLUMN_INDEX"),
            score_column_index=_env_int("SCORE_COLUMN_INDEX"),
        )
    except (RatingFetchError, RatingParseError) as exc:
        response = f"Не удалось войти или получить рейтинг: {exc}"
        await _log_event(message, "rea_login_result", result_status="failed", response_text=response, error_message=str(exc))
        await message.answer(response)
        return

    _store().save_credentials(
        telegram_user_id=user_id,
        rea_login=login_value,
        rea_password=password_value,
    )
    await asyncio.to_thread(
        _store().replace_rating_snapshots,
        telegram_user_id=user_id,
        snapshots=rating_items_to_snapshots(items),
    )
    await state.set_state(RatingStates.waiting_for_subject)
    response = "Готово. Теперь напишите предмет, например: матан, англ, алгоритмы."
    await _log_event(message, "rea_login_result", message_text=login_value, result_status="success", response_text=response)
    await message.answer(response)


@router.message(RatingStates.waiting_for_schedule_group, F.text)
async def handle_schedule_group(message: Message, state: FSMContext) -> None:
    logging.info("Received schedule group query from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "schedule_group", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "schedule_group", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return
    if _store().get_credentials(user_id) is None:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_group", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return

    query = message.text.strip()
    if not query or query.startswith("/"):
        response = "Введите номер своей группы."
        await _log_event(message, "schedule_group", message_text=query, result_status="invalid", response_text=response)
        await message.answer(response)
        return

    try:
        suggestions = await asyncio.to_thread(ScheduleClient().search_suggestions, query)
    except ScheduleFetchError as exc:
        response = f"Не удалось найти группу: {exc}"
        await _log_event(
            message,
            "schedule_group",
            message_text=query,
            result_status="fetch_failed",
            response_text=response,
            error_message=str(exc),
        )
        await message.answer(response)
        return

    group_suggestions = [suggestion for suggestion in suggestions if _looks_like_group_suggestion(suggestion)]
    if not group_suggestions:
        response = (
            "Группа не найдена. Введите номер группы точно как на сайте расписания, "
            "например 15.07В-ЭФ1/25б."
        )
        await _log_event(message, "schedule_group", message_text=query, result_status="not_found", response_text=response)
        await message.answer(response)
        return

    selected = _pick_schedule_suggestion(query, group_suggestions)
    if selected is not None:
        await save_schedule_preference_from_suggestion(message, selected)
        await ask_schedule_day(message, state, prefix=f"Группа сохранена: {selected.name}.")
        return

    limited_suggestions = group_suggestions[:8]
    await state.update_data(
        schedule_suggestions=[
            {
                "name": suggestion.name,
                "key": suggestion.key,
                "metadata": suggestion.metadata,
            }
            for suggestion in limited_suggestions
        ]
    )
    await state.set_state(RatingStates.waiting_for_schedule_group_choice)
    response = "Нашел несколько вариантов. Выберите свою группу:"
    await _log_event(
        message,
        "schedule_group",
        message_text=query,
        result_status="multiple_found",
        response_text=response,
    )
    await message.answer(response, reply_markup=_schedule_group_keyboard(limited_suggestions))


@router.message(RatingStates.waiting_for_schedule_group_choice, F.text)
async def handle_schedule_group_choice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw_suggestions = data.get("schedule_suggestions") or []
    suggestions = [
        ScheduleSuggestion(
            name=str(item.get("name") or ""),
            key=str(item.get("key") or ""),
            metadata=str(item.get("metadata") or "") or None,
        )
        for item in raw_suggestions
        if isinstance(item, dict)
    ]
    selected = _pick_schedule_suggestion(message.text.strip(), suggestions)
    if selected is None:
        await state.set_state(RatingStates.waiting_for_schedule_group)
        await handle_schedule_group(message, state)
        return

    await save_schedule_preference_from_suggestion(message, selected)
    await ask_schedule_day(message, state, prefix=f"Группа сохранена: {selected.name}.")


@router.message(RatingStates.waiting_for_schedule_day, F.text)
async def handle_schedule_day(message: Message, state: FSMContext) -> None:
    logging.info("Received schedule day query from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "schedule_day", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    day_query = message.text.strip()
    if not _is_known_schedule_day(day_query):
        response = "Выберите день текущей недели кнопкой ниже."
        await _log_event(
            message,
            "schedule_day",
            message_text=day_query,
            result_status="invalid_day",
            response_text=response,
        )
        await message.answer(response, reply_markup=_schedule_day_keyboard())
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "schedule_day", result_status="no_telegram_user", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return
    if _store().get_credentials(user_id) is None:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_day", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return

    preference = _store().get_schedule_preference(user_id)
    if preference is None:
        await state.set_state(RatingStates.waiting_for_schedule_group)
        response = "Сначала нужно указать группу. Введите номер своей группы."
        await _log_event(message, "schedule_day", result_status="no_group", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return

    try:
        html = await asyncio.to_thread(ScheduleClient().fetch_week_html, preference.schedule_key)
        week = parse_schedule_html(html)
    except (ScheduleFetchError, ScheduleParseError) as exc:
        response = f"Не удалось получить расписание: {exc}"
        await _log_event(
            message,
            "schedule_day",
            message_text=day_query,
            result_status="fetch_failed",
            response_text=response,
            error_message=str(exc),
        )
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        await state.set_state(RatingStates.waiting_for_subject)
        return

    if _is_full_schedule_request(day_query):
        response = format_schedule_week(week, group_name=preference.schedule_name)
    else:
        day = find_schedule_day(week, day_query)
        if day is None:
            response = "На сайте расписания нет выбранного дня для текущей недели."
        else:
            response = format_schedule_day(
                day,
                group_name=preference.schedule_name,
                week_num=week.week_num,
            )

    await _log_event(
        message,
        "schedule_day",
        message_text=day_query,
        result_status="found",
        response_text=response,
    )
    for index, response_part in enumerate(split_telegram_text(response)):
        await message.answer(
            response_part,
            reply_markup=ReplyKeyboardRemove() if index == 0 else None,
        )
    await state.set_state(RatingStates.waiting_for_subject)


@router.message(RatingStates.waiting_for_subject, F.text)
async def handle_subject(message: Message, state: FSMContext) -> None:
    logging.info("Received subject query from user_id=%s", message.from_user.id if message.from_user else None)
    if _is_schedule_request(message.text):
        await start_schedule_flow(message, state)
        return
    await answer_subject(message)


@router.message(F.text)
async def handle_subject_without_state(message: Message, state: FSMContext) -> None:
    logging.info("Received stateless subject query from user_id=%s", message.from_user.id if message.from_user else None)
    if message.text and message.text.startswith("/"):
        return
    if _is_schedule_request(message.text):
        await start_schedule_flow(message, state)
        return
    await answer_subject(message)


async def start_schedule_flow(message: Message, state: FSMContext) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "schedule_start", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "schedule_start", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    credentials = _store().get_credentials(user_id)
    if credentials is None:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_start", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=ReplyKeyboardRemove())
        return

    preference = _store().get_schedule_preference(user_id)
    if preference is not None:
        await ask_schedule_day(message, state)
        return

    detected_suggestion = await detect_schedule_group_from_rating(message, credentials)
    if detected_suggestion is not None:
        await save_schedule_preference_from_suggestion(message, detected_suggestion)
        await ask_schedule_day(message, state, prefix=f"Нашел вашу группу: {detected_suggestion.name}.")
        return

    await state.set_state(RatingStates.waiting_for_schedule_group)
    response = (
        "Не смог автоматически определить группу из рейтинга. "
        "Введите номер своей группы, чтобы я сохранил ее для расписания."
    )
    await _log_event(message, "schedule_start", result_status="group_required", response_text=response)
    await message.answer(response, reply_markup=ReplyKeyboardRemove())


async def detect_schedule_group_from_rating(
    message: Message,
    credentials,
) -> ScheduleSuggestion | None:
    try:
        html = await asyncio.to_thread(
            RatingClient(login=credentials.login, password=credentials.password).fetch_html
        )
    except RatingFetchError as exc:
        await _log_event(
            message,
            "schedule_group_autodetect",
            result_status="rating_fetch_failed",
            error_message=str(exc),
        )
        return None

    candidates = extract_group_candidates_from_rating_html(html)
    if not candidates:
        await _log_event(message, "schedule_group_autodetect", result_status="no_candidates")
        return None

    for candidate in candidates:
        try:
            suggestions = await asyncio.to_thread(ScheduleClient().search_suggestions, candidate)
        except ScheduleFetchError:
            continue
        group_suggestions = [suggestion for suggestion in suggestions if _looks_like_group_suggestion(suggestion)]
        selected = _pick_schedule_suggestion(candidate, group_suggestions)
        if selected is not None:
            await _log_event(
                message,
                "schedule_group_autodetect",
                subject_query=candidate,
                subject_matched=selected.name,
                result_status="found",
            )
            return selected

    await _log_event(
        message,
        "schedule_group_autodetect",
        result_status="not_found",
        response_text=", ".join(candidates),
    )
    return None


async def ask_schedule_day(message: Message, state: FSMContext, *, prefix: str | None = None) -> None:
    await state.set_state(RatingStates.waiting_for_schedule_day)
    response = "Какой день текущей недели вас интересует?"
    if prefix:
        response = f"{prefix}\n{response}"
    await _log_event(message, "schedule_start", result_status="day_requested", response_text=response)
    await message.answer(response, reply_markup=_schedule_day_keyboard())


async def save_schedule_preference_from_suggestion(
    message: Message,
    suggestion: ScheduleSuggestion,
) -> None:
    user_id = _telegram_user_id(message)
    if user_id is None:
        return
    await asyncio.to_thread(
        _store().save_schedule_preference,
        telegram_user_id=user_id,
        schedule_key=suggestion.key,
        schedule_name=suggestion.name,
        schedule_metadata=suggestion.metadata,
    )
    await _log_event(
        message,
        "schedule_group_saved",
        subject_matched=suggestion.name,
        result_status="saved",
    )


def _pick_schedule_suggestion(
    query: str,
    suggestions: list[ScheduleSuggestion],
) -> ScheduleSuggestion | None:
    if not suggestions:
        return None

    normalized_query = _normalize_schedule_key(query)
    exact_matches = [
        suggestion
        for suggestion in suggestions
        if _normalize_schedule_key(suggestion.name) == normalized_query
        or _normalize_schedule_key(suggestion.key) == normalized_query
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    if len(suggestions) == 1:
        return suggestions[0]

    return None


def _normalize_schedule_key(value: str) -> str:
    return _normalize_message_text(value).replace(" ", "")


async def answer_subject(message: Message) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "subject_query", message_text=message.text, result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    subject_query = message.text.strip()
    if not subject_query:
        response = "Напишите название предмета."
        await _log_event(message, "subject_query", message_text=message.text, result_status="empty_query", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "subject_query", message_text=subject_query, subject_query=subject_query, result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    credentials = _store().get_credentials(user_id)
    if credentials is None:
        response = "Сначала нужно войти в личный кабинет. Отправьте /start."
        await _log_event(message, "subject_query", message_text=subject_query, subject_query=subject_query, result_status="no_credentials", response_text=response)
        await message.answer(response)
        return

    client = RatingClient(login=credentials.login, password=credentials.password)

    try:
        html = await asyncio.to_thread(client.fetch_html)
        items = parse_rating_html(
            html,
            table_selector=os.getenv("RATING_TABLE_SELECTOR") or None,
            table_index=_env_int("RATING_TABLE_INDEX"),
            subject_column_index=_env_int("SUBJECT_COLUMN_INDEX"),
            score_column_index=_env_int("SCORE_COLUMN_INDEX"),
        )
    except (RatingFetchError, RatingParseError) as exc:
        response = f"Не удалось получить баллы: {exc}"
        await _log_event(
            message,
            "subject_query",
            message_text=subject_query,
            subject_query=subject_query,
            result_status="fetch_failed",
            response_text=response,
            error_message=str(exc),
        )
        await message.answer(response)
        return

    item = find_subject_score(items, subject_query)
    if item is None:
        available = ", ".join(sorted({entry.subject for entry in items})[:10])
        suffix = f"\nДоступные предметы: {available}" if available else ""
        response = f"Предмет не найден.{suffix}"
        await _log_event(
            message,
            "subject_query",
            message_text=subject_query,
            subject_query=subject_query,
            result_status="not_found",
            response_text=response,
        )
        await message.answer(response)
        return

    response = format_rating_item(item)
    await _log_event(
        message,
        "subject_query",
        message_text=subject_query,
        subject_query=subject_query,
        subject_matched=item.subject,
        result_status="found",
        response_text=response,
    )
    await message.answer(response)


def format_rating_item(item: RatingItem) -> str:
    if any([item.attendance, item.control, item.creative, item.intermediate]):
        return (
            f"{item.subject}\n"
            f"Работа на занятиях: {item.attendance or '-'}\n"
            f"Текущий и рубежный контроль: {item.control or '-'}\n"
            f"Творческий рейтинг: {item.creative or '-'}\n"
            f"Промежуточная аттестация: {item.intermediate or '-'}\n"
            f"Итого: {item.total}"
        )

    return f"{item.subject}: {item.total}"


def rating_items_to_snapshots(items: list[RatingItem]) -> list[RatingSnapshot]:
    return [
        RatingSnapshot(
            subject=item.subject,
            total=_clean_rating_value(item.total),
            attendance=_clean_optional_rating_value(item.attendance),
            control=_clean_optional_rating_value(item.control),
            creative=_clean_optional_rating_value(item.creative),
            intermediate=_clean_optional_rating_value(item.intermediate),
        )
        for item in items
    ]


def _clean_rating_value(value: str | None) -> str:
    return " ".join((value or "").split())


def _clean_optional_rating_value(value: str | None) -> str | None:
    cleaned = _clean_rating_value(value)
    return cleaned or None


def collect_rating_changes(
    previous: dict[str, RatingSnapshot],
    current_items: list[RatingItem],
) -> dict[str, list[tuple[str, str, str]]]:
    changes: dict[str, list[tuple[str, str, str]]] = {}
    for item in current_items:
        old_snapshot = previous.get(item.subject)
        if old_snapshot is None:
            continue

        for field_name, label in RATING_FIELDS:
            old_value = _clean_rating_value(getattr(old_snapshot, field_name))
            new_value = _clean_rating_value(getattr(item, field_name))
            if old_value != new_value:
                changes.setdefault(item.subject, []).append(
                    (label, _display_rating_value(old_value), _display_rating_value(new_value))
                )

    return changes


def _display_rating_value(value: str) -> str:
    return value or "-"


def format_rating_change_notification(changes: dict[str, list[tuple[str, str, str]]]) -> str:
    lines = ["Изменились баллы:"]
    for subject, subject_changes in changes.items():
        lines.append("")
        lines.append(subject)
        for label, old_value, new_value in subject_changes:
            lines.append(f"{label}: было {old_value}, стало {new_value}")
    return "\n".join(lines)


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block

    if current:
        chunks.append(current)
    return chunks


async def rating_monitor_loop(app: web.Application) -> None:
    if not _env_bool("MONITOR_ENABLED", True):
        logging.info("Rating monitor is disabled")
        return

    if not _env_bool("MONITOR_BACKGROUND_ENABLED", False):
        logging.info("Background rating monitor is disabled")
        return

    initial_delay = max(_env_float("MONITOR_INITIAL_DELAY_SECONDS", 60.0), 0.0)
    interval = max(_env_float("MONITOR_INTERVAL_SECONDS", 3600.0), 60.0)
    jitter = max(_env_float("MONITOR_JITTER_SECONDS", 60.0), 0.0)

    logging.info(
        "Rating monitor started: initial_delay=%s interval=%s jitter=%s",
        initial_delay,
        interval,
        jitter,
    )
    await asyncio.sleep(initial_delay)

    while True:
        try:
            await run_locked_rating_monitor_cycle(app, source="background")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Rating monitor cycle failed")

        sleep_for = interval + (random.uniform(0, jitter) if jitter else 0)
        await asyncio.sleep(sleep_for)


async def run_locked_rating_monitor_cycle(app: web.Application, *, source: str) -> bool:
    lock = app["rating_monitor_lock"]
    if lock.locked():
        await _log_store_event(
            telegram_user_id=None,
            event_type="rating_monitor",
            result_status="already_running",
            response_text=f"source={source}",
        )
        return False

    async with lock:
        await run_rating_monitor_cycle(app["bot"], source=source)
        return True


async def run_rating_monitor_cycle(bot: Bot, *, source: str) -> None:
    credentials = await asyncio.to_thread(_store().list_credentials)
    logging.info("Rating monitor cycle started from %s for %s users", source, len(credentials))
    await _log_store_event(
        telegram_user_id=None,
        event_type="rating_monitor",
        result_status="cycle_started",
        response_text=f"source={source} users={len(credentials)}",
    )

    user_delay = max(_env_float("MONITOR_USER_DELAY_SECONDS", 30.0), 0.0)
    for index, credentials_item in enumerate(credentials):
        await check_user_rating_changes(bot, credentials_item)
        if user_delay and index < len(credentials) - 1:
            await asyncio.sleep(user_delay)


async def check_user_rating_changes(bot: Bot, credentials: StoredCredentials) -> None:
    telegram_user_id = credentials.telegram_user_id
    try:
        html = await asyncio.to_thread(
            RatingClient(login=credentials.login, password=credentials.password).fetch_html
        )
        items = parse_rating_html(
            html,
            table_selector=os.getenv("RATING_TABLE_SELECTOR") or None,
            table_index=_env_int("RATING_TABLE_INDEX"),
            subject_column_index=_env_int("SUBJECT_COLUMN_INDEX"),
            score_column_index=_env_int("SCORE_COLUMN_INDEX"),
        )
        previous = await asyncio.to_thread(_store().get_rating_snapshots, telegram_user_id)
        snapshots = rating_items_to_snapshots(items)

        if not previous:
            await asyncio.to_thread(
                _store().replace_rating_snapshots,
                telegram_user_id=telegram_user_id,
                snapshots=snapshots,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="rating_monitor",
                result_status="baseline_saved",
                response_text=f"subjects={len(snapshots)}",
            )
            return

        changes = collect_rating_changes(previous, items)

        if not changes:
            await asyncio.to_thread(
                _store().replace_rating_snapshots,
                telegram_user_id=telegram_user_id,
                snapshots=snapshots,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="rating_monitor",
                result_status="no_changes",
                response_text=f"subjects={len(snapshots)}",
            )
            return

        notification = format_rating_change_notification(changes)
        for notification_part in split_telegram_text(notification):
            await bot.send_message(chat_id=telegram_user_id, text=notification_part)
        await asyncio.to_thread(
            _store().replace_rating_snapshots,
            telegram_user_id=telegram_user_id,
            snapshots=snapshots,
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_change",
            result_status="notified",
            response_text=notification,
        )
    except (RatingFetchError, RatingParseError) as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_monitor",
            result_status="fetch_failed",
            error_message=str(exc),
        )
    except Exception as exc:
        logging.exception("Rating monitor failed for user_id=%s", telegram_user_id)
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_monitor",
            result_status="failed",
            error_message=str(exc),
        )


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    return token


def build_webhook_url() -> str:
    webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL не задан в переменных окружения")
    if webhook_url.endswith("/webhook"):
        return webhook_url
    return f"{webhook_url}/webhook"


def get_webhook_secret() -> str:
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("WEBHOOK_SECRET не задан в переменных окружения")
    return secret


def get_monitor_run_secret() -> str:
    secret = os.getenv("MONITOR_RUN_SECRET", "").strip()
    if not secret:
        raise RuntimeError("MONITOR_RUN_SECRET не задан в переменных окружения")
    return secret


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "reu-rating-bot"})


async def trigger_monitor_run(request: web.Request) -> web.Response:
    if not _env_bool("MONITOR_ENABLED", True):
        return web.json_response({"status": "disabled"}, status=503)

    expected_secret = get_monitor_run_secret()
    received_secret = request.headers.get("X-Monitor-Secret", "")
    if not hmac.compare_digest(received_secret, expected_secret):
        return web.json_response({"error": "unauthorized"}, status=401)

    existing_task = request.app.get("manual_rating_monitor_task")
    if existing_task is not None and not existing_task.done():
        return web.json_response({"status": "already_running"}, status=202)

    task = asyncio.create_task(run_locked_rating_monitor_cycle(request.app, source="http"))
    request.app["manual_rating_monitor_task"] = task
    task.add_done_callback(_log_manual_monitor_task_result)
    return web.json_response({"status": "started"}, status=202)


def _log_manual_monitor_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logging.info("Manual rating monitor task was cancelled")
    except Exception:
        logging.exception("Manual rating monitor task failed")


async def on_webhook_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    webhook_url = build_webhook_url()
    await bot.set_webhook(
        webhook_url,
        secret_token=get_webhook_secret(),
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    logging.info("Webhook set to %s", webhook_url)


async def start_monitor_task(app: web.Application) -> None:
    app["rating_monitor_task"] = asyncio.create_task(rating_monitor_loop(app))


async def stop_monitor_task(app: web.Application) -> None:
    task = app.get("rating_monitor_task")
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def run_webhook() -> None:
    global store
    store = UserStore()
    bot = Bot(token=get_bot_token())
    dispatcher = create_dispatcher()
    dispatcher.startup.register(on_webhook_startup)

    app = web.Application()
    app["bot"] = bot
    app["rating_monitor_lock"] = asyncio.Lock()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/monitor/run", trigger_monitor_run)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=get_webhook_secret(),
    ).register(app, path="/webhook")
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(start_monitor_task)
    app.on_cleanup.append(stop_monitor_task)

    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


async def run_polling() -> None:
    global store
    store = UserStore()
    bot = Bot(token=get_bot_token())
    dispatcher = create_dispatcher()
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    run_mode = os.getenv("RUN_MODE", "webhook").strip().casefold()
    if run_mode == "polling":
        asyncio.run(run_polling())
        return

    run_webhook()


if __name__ == "__main__":
    main()
