"""Load local PyCharm config or deployment environment variables."""

import os


OFFLINE_TEST_MODE = os.getenv("PARADISE_OFFLINE_TEST_MODE") == "1"


token_ms = os.getenv("MOYSKLAD_TOKEN", "").strip()
token_tg = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
raw_users = os.getenv("TELEGRAM_USER_IDS", "").strip()

if token_tg and (token_ms or OFFLINE_TEST_MODE) and raw_users:
    users = [int(value.strip()) for value in raw_users.split(",") if value.strip()]
else:
    try:
        try:
            from .config import token_ms, token_tg, users
        except ImportError:
            # bot.py is normally started directly from PyCharm, not as a package.
            from config import token_ms, token_tg, users
    except ImportError:
        users = [int(value.strip()) for value in raw_users.split(",") if value.strip()]

if OFFLINE_TEST_MODE and token_ms:
    raise RuntimeError("В тестовом режиме удалите токен МоегоСклада из окружения и config.py.")

if not token_tg or (not OFFLINE_TEST_MODE and not token_ms):
    raise RuntimeError(
        "Не найдены токены. Создайте main/config.py для PyCharm или задайте "
        "MOYSKLAD_TOKEN и TELEGRAM_BOT_TOKEN в переменных окружения."
    )
