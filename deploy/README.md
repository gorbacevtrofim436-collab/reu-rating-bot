# VPS deployment

Production работает через polling. Входящие HTTP-порты не нужны.

## Каталоги

```bash
sudo install -d -m 755 -o root -g root /opt/reu-rating-bot/app
sudo install -d -m 700 -o reubot -g reubot /opt/reu-rating-bot/data
sudo install -d -m 700 -o reubot -g reubot /opt/reu-rating-bot/backups
sudo install -m 600 -o root -g root .env.example /etc/reu-rating-bot.env
```

Заполните `/etc/reu-rating-bot.env`. Для production обязательно:

```dotenv
RUN_MODE=polling
BOT_DB_PATH=/opt/reu-rating-bot/data/credentials.sqlite3
BOT_TOKEN=
BOT_ADMIN_USER_ID=
BOT_CREDENTIAL_KEY=
```

## Установка

```bash
sudo rsync -a --delete --exclude .git --exclude .env --exclude .venv \
  ./ /opt/reu-rating-bot/app/
sudo chown -R root:root /opt/reu-rating-bot/app

sudo install -m 644 deploy/reu-rating-bot.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-monitor.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-monitor.timer /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-sync-retry.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-sync-retry.timer /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-backup.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-backup.timer /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-watchdog.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-watchdog.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now reu-rating-bot.service
sudo systemctl enable --now reu-rating-monitor.timer
sudo systemctl enable --now reu-rating-sync-retry.timer
sudo systemctl enable --now reu-rating-backup.timer
sudo systemctl enable --now reu-rating-watchdog.timer
```

## Проверка

```bash
systemctl status reu-rating-bot.service
systemctl list-timers 'reu-rating-*'
journalctl -u reu-rating-bot.service -n 50 --no-pager
journalctl -u reu-rating-monitor.service -n 50 --no-pager
```

## Резервные копии вне VPS

Локальные backup защищают от ошибки приложения, но не от потери всего VPS.
Подключите внешний диск или object storage в `/mnt/reu-rating-bot-backups`.
Внешняя копия будет дополнительно зашифрована отдельным Fernet-ключом:

```dotenv
BOT_BACKUP_OFFSITE_DIR=/mnt/reu-rating-bot-backups
BOT_BACKUP_KEY=отдельный_Fernet_ключ
```

Без внешнего хранилища backup остаются только на VPS.

## Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```
