# GAVA Telegram Bot

Русскоязычный Telegram-бот с внутренней валютой GAVA, играми, бонусами, переводами, дуэлями, промокодами и админ-панелью.

## Run & Operate

- `python bot.py` — запуск Telegram-бота
- `python -m py_compile bot.py` — проверка синтаксиса
- `pnpm --filter @workspace/api-server run dev` — общий API-сервис шаблона
- Workflow `GAVA Telegram Bot` — постоянный процесс бота в рабочей среде
- Required secrets: `TELEGRAM_BOT_TOKEN` (или `BOT_TOKEN`) и `ADMIN_ID`
- SQLite database: `gava.sqlite3`

## Stack

- Python 3.11
- `python-telegram-bot` 22.x
- SQLite with WAL mode and atomic transactions
- Replit Secrets and VM deployment for continuous polling

## Where things live

- `bot.py` — обработчики Telegram, игры, админ-панель и слой SQLite
- `requirements.txt` — Python-зависимость бота
- `README.md` — настройка BotFather, групп и публикации

## Architecture decisions

- Важные состояния хранятся в SQLite, а не только в памяти процесса.
- Баланс меняется только внутри транзакций с записью в журнал операций.
- Callback-кнопки проверяют Telegram ID, ID игры и текущий статус перед выплатой.
- Для постоянной работы бота нужен VM / Always Running deployment, а не только открытая сессия редактора.

## Product

Игроки получают ежедневные бонусы, участвуют в пяти играх на GAVA, переводят валюту, используют промокоды и соревнуются в рейтинге. Администратор управляет пользователями, балансами, бонусами, блокировками и промокодами.

## User preferences

- Пользователь попросил, чтобы сообщения и интерфейс бота были только на русском языке.

## Gotchas

- Для текстовых триггеров в группах может потребоваться отключить Privacy Mode через BotFather.
- Для работы после закрытия Replit проект нужно опубликовать как VM / Always Running.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
