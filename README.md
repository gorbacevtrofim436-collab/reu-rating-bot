# REU Rating Telegram Bot

Telegram-бот для студентов РЭУ: пользователь вводит логин и пароль от личного кабинета, бот получает страницу рейтинга и отвечает баллами по предмету.

## Что внутри

- Язык: Python.
- Telegram-библиотека: `aiogram`.
- Точка входа: `bot.py`.
- Production-запуск: webhook через `aiohttp` HTTP-сервер.
- Локальный fallback: polling через `RUN_MODE=polling`.
- Хранилище: SQLite локально или внешний Postgres через `DATABASE_URL` на Render.
- Пароли пользователей шифруются через `BOT_CREDENTIAL_KEY`.
- Журнал действий: таблица `bot_events` хранит команды, запросы предметов, статусы и ответы бота без паролей.
- Мониторинг изменений: таблицы `rating_snapshots` и `schedule_snapshots` хранят последние снимки, а GitHub Actions раз в час запускает отдельный Python-процесс, подключается к Neon и отправляет уведомления без зависимости от сна Render. При смене недели бот отправляет полное расписание новой недели, а не список удаленных и добавленных пар.
- Уведомления можно включить или выключить кнопкой в главном меню; мониторинг проверяет только пользователей с включенными уведомлениями.
- Расписание: бот берет текущую неделю из ЛКС РЭУ (`student.rea.ru/lessons`) по тем же сохраненным логину и паролю.

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
MONITOR_ENABLED=true
MONITOR_BACKGROUND_ENABLED=false
MONITOR_RUN_SECRET=
MONITOR_INTERVAL_SECONDS=3600
MONITOR_USER_DELAY_SECONDS=120
MONITOR_STOP_ON_TRANSIENT_FAILURE=true
INTERACTIVE_REFRESH_COOLDOWN_SECONDS=21600
INITIAL_SYNC_RETRY_DELAYS_SECONDS=0,60,300,900,1800
```

Настройки РЭУ:

```env
REA_RATING_URL=https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр
REA_LOGIN_URL=https://student.rea.ru/
REA_REQUEST_TIMEOUT=25
REA_HTTP_RETRIES=0
REA_HTTP_CONNECT_RETRIES=0
REA_HTTP_READ_RETRIES=0
REA_HTTP_STATUS_RETRIES=0
REA_LESSONS_URL=https://student.rea.ru/lessons/index.php?login=yes
REA_SCHEDULE_REQUEST_TIMEOUT=25
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

## Деплой на Render Free Web Service + Neon Free Postgres

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
5. Создай бесплатную Postgres-базу в [Neon](https://neon.com):
   - план: `Free`
   - регион можно выбрать ближе к Render, например `eu-central-1`
   - скопируй pooled connection string с `sslmode=require`
6. В Web Service добавь Environment Variables:

```env
BOT_TOKEN=токен_из_BotFather
WEBHOOK_URL=https://your-service-name.onrender.com
WEBHOOK_SECRET=случайная_строка
BOT_CREDENTIAL_KEY=ключ_Fernet
DATABASE_URL=connection string из Neon
RUN_MODE=webhook
PORT=10000
MONITOR_ENABLED=true
MONITOR_BACKGROUND_ENABLED=false
MONITOR_RUN_SECRET=случайная_строка
MONITOR_USER_DELAY_SECONDS=120
MONITOR_STOP_ON_TRANSIENT_FAILURE=true
INTERACTIVE_REFRESH_COOLDOWN_SECONDS=21600
INITIAL_SYNC_RETRY_DELAYS_SECONDS=0,60,300,900,1800
REA_RATING_URL=https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр
REA_LOGIN_URL=https://student.rea.ru/
REA_REQUEST_TIMEOUT=25
REA_HTTP_RETRIES=0
REA_HTTP_CONNECT_RETRIES=0
REA_HTTP_READ_RETRIES=0
REA_HTTP_STATUS_RETRIES=0
REA_LESSONS_URL=https://student.rea.ru/lessons/index.php?login=yes
REA_SCHEDULE_REQUEST_TIMEOUT=25
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

В проекте есть `render.yaml`. Можно создать Render Blueprint из репозитория. Секреты с `sync: false` Render попросит ввести при создании. `DATABASE_URL` нужно взять из Neon.

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

## Ежечасный мониторинг через GitHub Actions

Render Free может засыпать, поэтому проверку запускает GitHub Actions workflow `.github/workflows/hourly-monitor.yml`. Workflow раз в час поднимает Python, подключается к Neon, проверяет ЛКС РЭУ и сам отправляет Telegram-уведомления. Ошибка у одного пользователя не останавливает проверку остальных; между пользователями есть пауза, чтобы не дергать сайт слишком часто. В GitHub repo settings нужно добавить Actions secrets:

```env
BOT_TOKEN
BOT_CREDENTIAL_KEY
DATABASE_URL
```

Запустить проверку вручную можно в GitHub → `Actions` → `Hourly rating monitor` → `Run workflow`.

За один цикл мониторинг делает один вход в ЛКС на пользователя и после этого забирает обе страницы: рейтинг и расписание.

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
REA_REQUEST_TIMEOUT=25
REA_HTTP_RETRIES=0
REA_HTTP_CONNECT_RETRIES=0
REA_HTTP_READ_RETRIES=0
REA_HTTP_STATUS_RETRIES=0
REA_LESSONS_URL=https://student.rea.ru/lessons/index.php?login=yes
REA_SCHEDULE_REQUEST_TIMEOUT=25
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

## Ограничения бесплатных тарифов

- Free Web Service может засыпать при неактивности.
- Neon Free имеет лимиты по хранению и compute-hours. Для небольшого личного бота этого обычно достаточно.
- Для долгого production-использования с гарантированной доступностью лучше VPS или платная база.

## Безопасность

- Не коммить `.env`, `credentials.sqlite3`, токены и базу.
- `BOT_TOKEN`, `WEBHOOK_SECRET`, `BOT_CREDENTIAL_KEY`, `DATABASE_URL` задаются только через Environment Variables.
- Пользователи должны понимать, что они вводят данные от ЛКС РЭУ в ваш бот.
- Пароли не пишутся в `bot_events`; они хранятся только в `rea_credentials.rea_password_encrypted`.

## Просмотр данных в Neon

Пользователи:

```sql
SELECT
  telegram_user_id,
  rea_login,
  created_at,
  updated_at
FROM rea_credentials
ORDER BY updated_at DESC;
```

Последние события бота:

```sql
SELECT
  created_at,
  telegram_user_id,
  event_type,
  message_text,
  subject_query,
  subject_matched,
  result_status,
  response_text,
  error_message
FROM bot_events
ORDER BY created_at DESC
LIMIT 100;
```

Последние сохранённые снимки баллов:

```sql
SELECT
  updated_at,
  telegram_user_id,
  subject,
  attendance,
  control,
  creative,
  intermediate,
  total
FROM rating_snapshots
ORDER BY updated_at DESC, subject;
```

Настройки уведомлений:

```sql
SELECT
  c.telegram_user_id,
  c.rea_login,
  COALESCE(s.notifications_enabled, TRUE) AS notifications_enabled
FROM rea_credentials AS c
LEFT JOIN user_settings AS s
  ON s.telegram_user_id = c.telegram_user_id
ORDER BY c.updated_at DESC;
```

Проверки баллов пишут события `rating_monitor`, найденные изменения баллов пишутся как `rating_change`. Проверки расписания пишут `schedule_monitor`, найденные изменения расписания пишутся как `schedule_change`.

Последний снимок расписания:

```sql
SELECT
  updated_at,
  telegram_user_id,
  schedule_key,
  schedule_hash,
  schedule_text
FROM schedule_snapshots
ORDER BY updated_at DESC;
```

## Команды бота

- `/start` — показать приветствие и меню выбора действия.
- `/login` — сменить логин и пароль ЛКС.
- `/logout` — удалить данные входа, настройки уведомлений, снимки баллов и снимок расписания.
- `/schedule` или сообщение `расписание` — открыть выбор дня текущей недели.

В обычном сценарии пользователь работает кнопками: `Баллы`, `Расписание пар`, `Включить уведомления об изменениях` / `Выключить уведомления об изменениях`, `Удалить данные`, `Назад`. Для рейтинга бот показывает полные названия предметов, но короткие варианты вроде `матан`, `англ`, `алгоритмы` по-прежнему понимает при ручном вводе.
