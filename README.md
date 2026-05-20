# REU Rating Telegram Bot

Telegram-бот для студентов РЭУ: пользователь вводит логин и пароль от личного кабинета, бот получает страницу рейтинга и отвечает баллами по предмету.

## Что внутри

- Язык: Python.
- Telegram-библиотека: `aiogram`.
- Точка входа: `bot.py`.
- Production-запуск: webhook через `aiohttp` HTTP-сервер.
- Локальный fallback: polling через `RUN_MODE=polling`.
- Хранилище: SQLite локально или Postgres через `DATABASE_URL` на Render.
- Пароли пользователей шифруются через `BOT_CREDENTIAL_KEY`.

## Routes

- `GET /` — health-check.
- `GET /health` — health-check для Render.
- `POST /webhook` — Telegram webhook.

Webhook защищен через заголовок Telegram:

```text
X-Telegram-Bot-Api-Secret-Token
```

Неверный `WEBHOOK_SECRET` отклоняется с HTTP `401`.

## Переменные окружения

Обязательные для Render:

```env
BOT_TOKEN=
WEBHOOK_URL=
WEBHOOK_SECRET=
BOT_CREDENTIAL_KEY=
DATABASE_URL=
RUN_MODE=webhook
PORT=10000
```

Настройки РЭУ:

```env
REA_RATING_URL=https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр
REA_LOGIN_URL=https://student.rea.ru/
REA_REQUEST_TIMEOUT=30
```

Локальные fallback-настройки:

```env
BOT_DB_PATH=credentials.sqlite3
RUN_MODE=polling
```

Сгенерировать `BOT_CREDENTIAL_KEY`:

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

Сгенерировать `WEBHOOK_SECRET`:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
```

## Деплой на Render Free Web Service

### Вариант через Dashboard

1. Залей проект в GitHub.
2. Открой [Render](https://render.com/) → `New` → `Web Service`.
3. Подключи репозиторий.
4. Укажи:
   - Runtime: `Python`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
   - Instance Type: `Free`
   - Health Check Path: `/health`
5. Создай Render Postgres:
   - `New` → `Postgres`
   - Instance Type: `Free`
6. В Web Service добавь Environment Variables:

```env
BOT_TOKEN=токен_из_BotFather
WEBHOOK_URL=https://your-service-name.onrender.com
WEBHOOK_SECRET=случайная_строка
BOT_CREDENTIAL_KEY=ключ_Fernet
DATABASE_URL=Internal Database URL из Render Postgres
RUN_MODE=webhook
PORT=10000
REA_RATING_URL=https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр
REA_LOGIN_URL=https://student.rea.ru/
REA_REQUEST_TIMEOUT=30
```

7. Нажми `Deploy`.
8. Открой:

```text
https://your-service-name.onrender.com/health
```

Должен быть ответ:

```json
{"status":"ok","service":"reu-rating-bot"}
```

### Вариант через render.yaml

В проекте есть `render.yaml`. Можно создать Render Blueprint из репозитория. Секреты с `sync: false` Render попросит ввести при создании.

После создания сервиса проверь, что `WEBHOOK_URL` равен публичному URL сервиса без `/webhook`, например:

```text
https://reu-rating-bot.onrender.com
```

## Установка webhook вручную

Код сам вызывает `setWebhook` при старте в `RUN_MODE=webhook`. Если нужно установить вручную:

```bash
curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}/webhook" \
  -d "secret_token=${WEBHOOK_SECRET}"
```

Проверить webhook:

```bash
curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

В `.env` для локального polling:

```env
BOT_TOKEN=токен_из_BotFather
BOT_CREDENTIAL_KEY=ключ_Fernet
BOT_DB_PATH=credentials.sqlite3
RUN_MODE=polling
REA_RATING_URL=https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр
REA_LOGIN_URL=https://student.rea.ru/
REA_REQUEST_TIMEOUT=30
```

Запуск:

```bash
RUN_MODE=polling python bot.py
```

## Откат с webhook на локальный polling

1. В Render останови Web Service.
2. Удали webhook:

```bash
curl "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook"
```

3. На Mac запусти:

```bash
cd "/Users/trofim/Documents/New project"
source .venv/bin/activate
RUN_MODE=polling python bot.py
```

## Ограничения бесплатного Render

- Free Web Service может засыпать при неактивности.
- Free Render Postgres истекает через 30 дней, затем данные нужно переносить или перейти на платный тариф.
- Для долгого production-использования лучше VPS или платная база.

## Безопасность

- Не коммить `.env`, `credentials.sqlite3`, токены и базу.
- `BOT_TOKEN`, `WEBHOOK_SECRET`, `BOT_CREDENTIAL_KEY`, `DATABASE_URL` задаются только через Environment Variables.
- Пользователи должны понимать, что они вводят данные от ЛКС РЭУ в ваш бот.
