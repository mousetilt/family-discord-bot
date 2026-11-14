# -*- coding: utf-8 -*-
"""Бот семьи Ульяновы — Россия Онлайн (сервер Арбатский). Запуск: python bot.py (или start.bat на Windows)."""
import logging
import sys
from logging.handlers import RotatingFileHandler

import discord

from core import config
from core.botcore import FamilyBot


def main():
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    for noisy in ("discord.gateway", "disnake.gateway"):  # служебные переподключения к Discord — не ошибки
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Предупреждения и ошибки с трассировкой — ещё и в файл: консоль хостинга хранит только последние строки.
    try:
        fh = RotatingFileHandler(config.ERR_LOG, maxBytes=300_000, backupCount=1, encoding="utf-8")
        fh.setLevel(logging.WARNING)
        fh.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(fh)
        logging.warning("=== бот запускается ===")
    except OSError as e:
        print(f"⚠️ Не могу писать журнал ошибок: {e}")
    if not config.TOKEN:
        print("❌ Нет DISCORD_TOKEN. Открой файл .env и вставь токен бота после DISCORD_TOKEN=")
        sys.exit(2)
    for presences in (True, False):
        bot = FamilyBot(presences=presences)
        try:
            bot.run(config.TOKEN, log_handler=None)
            logging.warning("=== бот остановлен штатно ===")
            return
        except discord.LoginFailure:
            print("❌ Неверный токен. Developer Portal → Bot → Reset Token, вставь новый в .env")
            sys.exit(2)
        except discord.PrivilegedIntentsRequired:
            if presences:
                print("⚠️ Presence Intent выключен — запускаюсь без автоанонсов стримов. Включить: Developer Portal → "
                      "Bot → Privileged Gateway Intents → Presence Intent → Save Changes, потом перезапусти бота.")
                continue
            print("❌ Включи интенты: Developer Portal → твоё приложение → Bot → Privileged Gateway Intents → "
                  "включи Presence, Server Members и Message Content → Save Changes")
            sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        logging.exception("Бот упал")
        raise
