# -*- coding: utf-8 -*-
"""Профиль семьи на конкретном сервере: название, родительный падеж, игровой сервер, «титул» для пиара.
Хранится в guild_settings (family:*); чего нет — берётся из окружения (config), т.е. у домашней семьи всё как было.
brand() подставляет профиль в любой текст, написанный под семью по умолчанию."""
from . import config

DEFAULT_TITLE = "Ульяновская Династия"
FIELDS = {  # ключ: (подпись, значение по умолчанию)
    "name": ("Название семьи", config.FAMILY),
    "gen": ("Родительный падеж («семья …»)", config.FAMILY_GEN),
    "server": ("Игровой сервер", config.GAME_SERVER),
    "title": ("Титул для пиара", DEFAULT_TITLE),
}


# Модули, которые семья может выключить в /настройки (по умолчанию всё включено)
FEATURES = {
    "ai_chat": "🎩 Швейцар отвечает в чате (ИИ-помощник)",
    "ai_moderation": "🛡 ИИ-модерация сообщений",
    "lawyer_channel": "⚖️ ИИ-юрист отвечает в канале и на упоминания",
    "voice_greet": "👋 Швейцар встречает входящих в войс",
    "voice_visit": "🫂 Швейцар проведывает тех, кто сидит один",
    "digest": "🗞 Сводка Главе в ЛС",
    "backup": "💾 Ежедневная копия базы владельцу",
    "banner": "🖼 Авто-баннер сервера",
    "weekly_report": "📊 Еженедельный отчёт активности",
}


def feature(db, gid, key):
    return (db.gget(gid, f"feat:{key}", "1") if gid else "1") != "0"


def profile(db, gid):
    """{'name','gen','server','title'} для сервера gid (int)."""
    out = {}
    for k, (_, default) in FIELDS.items():
        v = db.gget(gid, f"family:{k}") if gid else None
        out[k] = (v or "").strip() or default
    return out


def brand(db, gid, text):
    """Текст «под Ульяновых» → под семью этого сервера. Для семьи по умолчанию — без изменений."""
    if not text or not isinstance(text, str) or not gid:
        return text
    p = profile(db, gid)
    pairs = [(DEFAULT_TITLE, p["title"]), (config.FAMILY_GEN, p["gen"]), (config.FAMILY, p["name"]),
             (config.GAME_SERVER, p["server"])]
    for old, new in pairs:
        if old and old != new:
            text = text.replace(old, new)
    return text
