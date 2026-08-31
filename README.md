# NarroMarket

NarroMarket is a Telegram bot for selling time-limited subscriptions. Customers choose a plan,
pay with Telegram Stars, and receive or renew access in chat. Staff manage the catalog, orders,
customers, and delivery in the same bot.

The project uses Telethon, SQLite through aiosqlite, and APScheduler. It runs as a single Python
process and does not include a web dashboard.

## Features

- Public catalog and per-customer offers
- Telegram Stars invoices with payment validation and refunds
- Orders, subscriptions, renewals and expiry reminders
- Manual delivery and updating of access details
- In-bot administration for products, users, roles, settings and broadcasts
- Optional product images stored on the server
- Local SQLite storage with schema migrations

## Payment rules

Telegram requires digital goods and services sold inside bots to be paid for exclusively with
[Telegram Stars](https://core.telegram.org/bots/payments-stars). Because NarroMarket sells
subscription access, Telegram Stars are the only supported payment method inside the bot. The
repository still contains a manual bank-transfer and receipt flow; do not enable it as an
alternative way to buy digital access inside Telegram.

The bot provides `/terms`, `/support` and `/paysupport`, and asks the customer to confirm the
terms before it creates a Stars invoice. Review the default copy in `/admin` and replace it with
terms and support details appropriate for your business before accepting payments.

## Requirements

- Python 3.10 or newer
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A Linux host if you plan to use the included systemd service

## Setup

```bash
git clone https://github.com/narrovel/narromarket.git
cd narromarket
python3 -m venv venv
venv/bin/pip install -r requirements.txt
umask 077
cp .env.example .env
```

Fill in `BOT_TOKEN`, `API_ID`, `API_HASH` and `OWNER_IDS` in `.env`, then start the bot:

```bash
venv/bin/python bot.py
```

Open the bot and send `/start`. An owner can open the staff panel with `/admin`.

To add a disposable sample catalog before the first start:

```bash
venv/bin/python -m tools.seed_demo
```

The seed command writes to the configured database. It refuses to add products to a non-empty
catalog unless it is run again with `--force`.

## Configuration

`.env.example` documents the startup settings. `OWNER_IDS` is a comma-separated list of numeric
Telegram IDs.

The list controls who receives the `owner` role at startup. An owner removed from `OWNER_IDS`
is demoted to `user` on the next start. Manager and admin roles are managed separately in the
staff panel.

Most shop settings are stored in SQLite and edited from `/admin`, including the shop name,
manager username, reminders, catalog page size and customer-facing copy. Product images are not
uploaded through the panel: place them in `images/` and enter the file name on the product card.

## Deployment

`deploy/narromarket.service` is a systemd unit template for source code in `/opt/narromarket`.
By default it keeps runtime state in `/opt/narromarket/data` and logs in
`/opt/narromarket/logs`, matching earlier releases. Review the user, group and paths before
installing it. If you set different paths in `.env`, add them to `ReadWritePaths` in the unit.

Before enabling the unit, make sure that:

- the repository is checked out at `/opt/narromarket`;
- its virtual environment is `/opt/narromarket/venv`, with the project dependencies installed;
- `/opt/narromarket/.env` contains the production configuration and is readable by the
  `narromarket` group;
- the `narromarket` system user and group exist.

For example, create the service account and protect the configuration file with:

```bash
sudo useradd --system --home-dir /var/lib/narromarket --shell /usr/sbin/nologin narromarket
sudo install -d -o narromarket -g narromarket -m 0700 \
  /opt/narromarket/data /opt/narromarket/logs
sudo chown root:narromarket /opt/narromarket/.env
sudo chmod 0640 /opt/narromarket/.env
```

The `useradd` command is needed only once. Then install and start the unit:

```bash
sudo cp /opt/narromarket/deploy/narromarket.service /etc/systemd/system/narromarket.service
sudo systemctl daemon-reload
sudo systemctl enable --now narromarket
```

Check startup and runtime errors with:

```bash
systemctl status narromarket
journalctl -u narromarket
```

## Data and security

Keep `.env`, the Telethon session file, the SQLite database and uploaded receipts private. The
included systemd unit uses `UMask=0077`; for a manual deployment, use a restrictive umask and
permissions as well.

Important limitations:

- Access details are stored as plaintext in SQLite and are also sent through Telegram chats.
- Transfer receipts are stored under `data/receipts/` until the configured retention period
  has passed after a final outcome or access delivery. Receipts for an open review or problem
  are kept longer. They are also copied to staff chats, and removing the server copy does not
  remove Telegram's copies.
- Orders, payment identifiers and staff audit records are retained for accounting and incident
  review. Erasing a profile removes its Telegram ID from the customer record and activity
  history, but a paid Stars order keeps the original payment recipient ID for later refunds and
  financial reconciliation. The erase action does not rewrite server logs, the Telethon session
  or copies already sent through Telegram.
- The application does not create backups. Back up both the database and receipt directory.
- Run only one bot process against a database. In-process locks do not coordinate multiple
  instances.
- Renewals are separate purchases; this version does not create automatically recurring Stars
  subscriptions.
- Customer and staff messages are currently English, while the manual transfer flow assumes RUB.

For a consistent SQLite backup while the bot is running, use `VACUUM INTO` with a new file name.

```bash
sqlite3 data/narromarket.db "VACUUM INTO '/backup/narromarket-$(date +%F).db'"
```

## Checks

The repository includes four offline checks. They use temporary databases and do not need
Telegram credentials or a network connection.

```bash
python -m pip install -r requirements-dev.txt
ruff check .
python -m tools.selfcheck
python -m tools.walkthrough
python -m tools.edgecases
python -m tools.loadcheck
bandit -q -r bot.py config.py db handlers services utils -s B608
pip-audit -r requirements.txt
```

Bandit's B608 check is skipped because the dynamic SQL fragments are internal column names or
placeholder counts; user-provided values are passed as query parameters.

Ruff, the four scenario suites, Bandit and pip-audit run in GitHub Actions on pushes and pull
requests.

## Project layout

```text
bot.py              application entry point
config.py           environment configuration
db/                 schema and database queries
handlers/           customer and staff interactions
services/           billing, permissions, notifications and scheduled work
utils/              dates, keyboards, text formatting and flow state
tools/seed_demo.py   optional sample catalog
tools/*check.py      offline checks
deploy/              systemd service template
```

## License

[MIT](LICENSE)
