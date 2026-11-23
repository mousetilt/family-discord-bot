"""Игровые проекты: Россия Онлайн, Majestic RP и любой другой.

Проект — поле профиля семьи (family:project, /семья профиль). От него зависит:
• как бот называет игру в текстах и ИИ-промптах (вместо «Россия Онлайн» — название проекта; family.brand, AI.ask);
• правила проекта для юриста (/база правила-проекта) — только с открытых страниц;
• форум-монитор и сбор законов с форума — только там, где разделы форума известны и открыты для чтения.

Majestic RP: форум закрыт анти-DDoS-проверкой на JavaScript. Обходить такие проверки бот не будет (CLAUDE.md:
не скрейпить и не обходить антибот) — правила и законы семья загружает в базу юриста текстом или файлом (/база).
"""
import os
import re

from . import config

RO_RULES = list(config.PROJECT_RULE_URLS)

PROJECTS = {
    "ro": {
        "name": "Россия Онлайн",
        "game": "игра «Россия Онлайн»",
        "rules": RO_RULES,
        # серверы, для которых известны разделы форума (жалобы, суды, прокуратура, законы) — core/forum.SECTIONS
        "forum_servers": ["Арбатский"],
        "money": "₽",
    },
    "majestic": {
        "name": "Majestic RP",
        "game": "GTA V RP-проект «Majestic RP»",
        "rules": [],
        "forum_servers": [],
        "money": "$",
        "note": "Форум Majestic закрыт анти-DDoS-проверкой — бот его не читает. Правила и законы загрузите в базу "
                "юриста текстом или файлом: /база текст, /база файл.",
    },
    "other": {
        "name": "",  # название — как ввели в профиле
        "game": "RP-проект",
        "rules": [],
        "forum_servers": [],
        "money": "",
    },
}
ALIASES = {
    "ro": re.compile(r"росси|^ро$|russia|rus\s*online", re.I),
    "majestic": re.compile(r"maj|мадж|маж|мажест|меджест|маджест", re.I),
}
DEFAULT = os.getenv("PROJECT", "ro").strip().lower() or "ro"


def detect(text):
    """«Россия Онлайн» / «РО» → ro, «Majestic» / «маджестик» → majestic, иначе other (пусто — проект по умолчанию)."""
    t = (text or "").strip()
    if not t:
        return DEFAULT
    for key, rx in ALIASES.items():
        if rx.search(t):
            return key
    return "other"


def key_of(db, gid):
    raw = db.gget(gid, "family:project") if gid else None
    return detect(raw) if raw else DEFAULT


def info(db, gid):
    """Проект семьи: словарь из PROJECTS + key и name (для «other» — как ввели)."""
    key = key_of(db, gid)
    p = dict(PROJECTS.get(key, PROJECTS["other"]))
    p["key"] = key
    if not p["name"]:
        p["name"] = ((db.gget(gid, "family:project") if gid else None) or "RP-проект").strip()[:40]
        p["game"] = f"RP-проект «{p['name']}»"
    return p


def name(db, gid):
    return info(db, gid)["name"]


def forum_ok(db, gid, server=None):
    """Есть ли у семьи форум-монитор: проект с известными разделами и её игровой сервер среди них."""
    p = info(db, gid)
    if not p["forum_servers"]:
        return False
    if server is None:
        from .family import profile
        server = profile(db, gid)["server"]
    norm = lambda s: re.sub(r"\W+", "", (s or "").lower())  # noqa: E731
    return norm(server) in {norm(s) for s in p["forum_servers"]}


def why_no_forum(db, gid):
    p = info(db, gid)
    if p.get("note"):
        return p["note"]
    if p["forum_servers"]:
        return (f"Форум-монитор знает разделы только для сервера(ов) {', '.join(p['forum_servers'])} проекта "
                f"«{p['name']}». Для другого сервера разделы пока не настроены.")
    return f"Для проекта «{p['name']}» форум-монитор не настроен."


def rebrand(text, proj):
    """«Россия Онлайн» в тексте/промпте → проект семьи (для своего проекта — без изменений)."""
    if not text or not proj or proj.get("key") == "ro":
        return text
    text = text.replace("игра «Россия Онлайн»", proj["game"]).replace("RP-игры «Россия Онлайн»", proj["game"])
    text = text.replace("RP-проекте «Россия Онлайн»", f"проекте «{proj['name']}»")
    return text.replace("Россия Онлайн", proj["name"])
