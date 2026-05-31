# REU Rating Telegram Bot

Telegram-бот для студентов РЭУ. Он хранит зашифрованные данные входа в ЛКС,
показывает сохраненные баллы и расписание без ожидания ответа сайта, а отдельный
monitor раз в час проверяет изменения.

## Возможности

- быстрый просмотр баллов по предметам и расписания текущей недели;
- уведомления об изменениях баллов и расписания;
- надежная очередь доставки уведомлений с повторными попытками;
- повторная загрузка данных нового аккаунта каждые пять минут;
- административные объявления через `/broadcast`;
- административный статус через `/admin`;
- удаление входа в ЛКС или полное удаление пользовательских данных.

## Архитектура production

- Python + `aiogram`;
- polling на постоянно работающем VPS;
- SQLite в `/opt/reu-rating-bot/data/credentials.sqlite3`;
- `reu-rating-bot.service` принимает сообщения Telegram;
- `reu-rating-monitor.timer` проверяет ЛКС раз в час;
- `reu-rating-sync-retry.timer` повторяет незавершенные регистрации раз в пять минут;
- `reu-rating-backup.timer` ежедневно проверяет и сохраняет резервную копию SQLite;
- `reu-rating-watchdog.timer` проверяет сервис, Telegram API, monitor и backup.

Render, Neon и GitHub Actions больше не используются.

## Безопасность

- секреты хранятся только в `/etc/reu-rating-bot.env` с правами `600`;
- пароль пользователя шифруется ключом `BOT_CREDENTIAL_KEY`;
- сообщения с паролями удаляются из Telegram после получения;
- незавершенный ввод логина истекает через 30 минут;
- логи старше 90 дней удаляются;
- `Выйти из аккаунта` удаляет данные ЛКС и снимки;
- `Удалить все мои данные` также удаляет историю действий и подписку на объявления.

Шифрование защищает резервную копию от простого просмотра. Администратор VPS,
имеющий одновременно базу и `BOT_CREDENTIAL_KEY`, технически может расшифровать
пароли. Пользователи должны понимать это до ввода данных.

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Перед запуском заполните `BOT_TOKEN`, `BOT_ADMIN_USER_ID` и
`BOT_CREDENTIAL_KEY`. Сгенерировать ключ:

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

## Проверка

```bash
python -m unittest discover -s tests -v
python -m py_compile bot.py user_store.py rating_client.py rating_parser.py \
  schedule_client.py schedule_parser.py deploy/backup_sqlite.py deploy/watchdog.py
```

## VPS

Точные команды находятся в [`deploy/README.md`](deploy/README.md).

## Команды бота

- `/start` — начать работу;
- `/rating` — баллы;
- `/schedule` — расписание;
- `/check` — дополнительная загрузка из ЛКС;
- `/help` — помощь;
- `/logout` — удалить данные входа ЛКС;
- `/admin` — закрытый статус для владельца;
- `/broadcast` — закрытая рассылка для владельца.
