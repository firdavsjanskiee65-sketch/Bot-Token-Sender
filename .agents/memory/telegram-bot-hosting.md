---
name: Telegram bot hosting and logs
description: Replit deployment and logging constraints for long-running Telegram polling bots.
---

Telegram polling bots need a VM / Always Running deployment to continue after the editor session ends; an editor workflow alone is not a 24/7 guarantee.

**Why:** A workspace process can stop when the interactive Replit session closes, while VM deployment is intended for long-running background processes.

**How to apply:** Keep the bot workflow configured for development, but tell the user to publish it as VM / Always Running for continuous operation.

HTTP client request logging must be kept at warning level or lower for bot processes.

**Why:** Some HTTP client loggers include the full Telegram API URL, which can contain the bot token.

**How to apply:** Silence verbose `httpx` and `httpcore` logs before starting the bot and never include startup logs containing API URLs in user-facing responses.