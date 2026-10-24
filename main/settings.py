"""Load local PyCharm config or deployment environment variables."""

import os


try:
    try:
        from .config import token_ms, token_tg, users
    except ImportError:
        # bot.py is normally started directly from PyCharm, not as a package.
        from config import token_ms, token_tg, users
except ImportError:
    token_ms = os.getenv("MOYSKLAD_TOKEN", "").strip()
    token_tg = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_users = os.getenv("TELEGRAM_USER_IDS", "")
    users = [int(value.strip()) for value in raw_users.split(",") if value.strip()]

if not token_ms or not token_tg:
    raise RuntimeError(
        "Не найдены токены. Создайте main/config.py для PyCharm или задайте "
        "MOYSKLAD_TOKEN и TELEGRAM_BOT_TOKEN в переменных окружения."
    )
