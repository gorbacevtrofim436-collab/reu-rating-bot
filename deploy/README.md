# VPS deployment

The production VPS runs the Telegram bot with long polling. A public HTTP
server is not required.

## Environment

Store secrets only in `/etc/reu-rating-bot.env` with mode `600`. The VPS
configuration uses:

```dotenv
RUN_MODE=polling
BOT_DB_PATH=/opt/reu-rating-bot/data/credentials.sqlite3
DATABASE_URL=
```

Keep `BOT_TOKEN`, `BOT_CREDENTIAL_KEY` and the remaining project settings in
the same protected file.

## Services

Install the unit files:

```bash
sudo install -m 644 deploy/reu-rating-bot.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-monitor.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-monitor.timer /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-backup.service /etc/systemd/system/
sudo install -m 644 deploy/reu-rating-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reu-rating-bot.service
sudo systemctl enable --now reu-rating-monitor.timer
sudo systemctl enable --now reu-rating-backup.timer
```

Check the deployment:

```bash
systemctl status reu-rating-bot.service
systemctl list-timers reu-rating-monitor.timer reu-rating-backup.timer
journalctl -u reu-rating-bot.service -n 50 --no-pager
```

## Firewall

Polling does not require inbound HTTP traffic. Keep only SSH open:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```
