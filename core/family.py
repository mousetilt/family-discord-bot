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


# Дерево настроек (/настройки, шаг «Модули» мастера): модуль → его отдельные действия.
# Хранение: mod:<модуль> — рубильник всего модуля, mod:<модуль>:<действие> — отдельное действие ("1"/"0").
# Новую отключаемую фичу — добавить сюда и проверить в коде: bot.feature(guild, "модуль:действие").
MODULES = {
    "assistant": ("🎩 Швейцар в чате", {
        "chat": "Отвечает в чате (ИИ-помощник)",
    }),
    "persona": ("🧠 Характер Швейцара", {
        "greet": "Встречает входящих в войс",
        "visit": "Проведывает тех, кто сидит один",
        "tease": "Подколы и дерзость (выкл — вежливый)",
        "memory": "Помнит людей: досье, обиды, прозвища",
    }),
    "moderation": ("🛡 Модерация", {
        "antispam": "Анти-спам: мут за флуд и масс-пинги",
        "invites": "Чужие приглашения (реклама)",
        "scam": "Скам и фишинг-ссылки",
        "realmoney": "Сделки за реальные деньги",
        "ai": "ИИ-проверка сообщений",
        "autopunish": "Авто-наказания (удаление + мут за тяжёлое)",
    }),
    "treasury": ("💰 Казна", {
        "dues": "Членские взносы и должники",
        "salary": "Зарплаты из казны",
        "fines": "Штрафы",
        "remind": "Напоминания в ЛС: штрафы (раз в день) и взнос (за 2 дня до конца периода)",
    }),
    "hr": ("🗂 Кадры", {
        "exam": "Экзамен при вступлении (выкл — анкета сразу к рекрутёрам)",
        "auto": "Авто-перевод новичка в Члены семьи после срока (выкл — спросит)",
        "remind": "Напоминания наставнику по ходу испытательного срока",
        "nick": "Контроль ника по шаблону (/ник шаблон): ЛС, если ник не по форме",
    }),
    "auto": ("🤖 Автоматизация", {
        "search": "Поиск в интернете для ответов Швейцара",
        "missed": "«Что я пропустил» — ИИ-сводка за часы",
        "drafts": "ИИ-черновики объявлений, ивентов, пиар-постов",
        "schedule": "Отложенные объявления",
        "relay": "Ретранслятор внешних анонсов в новости (/ретранслятор)",
    }),
    "garage": ("🚗 Автопарк", {
        "log": "Реестр машин семьи, журнал и канал автопарка",
    }),
    "lawyer": ("⚖️ ИИ-юрист", {
        "channel": "Отвечает в канале юриста и на упоминания",
    }),
    "reports": ("📊 Отчёты", {
        "digest": "Сводка Главе в ЛС",
        "weekly": "Еженедельный отчёт активности",
    }),
    "journal": ("📜 Журнал действий", {
        "voice": "Вход, выход и переходы по войсам",
        "mic": "Микрофон, звук, камера, стрим (и серверный мут)",
        "members": "Ники, роли, аватары, тайм-ауты, вход/выход с сервера",
        "messages": "Изменённые и удалённые сообщения",
        "server": "Каналы, роли, права, приглашения, баны, кики (журнал аудита)",
    }),
    "service": ("🛠 Служебное", {
        "backup": "Ежедневная копия базы владельцу",
        "watchdog": "Сторож: ЛС Главе, если ИИ/голос/связь лежат дольше 10 минут",
    }),
}
# Числа семьи (/настройки → «🔢 Числа»): ключ set:<имя> в guild_settings → (подпись, по умолчанию, мин, макс).
# Раньше это были переменные окружения хоста — одни на все семьи, и семья не могла их поменять без кода.
NUMBERS = {
    "warn_limit": ("Выговоров до разбора исключения", 3, 1, 10),
    "warn_days": ("Выговор сгорает через, дней (0 — никогда)", config.WARN_DAYS, 0, 365),
    "digest_days": ("Сводка Главе раз в, дней (0 — выкл)", config.DIGEST_DAYS, 0, 30),
    "market_days": ("Объявление на рынке живёт, дней", config.MARKET_DAYS, 1, 60),
    "probation_days": ("Испытательный срок, дней", config.MENTOR_DAYS, 1, 60),
    "exam_questions": ("Вопросов в экзамене", config.EXAM_QUESTIONS, 3, 30),
    "exam_pass": ("Нужно верных ответов", config.EXAM_PASS, 1, 30),
    "exam_time": ("Время на экзамен, минут", config.EXAM_TIME_MIN, 3, 60),
    "exam_retry": ("Пересдача через, минут", config.EXAM_RETRY_MIN, 0, 10080),
    "journal_days": ("Журнал действий хранится, дней", 30, 1, 180),
}
NUMBER_GROUPS = {"discipline": ("⚖️ Дисциплина и сроки", ["warn_limit", "warn_days", "probation_days", "digest_days",
                                                          "market_days", "journal_days"]),
                 "exam": ("🧠 Экзамен", ["exam_questions", "exam_pass", "exam_time", "exam_retry"])}
# Тексты семьи (/настройки → «📜 Тексты»): ключ text:<имя>. Пусто — текст по умолчанию из config (под семью).
TEXTS = {
    "rules": ("📜 Правила семьи", "Текст правил: канал правил и база ИИ-юриста"),
    "welcome": ("🏛 О семье", "Текст канала «о семье»: кто мы и как вступить"),
    "checklist": ("🎓 Чек-лист наставника", "Пункты испытательного срока — по одному в строке (до 20)"),
    "situational": ("💬 Вопросы анкеты", "Вопросы о себе — по одному в строке: «Вопрос | подсказка» (из них берутся 2)"),
}


def number(db, gid, key):
    """Число семьи из /настройки (или значение по умолчанию), с ограничением диапазона."""
    _, default, lo, hi = NUMBERS[key]
    raw = db.gget(gid, f"set:{key}") if gid else None
    if raw is None and key == "probation_days" and gid:
        raw = db.gget(gid, "probation:days")  # старый ключ (/наставник срок)
    try:
        return max(lo, min(hi, int(raw))) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def text(db, gid, key):
    """Текст семьи (или None — тогда берётся текст по умолчанию)."""
    v = db.gget(gid, f"text:{key}") if gid else None
    return v if v and v.strip() else None


def checklist(db, gid):
    t = text(db, gid, "checklist")
    items = [ln.strip(" •-–\t") for ln in t.splitlines() if ln.strip(" •-–\t")] if t else []
    return items[:20] or list(config.MENTOR_CHECKLIST)


def situational(db, gid):
    t = text(db, gid, "situational")
    out = []
    for ln in (t.splitlines() if t else []):
        q, _, hint = ln.strip(" •-–\t").partition("|")
        if q.strip():
            out.append((q.strip()[:45], (hint.strip() or "Ответь честно и развёрнуто")[:100]))
    return out if len(out) >= 2 else list(config.SITUATIONAL)


DEFAULT_OFF = {"hr:auto", "hr:nick", "auto:relay"}  # "модуль" или "модуль:действие", которые по умолчанию выключены

# старые ключи feat:* (до дерева) → новое место; старые значения читаются, если новое не задано
LEGACY = {
    "ai_chat": ("assistant", "chat"),
    "ai_moderation": ("moderation", "ai"),
    "lawyer_channel": ("lawyer", "channel"),
    "voice_greet": ("persona", "greet"),
    "voice_visit": ("persona", "visit"),
    "digest": ("reports", "digest"),
    "backup": ("service", "backup"),
    "weekly_report": ("reports", "weekly"),
}
_LEGACY_BACK = {v: k for k, v in LEGACY.items()}
# совместимость: плоский список «ключ: подпись» старых модулей
FEATURES = {k: f"{MODULES[m][0]} · {MODULES[m][1][a]}" for k, (m, a) in LEGACY.items()}


def split_key(key):
    """«persona:visit» / «persona» / старый «voice_visit» → (модуль, действие|None)."""
    key = str(key or "").removeprefix("mod:")
    if key in LEGACY:
        return LEGACY[key]
    mod, _, act = key.partition(":")
    return mod, (act or None)


def _on(v, default):
    return (default if v is None else v) != "0"


def module_on(db, gid, mod):
    return _on(db.gget(gid, f"mod:{mod}"), "0" if mod in DEFAULT_OFF else "1")


def action_on(db, gid, mod, act):
    """Только флаг действия (без рубильника модуля)."""
    v = db.gget(gid, f"mod:{mod}:{act}")
    if v is None and (mod, act) in _LEGACY_BACK:
        v = db.gget(gid, f"feat:{_LEGACY_BACK[(mod, act)]}")
    return _on(v, "0" if f"{mod}:{act}" in DEFAULT_OFF else "1")


def feature(db, gid, key):
    """Включено ли у семьи: «модуль», «модуль:действие» или старый ключ feat:*. Без сервера — всё включено."""
    if not gid:
        return True
    mod, act = split_key(key)
    if mod not in MODULES:
        return _on(db.gget(gid, f"feat:{key}"), "1")
    if not module_on(db, gid, mod):
        return False
    return act is None or action_on(db, gid, mod, act)


def set_module(db, gid, mod, on):
    db.gset(gid, f"mod:{mod}", "1" if on else "0")


def set_action(db, gid, mod, act, on):
    db.gset(gid, f"mod:{mod}:{act}", "1" if on else "0")


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
