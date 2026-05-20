from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from rating_client import RatingClient, RatingFetchError
from rating_parser import RatingItem, RatingParseError, find_subject_score, parse_rating_html
from user_store import UserStore, UserStoreError


router = Router()


class RatingStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_subject = State()


store: UserStore | None = None


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
    await message.answer(response)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    logging.info("Received /cancel from user_id=%s", _telegram_user_id(message))
    await state.clear()
    response = "Действие отменено."
    await _log_event(message, "command", message_text="/cancel", result_status="cancelled", response_text=response)
    await message.answer(response)


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
        parse_rating_html(
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
    await state.set_state(RatingStates.waiting_for_subject)
    response = "Готово. Теперь напишите предмет, например: матан, англ, алгоритмы."
    await _log_event(message, "rea_login_result", message_text=login_value, result_status="success", response_text=response)
    await message.answer(response)


@router.message(RatingStates.waiting_for_subject, F.text)
async def handle_subject(message: Message) -> None:
    logging.info("Received subject query from user_id=%s", message.from_user.id if message.from_user else None)
    await answer_subject(message)


@router.message(F.text)
async def handle_subject_without_state(message: Message) -> None:
    logging.info("Received stateless subject query from user_id=%s", message.from_user.id if message.from_user else None)
    if message.text and message.text.startswith("/"):
        return
    await answer_subject(message)


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


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "reu-rating-bot"})


async def on_webhook_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    webhook_url = build_webhook_url()
    await bot.set_webhook(
        webhook_url,
        secret_token=get_webhook_secret(),
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    logging.info("Webhook set to %s", webhook_url)


def run_webhook() -> None:
    global store
    store = UserStore()
    bot = Bot(token=get_bot_token())
    dispatcher = create_dispatcher()
    dispatcher.startup.register(on_webhook_startup)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=get_webhook_secret(),
    ).register(app, path="/webhook")
    setup_application(app, dispatcher, bot=bot)

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
