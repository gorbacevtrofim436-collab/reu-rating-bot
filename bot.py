from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import random
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from rating_client import RatingClient, RatingFetchError, create_rea_session
from rating_parser import (
    RatingItem,
    RatingParseError,
    find_subject_score,
    parse_rating_html,
)
from schedule_client import ScheduleClient, ScheduleFetchError
from schedule_parser import (
    ScheduleParseError,
    find_schedule_day,
    format_schedule_day,
    format_schedule_week,
    parse_schedule_html,
    schedule_snapshot_text,
    schedule_week_key,
)
from user_store import RatingSnapshot, StoredCredentials, UserStore, UserStoreError


router = Router()


class RatingStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_action = State()
    waiting_for_subject = State()
    waiting_for_schedule_day = State()
    confirming_delete_data = State()


store: UserStore | None = None

RATING_FIELDS = (
    ("attendance", "Работа на занятиях"),
    ("control", "Текущий и рубежный контроль"),
    ("creative", "Творческий рейтинг"),
    ("intermediate", "Промежуточная аттестация"),
    ("total", "Итого"),
)

SCHEDULE_TRIGGER_TEXTS = {"расписание", "расписание пар", "schedule"}
RATING_TRIGGER_TEXTS = {"баллы", "мои баллы", "рейтинг", "оценки", "rating"}
START_BUTTON_TEXT = "Старт"
RATING_ACTION_TEXT = "Баллы"
SCHEDULE_ACTION_TEXT = "Расписание пар"
BACK_BUTTON_TEXT = "Назад"
DELETE_DATA_TEXT = "Удалить данные"
CONFIRM_DELETE_TEXT = "Да, удалить"
NOTIFICATIONS_ENABLE_TEXT = "Включить уведомления об изменениях"
NOTIFICATIONS_DISABLE_TEXT = "Выключить уведомления об изменениях"
NOTIFICATION_TRIGGER_TEXTS = {
    "уведомления",
    "включить уведомления",
    "выключить уведомления",
    "уведомления: включены",
    "уведомления: выключены",
    NOTIFICATIONS_ENABLE_TEXT.casefold(),
    NOTIFICATIONS_DISABLE_TEXT.casefold(),
}
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
WELCOME_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "reu_bot_avatar.png"
BOT_DESCRIPTION = (
    "РЭУ в кармане: баллы и расписание без лишних заходов в ЛКС. "
    "Выберите предмет или день недели — бот сразу покажет актуальные данные "
    "и предупредит, если изменятся баллы или пары. Один вход — дальше все под рукой."
)
BOT_SHORT_DESCRIPTION = "Баллы, расписание и уведомления РЭУ"


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


def _is_rating_request(value: str | None) -> bool:
    return _normalize_message_text(value).lstrip("/") in RATING_TRIGGER_TEXTS


def _is_start_button(value: str | None) -> bool:
    return _normalize_message_text(value) == _normalize_message_text(START_BUTTON_TEXT)


def _is_back_button(value: str | None) -> bool:
    return _normalize_message_text(value) == _normalize_message_text(BACK_BUTTON_TEXT)


def _is_delete_data_request(value: str | None) -> bool:
    return _normalize_message_text(value) == _normalize_message_text(DELETE_DATA_TEXT)


def _is_confirm_delete_request(value: str | None) -> bool:
    return _normalize_message_text(value) == _normalize_message_text(CONFIRM_DELETE_TEXT)


def _is_notifications_request(value: str | None) -> bool:
    return _normalize_message_text(value) in {_normalize_message_text(text) for text in NOTIFICATION_TRIGGER_TEXTS}


def _is_full_schedule_request(value: str | None) -> bool:
    return _normalize_message_text(value) == SCHEDULE_FULL_TEXT


def _start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=START_BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _action_keyboard(telegram_user_id: int | None = None) -> ReplyKeyboardMarkup:
    notifications_text = NOTIFICATIONS_DISABLE_TEXT
    if telegram_user_id is not None:
        try:
            notifications_text = (
                NOTIFICATIONS_DISABLE_TEXT
                if _store().notifications_enabled(telegram_user_id)
                else NOTIFICATIONS_ENABLE_TEXT
            )
        except Exception:
            logging.exception("Could not read notification settings for user_id=%s", telegram_user_id)

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=RATING_ACTION_TEXT), KeyboardButton(text=SCHEDULE_ACTION_TEXT)],
            [KeyboardButton(text=notifications_text)],
            [KeyboardButton(text=DELETE_DATA_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _subject_keyboard(items: list[RatingItem]) -> ReplyKeyboardMarkup:
    subjects = sorted({item.subject for item in items})
    rows = [[KeyboardButton(text=subject)] for subject in subjects]
    rows.append([KeyboardButton(text=BACK_BUTTON_TEXT)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _schedule_day_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Понедельник"), KeyboardButton(text="Вторник")],
            [KeyboardButton(text="Среда"), KeyboardButton(text="Четверг")],
            [KeyboardButton(text="Пятница"), KeyboardButton(text="Суббота")],
            [KeyboardButton(text="Полное расписание")],
            [KeyboardButton(text=BACK_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _confirm_delete_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CONFIRM_DELETE_TEXT)], [KeyboardButton(text=BACK_BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BACK_BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _is_known_schedule_day(value: str | None) -> bool:
    normalized = _normalize_message_text(value)
    return normalized in {_normalize_message_text(day) for day in SCHEDULE_DAY_BUTTONS}


def _welcome_caption() -> str:
    return (
        "РЭУ в кармане.\n\n"
        "Больше не нужно каждый раз заходить в ЛКС и искать нужную вкладку. "
        "Бот быстро покажет ваши баллы по предметам, расписание пар на текущую неделю "
        "и сам предупредит, если в рейтинге или расписании что-то изменится.\n\n"
        "Нажмите Старт, выберите действие и получите актуальные данные за несколько секунд. "
        "Логин и пароль нужны один раз и хранятся в зашифрованном виде."
    )


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
    if credentials is not None:
        await _log_event(message, "command", message_text="/start", result_status="menu_for_existing_user")
        await show_action_menu(message, state, prefix="С возвращением.")
        return

    await state.set_state(RatingStates.waiting_for_action)
    response = _welcome_caption()
    await _log_event(message, "command", message_text="/start", result_status="welcome_for_new_user", response_text=response)
    if WELCOME_IMAGE_PATH.exists():
        await message.answer_photo(
            FSInputFile(WELCOME_IMAGE_PATH),
            caption=response,
            reply_markup=_start_keyboard(),
        )
        return

    await message.answer(response, reply_markup=_start_keyboard())


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
    await message.answer(response, reply_markup=_back_keyboard())


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
    response = "Данные входа и сохраненные снимки удалены."
    await _log_event(message, "command", message_text="/logout", result_status="credentials_deleted", response_text=response)
    await message.answer(response, reply_markup=_start_keyboard())


@router.message(Command("schedule"))
async def schedule_command(message: Message, state: FSMContext) -> None:
    logging.info("Received /schedule from user_id=%s", _telegram_user_id(message))
    await start_schedule_flow(message, state)


@router.message(Command("rating"))
async def rating_command(message: Message, state: FSMContext) -> None:
    logging.info("Received /rating from user_id=%s", _telegram_user_id(message))
    await start_rating_flow(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    logging.info("Received /cancel from user_id=%s", _telegram_user_id(message))
    await state.clear()
    response = "Действие отменено."
    await _log_event(message, "command", message_text="/cancel", result_status="cancelled", response_text=response)
    await message.answer(response)
    await show_action_menu(message, state)


@router.message(RatingStates.waiting_for_action, F.text)
async def handle_action_choice(message: Message, state: FSMContext) -> None:
    if _is_start_button(message.text):
        await show_action_menu(message, state)
        return
    if _is_delete_data_request(message.text):
        await ask_delete_data_confirmation(message, state)
        return
    if _is_notifications_request(message.text):
        await toggle_notifications(message, state)
        return
    if _is_rating_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(RATING_ACTION_TEXT):
        await start_rating_flow(message, state)
        return
    if _is_schedule_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(SCHEDULE_ACTION_TEXT):
        await start_schedule_flow(message, state)
        return

    response = "Выберите действие кнопкой ниже."
    await _log_event(message, "action_choice", message_text=message.text, result_status="invalid", response_text=response)
    await message.answer(response, reply_markup=_action_keyboard(_telegram_user_id(message)))


@router.message(RatingStates.confirming_delete_data, F.text)
async def handle_delete_data_confirmation(message: Message, state: FSMContext) -> None:
    if _is_back_button(message.text):
        await show_action_menu(message, state)
        return

    if not _is_confirm_delete_request(message.text):
        response = "Подтвердите удаление кнопкой ниже или вернитесь назад."
        await _log_event(message, "delete_data", message_text=message.text, result_status="invalid", response_text=response)
        await message.answer(response, reply_markup=_confirm_delete_keyboard())
        return

    await delete_user_data(message, state)


@router.message(RatingStates.waiting_for_login, F.text)
async def handle_login(message: Message, state: FSMContext) -> None:
    logging.info("Received REA login from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "rea_login", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    login_value = message.text.strip()
    if _is_back_button(login_value):
        await show_action_menu(message, state)
        return

    if not login_value or login_value.startswith("/"):
        response = "Введите логин от личного кабинета РЭУ."
        await _log_event(message, "rea_login", message_text=login_value, result_status="invalid", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return

    await state.update_data(rea_login=login_value)
    await state.set_state(RatingStates.waiting_for_password)
    response = "Теперь введите пароль от личного кабинета РЭУ."
    await _log_event(message, "rea_login", message_text=login_value, result_status="accepted", response_text=response)
    await message.answer(response, reply_markup=_back_keyboard())


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
    if _is_back_button(password_value):
        await state.update_data(rea_login=None, pending_action=None)
        await show_action_menu(message, state)
        return

    if not login_value:
        await state.set_state(RatingStates.waiting_for_login)
        response = "Логин не найден. Введите логин заново."
        await _log_event(message, "rea_password", result_status="missing_login", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return
    if not password_value or password_value.startswith("/"):
        response = "Введите пароль от личного кабинета РЭУ."
        await _log_event(message, "rea_password", result_status="invalid_password_message", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return

    await _log_event(message, "rea_password", result_status="check_started", response_text="Проверяю вход в личный кабинет...")
    await message.answer("Проверяю вход в личный кабинет...")

    try:
        html, schedule_html, schedule_error = await asyncio.to_thread(
            fetch_rating_and_schedule_html_once,
            login=login_value,
            password=password_value,
        )
        items = parse_rating_items_from_html(html)
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
    await save_schedule_baseline_if_available(
        telegram_user_id=user_id,
        login=login_value,
        password=password_value,
        html=schedule_html,
        fetch_error=schedule_error,
    )

    data = await state.get_data()
    pending_action = str(data.get("pending_action") or "").strip()
    await _log_event(message, "rea_login_result", message_text=login_value, result_status="success")

    if pending_action == "schedule":
        await state.update_data(pending_action=None)
        await ask_schedule_day(message, state, prefix="Готово. Данные входа сохранены.")
        return

    if pending_action == "rating":
        await state.update_data(pending_action=None)
        await ask_subject_from_items(message, state, items, prefix="Готово. Данные входа сохранены.")
        return

    await state.update_data(pending_action=None)
    response = "Готово. Данные входа сохранены."
    await message.answer(response)
    await show_action_menu(message, state)


@router.message(RatingStates.waiting_for_schedule_day, F.text)
async def handle_schedule_day(message: Message, state: FSMContext) -> None:
    logging.info("Received schedule day query from user_id=%s", _telegram_user_id(message))
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "schedule_day", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    day_query = message.text.strip()
    if _is_back_button(day_query):
        await show_action_menu(message, state)
        return

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
    credentials = _store().get_credentials(user_id)
    if credentials is None:
        await state.set_state(RatingStates.waiting_for_login)
        await state.update_data(pending_action="schedule")
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_day", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return

    try:
        html = await asyncio.to_thread(
            ScheduleClient(login=credentials.login, password=credentials.password).fetch_week_html
        )
        week = parse_schedule_html(html)
        current_schedule_text = schedule_snapshot_text(week)
        previous_schedule_snapshot = await asyncio.to_thread(_store().get_schedule_snapshot, user_id)
        if previous_schedule_snapshot is None:
            await asyncio.to_thread(
                _store().save_schedule_snapshot,
                telegram_user_id=user_id,
                schedule_key=schedule_week_key(week),
                schedule_hash=hashlib.sha256(current_schedule_text.encode("utf-8")).hexdigest(),
                schedule_text=current_schedule_text,
            )
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
        await message.answer(response, reply_markup=_action_keyboard(user_id))
        await state.set_state(RatingStates.waiting_for_action)
        return

    if _is_full_schedule_request(day_query):
        response = format_schedule_week(week)
    else:
        day = find_schedule_day(week, day_query)
        if day is None:
            response = "На сайте расписания нет выбранного дня для текущей недели."
        else:
            response = format_schedule_day(
                day,
                group_name=week.group_name,
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
            reply_markup=_action_keyboard(user_id) if index == 0 else None,
        )
    await state.set_state(RatingStates.waiting_for_action)


@router.message(RatingStates.waiting_for_subject, F.text)
async def handle_subject(message: Message, state: FSMContext) -> None:
    logging.info("Received subject query from user_id=%s", message.from_user.id if message.from_user else None)
    if _is_start_button(message.text) or _is_back_button(message.text):
        await show_action_menu(message, state)
        return
    if _is_delete_data_request(message.text):
        await ask_delete_data_confirmation(message, state)
        return
    if _is_notifications_request(message.text):
        await toggle_notifications(message, state)
        return
    if _is_rating_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(RATING_ACTION_TEXT):
        await start_rating_flow(message, state)
        return
    if _is_schedule_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(SCHEDULE_ACTION_TEXT):
        await start_schedule_flow(message, state)
        return
    await answer_subject(message)


@router.message(F.text)
async def handle_subject_without_state(message: Message, state: FSMContext) -> None:
    logging.info("Received stateless subject query from user_id=%s", message.from_user.id if message.from_user else None)
    if message.text and message.text.startswith("/"):
        return
    if _is_start_button(message.text) or _is_back_button(message.text):
        await show_action_menu(message, state)
        return
    if _is_delete_data_request(message.text):
        await ask_delete_data_confirmation(message, state)
        return
    if _is_notifications_request(message.text):
        await toggle_notifications(message, state)
        return
    if _is_rating_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(RATING_ACTION_TEXT):
        await start_rating_flow(message, state)
        return
    if _is_schedule_request(message.text) or _normalize_message_text(message.text) == _normalize_message_text(SCHEDULE_ACTION_TEXT):
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
        await state.update_data(pending_action="schedule")
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "schedule_start", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return

    await ask_schedule_day(message, state)


async def ask_schedule_day(message: Message, state: FSMContext, *, prefix: str | None = None) -> None:
    await state.set_state(RatingStates.waiting_for_schedule_day)
    response = "Какой день текущей недели вас интересует?"
    if prefix:
        response = f"{prefix}\n{response}"
    await _log_event(message, "schedule_start", result_status="day_requested", response_text=response)
    await message.answer(response, reply_markup=_schedule_day_keyboard())


async def show_action_menu(message: Message, state: FSMContext, *, prefix: str | None = None) -> None:
    await state.set_state(RatingStates.waiting_for_action)
    response = "Выберите, что хотите посмотреть:"
    if prefix:
        response = f"{prefix}\n{response}"
    await _log_event(message, "action_menu", result_status="shown", response_text=response)
    await message.answer(response, reply_markup=_action_keyboard(_telegram_user_id(message)))


async def ask_delete_data_confirmation(message: Message, state: FSMContext) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "delete_data", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    await state.set_state(RatingStates.confirming_delete_data)
    response = "Удалить сохраненный логин, пароль, снимки баллов и расписания?"
    await _log_event(message, "delete_data", result_status="confirmation_requested", response_text=response)
    await message.answer(response, reply_markup=_confirm_delete_keyboard())


async def delete_user_data(message: Message, state: FSMContext) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "delete_data", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "delete_data", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    await asyncio.to_thread(_store().delete_credentials, user_id)
    await state.clear()
    await state.set_state(RatingStates.waiting_for_action)
    response = "Данные удалены. Чтобы снова пользоваться ботом, нажмите Старт и войдите в ЛКС."
    await _log_event(message, "delete_data", result_status="deleted", response_text=response)
    await message.answer(response, reply_markup=_start_keyboard())


async def toggle_notifications(message: Message, state: FSMContext) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "notifications", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "notifications", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    enabled = await asyncio.to_thread(_store().toggle_notifications_enabled, user_id)
    credentials = await asyncio.to_thread(_store().get_credentials, user_id)
    baseline_status = ""
    if enabled and credentials is not None:
        baseline_status = await refresh_user_baselines_if_available(user_id, credentials)

    response = (
        "Уведомления включены. Бот будет проверять изменения примерно раз в час."
        if enabled
        else "Уведомления выключены. Автоматические сообщения о баллах и расписании приходить не будут."
    )
    if baseline_status:
        response = f"{response}\n{baseline_status}"
    await _log_event(
        message,
        "notifications",
        result_status="enabled" if enabled else "disabled",
        response_text=response,
    )
    await message.answer(response, reply_markup=_action_keyboard(user_id))
    await state.set_state(RatingStates.waiting_for_action)


async def ask_subject_from_items(
    message: Message,
    state: FSMContext,
    items: list[RatingItem],
    *,
    prefix: str | None = None,
) -> None:
    await state.set_state(RatingStates.waiting_for_subject)
    response = "Выберите предмет:"
    if prefix:
        response = f"{prefix}\n{response}"
    await _log_event(message, "rating_start", result_status="subject_requested", response_text=response)
    await message.answer(response, reply_markup=_subject_keyboard(items))


async def fetch_rating_items(credentials) -> list[RatingItem]:
    html = await asyncio.to_thread(
        RatingClient(login=credentials.login, password=credentials.password).fetch_html
    )
    return parse_rating_items_from_html(html)


async def save_rating_items_cache(telegram_user_id: int, items: list[RatingItem]) -> None:
    await asyncio.to_thread(
        _store().replace_rating_snapshots,
        telegram_user_id=telegram_user_id,
        snapshots=rating_items_to_snapshots(items),
    )


async def get_cached_rating_items(telegram_user_id: int) -> list[RatingItem]:
    snapshots = await asyncio.to_thread(_store().get_rating_snapshots, telegram_user_id)
    return rating_snapshots_to_items(snapshots)


def rating_snapshots_to_items(snapshots: dict[str, RatingSnapshot]) -> list[RatingItem]:
    return [
        RatingItem(
            subject=snapshot.subject,
            total=snapshot.total,
            attendance=snapshot.attendance,
            control=snapshot.control,
            creative=snapshot.creative,
            intermediate=snapshot.intermediate,
        )
        for snapshot in sorted(snapshots.values(), key=lambda snapshot: snapshot.subject)
    ]


def parse_rating_items_from_html(html: str) -> list[RatingItem]:
    return parse_rating_html(
        html,
        table_selector=os.getenv("RATING_TABLE_SELECTOR") or None,
        table_index=_env_int("RATING_TABLE_INDEX"),
        subject_column_index=_env_int("SUBJECT_COLUMN_INDEX"),
        score_column_index=_env_int("SCORE_COLUMN_INDEX"),
    )


def fetch_rating_and_schedule_html_once(
    *,
    login: str,
    password: str,
) -> tuple[str, str | None, str | None]:
    session = create_rea_session()
    rating_client = RatingClient(login=login, password=password)
    schedule_client = ScheduleClient(login=login, password=password)

    rating_client._login(session)
    rating_html = rating_client.fetch_html_from_session(session)

    try:
        schedule_html = schedule_client.fetch_week_html_from_session(session)
        schedule_error = None
    except ScheduleFetchError as exc:
        schedule_html = None
        schedule_error = str(exc)

    return rating_html, schedule_html, schedule_error


async def start_rating_flow(message: Message, state: FSMContext) -> None:
    if not await _is_allowed(message):
        response = "Доступ запрещен."
        await _log_event(message, "rating_start", result_status="access_denied", response_text=response)
        await message.answer(response)
        return

    user_id = _telegram_user_id(message)
    if user_id is None:
        response = "Не удалось определить пользователя Telegram."
        await _log_event(message, "rating_start", result_status="no_telegram_user", response_text=response)
        await message.answer(response)
        return

    credentials = _store().get_credentials(user_id)
    if credentials is None:
        await state.set_state(RatingStates.waiting_for_login)
        await state.update_data(pending_action="rating")
        response = "Сначала войдите в личный кабинет РЭУ. Введите логин."
        await _log_event(message, "rating_start", result_status="login_required", response_text=response)
        await message.answer(response, reply_markup=_back_keyboard())
        return

    try:
        items = await fetch_rating_items(credentials)
        await save_rating_items_cache(user_id, items)
    except (RatingFetchError, RatingParseError) as exc:
        cached_items = await get_cached_rating_items(user_id)
        if cached_items:
            response = (
                "Сайт РЭУ сейчас не ответил. Показываю последние сохраненные данные.\n"
                "Выберите предмет:"
            )
            await _log_event(
                message,
                "rating_start",
                result_status="cache_used",
                response_text=response,
                error_message=str(exc),
            )
            await state.set_state(RatingStates.waiting_for_subject)
            await message.answer(response, reply_markup=_subject_keyboard(cached_items))
            return

        response = f"Не удалось получить список предметов: {exc}"
        await _log_event(
            message,
            "rating_start",
            result_status="fetch_failed",
            response_text=response,
            error_message=str(exc),
        )
        await message.answer(response, reply_markup=_action_keyboard(user_id))
        await state.set_state(RatingStates.waiting_for_action)
        return

    await ask_subject_from_items(message, state, items)


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
        response = "Сначала войдите в личный кабинет РЭУ. Нажмите Баллы или Расписание пар."
        await _log_event(message, "subject_query", message_text=subject_query, subject_query=subject_query, result_status="no_credentials", response_text=response)
        await message.answer(response, reply_markup=_action_keyboard(user_id))
        return

    client = RatingClient(login=credentials.login, password=credentials.password)

    try:
        html = await asyncio.to_thread(client.fetch_html)
        items = parse_rating_items_from_html(html)
        await save_rating_items_cache(user_id, items)
    except (RatingFetchError, RatingParseError) as exc:
        cached_items = await get_cached_rating_items(user_id)
        cached_item = find_subject_score(cached_items, subject_query)
        if cached_item is not None:
            response = (
                "Сайт РЭУ сейчас не ответил. Показываю последние сохраненные баллы.\n\n"
                f"{format_rating_item(cached_item)}"
            )
            await _log_event(
                message,
                "subject_query",
                message_text=subject_query,
                subject_query=subject_query,
                subject_matched=cached_item.subject,
                result_status="cache_used",
                response_text=response,
                error_message=str(exc),
            )
            await message.answer(response, reply_markup=_subject_keyboard(cached_items))
            return

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
        await message.answer(response, reply_markup=_subject_keyboard(items))
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
    await message.answer(response, reply_markup=_subject_keyboard(items))


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


def build_schedule_snapshot(html: str):
    week = parse_schedule_html(html)
    text = schedule_snapshot_text(week)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest, text, schedule_week_key(week), week


async def save_schedule_baseline_if_available(
    *,
    telegram_user_id: int,
    login: str | None = None,
    password: str | None = None,
    html: str | None = None,
    fetch_error: str | None = None,
) -> None:
    if fetch_error:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="baseline_failed",
            error_message=fetch_error,
        )
        return

    try:
        if html is None:
            if not login or not password:
                raise ScheduleFetchError("Нет логина и пароля для сохранения снимка расписания.")
            html = await asyncio.to_thread(ScheduleClient(login=login, password=password).fetch_week_html)
        schedule_hash, schedule_text, schedule_key, _ = build_schedule_snapshot(html)
        await asyncio.to_thread(
            _store().save_schedule_snapshot,
            telegram_user_id=telegram_user_id,
            schedule_key=schedule_key,
            schedule_hash=schedule_hash,
            schedule_text=schedule_text,
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="baseline_saved",
            response_text=f"lessons={len(schedule_text.splitlines())}",
        )
    except (ScheduleFetchError, ScheduleParseError) as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="baseline_failed",
            error_message=str(exc),
        )


async def refresh_user_baselines_if_available(telegram_user_id: int, credentials) -> str:
    try:
        rating_html, schedule_html, schedule_error = await asyncio.to_thread(
            fetch_rating_and_schedule_html_once,
            login=credentials.login,
            password=credentials.password,
        )
        items = parse_rating_items_from_html(rating_html)
        await asyncio.to_thread(
            _store().replace_rating_snapshots,
            telegram_user_id=telegram_user_id,
            snapshots=rating_items_to_snapshots(items),
        )
        await save_schedule_baseline_if_available(
            telegram_user_id=telegram_user_id,
            html=schedule_html,
            fetch_error=schedule_error,
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="notifications",
            result_status="baseline_refreshed",
            response_text=f"subjects={len(items)}",
        )
        return "Текущий снимок обновлен, старые изменения не будут приходить задним числом."
    except (RatingFetchError, RatingParseError) as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="notifications",
            result_status="baseline_refresh_failed",
            error_message=str(exc),
        )
        return "Текущий снимок пока не обновился: сайт РЭУ не ответил или изменил страницу."


def format_schedule_change_notification(previous_text: str, current_text: str) -> str:
    previous_lines = [line for line in previous_text.splitlines() if line.strip()]
    current_lines = [line for line in current_text.splitlines() if line.strip()]
    previous_set = set(previous_lines)
    current_set = set(current_lines)

    added = [line for line in current_lines if line not in previous_set]
    removed = [line for line in previous_lines if line not in current_set]

    lines = ["Изменилось расписание на текущую неделю:"]
    if added:
        lines.append("")
        lines.append("Добавлено:")
        lines.extend(f"+ {line}" for line in added[:20])
    if removed:
        lines.append("")
        lines.append("Удалено:")
        lines.extend(f"- {line}" for line in removed[:20])
    if not added and not removed:
        lines.append("Структура расписания обновилась на сайте.")
    return "\n".join(lines)


def format_new_week_schedule_notification(week) -> str:
    return "Началась новая учебная неделя. Актуальное расписание:\n\n" + format_schedule_week(week)


def infer_schedule_week_key_from_snapshot_text(schedule_text: str) -> str | None:
    dates: list[str] = []
    for line in schedule_text.splitlines():
        day_part = line.split("|", 1)[0].strip()
        if "," not in day_part:
            continue
        _, date_value = day_part.split(",", 1)
        date_value = " ".join(date_value.split())
        if date_value and date_value not in dates:
            dates.append(date_value)
    return "|".join(dates) if dates else None


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
    credentials = await asyncio.to_thread(_store().list_notification_credentials)
    logging.info("Rating monitor cycle started from %s for %s users", source, len(credentials))
    await _log_store_event(
        telegram_user_id=None,
        event_type="rating_monitor",
        result_status="cycle_started",
        response_text=f"source={source} users={len(credentials)} notifications_only=true",
    )

    user_delay = max(_env_float("MONITOR_USER_DELAY_SECONDS", 30.0), 0.0)
    for index, credentials_item in enumerate(credentials):
        await check_user_rating_and_schedule_changes(bot, credentials_item)
        if user_delay and index < len(credentials) - 1:
            await asyncio.sleep(user_delay)


async def check_user_rating_and_schedule_changes(bot: Bot, credentials: StoredCredentials) -> None:
    telegram_user_id = credentials.telegram_user_id
    try:
        rating_html, schedule_html, schedule_error = await asyncio.to_thread(
            fetch_rating_and_schedule_html_once,
            login=credentials.login,
            password=credentials.password,
        )
    except RatingFetchError as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_monitor",
            result_status="fetch_failed",
            error_message=str(exc),
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="fetch_failed",
            error_message=str(exc),
        )
        return
    except Exception as exc:
        logging.exception("Monitor login failed for user_id=%s", telegram_user_id)
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_monitor",
            result_status="failed",
            error_message=str(exc),
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="failed",
            error_message=str(exc),
        )
        return

    await process_user_rating_changes(bot, telegram_user_id, rating_html)
    if schedule_html is None:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="fetch_failed",
            error_message=schedule_error or "Не удалось получить расписание.",
        )
        return

    await process_user_schedule_changes(bot, telegram_user_id, schedule_html)


async def process_user_rating_changes(bot: Bot, telegram_user_id: int, html: str) -> None:
    try:
        items = parse_rating_items_from_html(html)
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
    except RatingParseError as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="rating_monitor",
            result_status="parse_failed",
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


async def process_user_schedule_changes(bot: Bot, telegram_user_id: int, html: str) -> None:
    try:
        current_hash, current_text, current_key, current_week = build_schedule_snapshot(html)
        previous = await asyncio.to_thread(_store().get_schedule_snapshot, telegram_user_id)

        if previous is None:
            await asyncio.to_thread(
                _store().save_schedule_snapshot,
                telegram_user_id=telegram_user_id,
                schedule_key=current_key,
                schedule_hash=current_hash,
                schedule_text=current_text,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="schedule_monitor",
                result_status="baseline_saved",
                response_text=f"lessons={len(current_text.splitlines())}",
            )
            return

        previous_key = previous.schedule_key or infer_schedule_week_key_from_snapshot_text(previous.schedule_text)
        if previous_key is None:
            await asyncio.to_thread(
                _store().save_schedule_snapshot,
                telegram_user_id=telegram_user_id,
                schedule_key=current_key,
                schedule_hash=current_hash,
                schedule_text=current_text,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="schedule_monitor",
                result_status="week_key_backfilled",
                response_text=f"lessons={len(current_text.splitlines())}",
            )
            return

        if previous_key != current_key:
            notification = format_new_week_schedule_notification(current_week)
            for notification_part in split_telegram_text(notification):
                await bot.send_message(chat_id=telegram_user_id, text=notification_part)
            await asyncio.to_thread(
                _store().save_schedule_snapshot,
                telegram_user_id=telegram_user_id,
                schedule_key=current_key,
                schedule_hash=current_hash,
                schedule_text=current_text,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="schedule_week_change",
                result_status="notified",
                response_text=notification,
            )
            return

        if previous.schedule_key is None and previous.schedule_hash == current_hash:
            await asyncio.to_thread(
                _store().save_schedule_snapshot,
                telegram_user_id=telegram_user_id,
                schedule_key=current_key,
                schedule_hash=current_hash,
                schedule_text=current_text,
            )
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="schedule_monitor",
                result_status="week_key_backfilled",
                response_text=f"lessons={len(current_text.splitlines())}",
            )
            return

        if previous.schedule_hash == current_hash:
            await _log_store_event(
                telegram_user_id=telegram_user_id,
                event_type="schedule_monitor",
                result_status="no_changes",
                response_text=f"lessons={len(current_text.splitlines())}",
            )
            return

        notification = format_schedule_change_notification(previous.schedule_text, current_text)
        for notification_part in split_telegram_text(notification):
            await bot.send_message(chat_id=telegram_user_id, text=notification_part)
        await asyncio.to_thread(
            _store().save_schedule_snapshot,
            telegram_user_id=telegram_user_id,
            schedule_key=current_key,
            schedule_hash=current_hash,
            schedule_text=current_text,
        )
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_change",
            result_status="notified",
            response_text=notification,
        )
    except (ScheduleFetchError, ScheduleParseError) as exc:
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
            result_status="fetch_failed",
            error_message=str(exc),
        )
    except Exception as exc:
        logging.exception("Schedule monitor failed for user_id=%s", telegram_user_id)
        await _log_store_event(
            telegram_user_id=telegram_user_id,
            event_type="schedule_monitor",
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
    await configure_bot_profile(bot)
    await bot.set_webhook(
        webhook_url,
        secret_token=get_webhook_secret(),
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    logging.info("Webhook set to %s", webhook_url)


async def configure_bot_profile(bot: Bot) -> None:
    commands = [
        BotCommand(command="rating", description="Просмотр баллов"),
        BotCommand(command="schedule", description="Расписание пар"),
    ]
    await bot.set_my_commands(commands)
    with contextlib.suppress(Exception):
        await bot.set_my_description(BOT_DESCRIPTION)
    with contextlib.suppress(Exception):
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)


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
    await configure_bot_profile(bot)
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
