# -*- coding: utf-8 -*-
"""Все настройки и тексты бота. Меняй здесь названия, каналы, станции радио и т.д."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _snowflake(name):
    """ID из .env: берём только цифры (защита от случайных букв/пробелов)."""
    raw = os.getenv(name, "") or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.strip() and digits != raw.strip():
        print(f"⚠️  В .env {name}='{raw.strip()}' есть лишние символы — использую {digits or 0}")
    return int(digits) if digits else 0


TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
GUILD_ID = _snowflake("GUILD_ID")
FAMILY = os.getenv("FAMILY_NAME", "Ульяновы")
FAMILY_GEN = os.getenv("FAMILY_NAME_GENITIVE", "Ульяновых")  # «семья Ульяновых»
GAME_SERVER = os.getenv("GAME_SERVER", "Арбатский")
# ИИ: работает с БЕСПЛАТНЫМ ключом Groq или Gemini (или локальной моделью Ollama). Без ключа ИИ просто выключен.
AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip().lower()  # groq | gemini | ollama | anthropic (пусто = авто)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()  # платный, необязательно
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_MODERATION = os.getenv("AI_MODERATION", "1") == "1"
AI_MOD_INTERVAL = _int("AI_MOD_INTERVAL", 60)  # ИИ проверяет сообщения пачкой раз в N секунд (экономит лимит)
AI_DAILY_LIMIT = _int("AI_DAILY_LIMIT", 900)
DB_PATH = str(BASE_DIR / os.getenv("DB_PATH", "family.db"))
ERR_LOG = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "errors.log")  # журнал ошибок

EXAM_QUESTIONS = _int("EXAM_QUESTIONS", 10)
EXAM_PASS = _int("EXAM_PASS", 7)
EXAM_RETRY_MIN = _int("EXAM_RETRY_MIN", 60)
EXAM_TIME_MIN = _int("EXAM_TIME_MIN", 12)
MENTOR_DAYS = _int("MENTOR_DAYS", 7)
MARKET_DAYS = _int("MARKET_DAYS", 7)
GATHER_UTC_OFFSET = _int("GATHER_UTC_OFFSET", 3)   # часовой пояс времени сборов: 3 = Москва
WARN_DAYS = _int("WARN_DAYS", 7)                   # выговор снимается сам через столько дней без новых (0 — никогда)
DIGEST_DAYS = _int("DIGEST_DAYS", 2)               # сводка Главе в ЛС раз в столько суток (0 — выключить)

ASSETS = BASE_DIR / "assets"
FONTS = ASSETS / "fonts"
DATA = BASE_DIR / "data"

COLOR = 0x7A1022
GOLD = 0xE8E2D6
DARK = 0x0A0A0A
GREEN = 0x43A047
RED = 0xE53935
FOOTER = f"Семья {FAMILY} · Россия Онлайн · {GAME_SERVER}"
LINE = "━━━━━━━━━━━━━━━━━━━━"

# ─────────────────────────────── РОЛИ ───────────────────────────────
# ключ, название, цвет, права, показывать отдельно
ROLES = [
    ("leader",   "👑 Глава семьи",            0xF2EEE6, {"administrator": True}, True),
    ("deputy",   "⭐ Заместитель главы",       0xE53935, {"manage_channels": True, "manage_roles": True, "kick_members": True,
                                                         "ban_members": True, "manage_messages": True, "moderate_members": True,
                                                         "move_members": True, "mute_members": True, "manage_nicknames": True,
                                                         "mention_everyone": True, "view_audit_log": True,
                                                         "manage_threads": True, "create_events": True}, True),
    ("live",     "🔴 В эфире",                 0xFF2D55, {}, True),
    ("council",  "🏛 Совет семьи",             0x8E24AA, {"manage_messages": True, "moderate_members": True, "move_members": True,
                                                         "mute_members": True, "manage_nicknames": True, "mention_everyone": True,
                                                         "manage_threads": True, "create_events": True}, True),
    ("hr_head",  "📋 Начальник отдела кадров", 0x1E88E5, {"manage_nicknames": True, "move_members": True, "moderate_members": True}, True),
    ("diplomat", "🕊 Дипломат",                0x00ACC1, {"move_members": True}, True),
    ("recruiter","📨 Рекрутер",                0x42A5F5, {"move_members": True}, True),
    ("events",   "🎉 Организатор мероприятий", 0xFB8C00, {"mention_everyone": True, "move_members": True, "create_events": True}, True),
    ("mentor",   "🎓 Наставник",               0x26A69A, {"move_members": True}, True),
    ("senior",   "🛡 Старший состав",          0x3949AB, {"create_events": True}, True),
    ("member",   "👥 Член семьи",              0x5C6BC0, {}, True),
    ("newbie",   "🆕 Новичок",                 0x66BB6A, {}, True),
    ("vacation", "🌙 В отпуске",               0x78909C, {}, False),
    ("friend",   "🤝 Друг семьи",              0x8D6E63, {}, True),
    ("guest",    "👤 Гость",                   0x9E9E9E, {}, False),
    ("n_gather", "🔔 Сборы",                   0x000000, {}, False),
    ("n_events", "🔔 Мероприятия",             0x000000, {}, False),
    ("n_news",   "🔔 Новости",                 0x000000, {}, False),
    ("n_market", "🔔 Рынок",                   0x000000, {}, False),
    ("n_streams","🔔 Эфиры",                   0x000000, {}, False),
]
ROLE_NAMES = {k: n for k, n, *_ in ROLES}

LEAD = ["leader", "deputy", "council"]
HR = LEAD + ["hr_head", "recruiter"]
DIPLO = LEAD + ["diplomat"]
ORGANIZERS = LEAD + ["events", "senior"]
ALLIANCE_NAME = os.getenv("ALLIANCE_NAME", "Альянс семей Арбатского")
ALLIANCE_COLOR = 0xB08D57
MOD_EXEMPT = LEAD + ["hr_head", "senior"]  # без автонаказаний модерации и анти-спама (остальные должности — как все)
MENTORS = LEAD + ["hr_head", "mentor"]
FAMILY_RANKS = ["leader", "deputy", "council", "hr_head", "diplomat", "recruiter", "events", "mentor", "senior",
                "member", "newbie"]
FAMILY_PLUS = FAMILY_RANKS + ["friend"]
CORE = [k for k in FAMILY_RANKS if k != "newbie"]  # семья без новичков на испытательном сроке
BASE_RANKS = {"newbie": "🆕 Новичок", "member": "👥 Член семьи", "senior": "🛡 Старший состав"}
STAFF_POSTS = {"recruiter": "📨 Рекрутер", "hr_head": "📋 Начальник отдела кадров", "diplomat": "🕊 Дипломат",
               "events": "🎉 Организатор мероприятий", "mentor": "🎓 Наставник", "council": "🏛 Совет семьи"}
# Должности раздают Глава и заместитель (любые), а Совет и старший состав — только «младшие»:
JUNIOR_POSTS = ["recruiter", "diplomat", "events", "mentor"]
POST_GIVERS = LEAD + ["senior"]
ROSTER_ORDER = ["leader", "deputy", "council", "hr_head", "diplomat", "recruiter", "events", "mentor", "senior",
                "member", "newbie"]

# ─────────────────────────────── СТРУКТУРА СЕРВЕРА ───────────────────────────────
# (тип, ключ, название, доп.: view/send — кто видит/пишет, topic, slowmode, limit)
STRUCTURE = [
    {"name": "📢 ИНФОРМАЦИЯ", "view": "all", "send": LEAD, "channels": [
        ("text", "rules", "📜・правила", {"topic": "Правила семьи. Незнание не освобождает от ответственности."}),
        ("text", "welcome", "👋・о-семье", {"topic": f"Кто такие {FAMILY} и как к нам попасть"}),
        ("text", "news", "📢・объявления", {}),
        ("text", "chronicle", "📖・летопись", {"topic": "Летопись семьи — бот записывает важные события сам"}),
        ("text", "roster", "👥・состав", {"topic": "Обновляется ботом автоматически"}),
        ("text", "selfroles", "🎭・уведомления", {}),
        ("text", "boosts", "💎・бусты", {"topic": "Поддержи семью бустом — прогресс, цели и Зал почёта"}),
        ("text", "streams", "🔴・эфиры", {"topic": "Стримы семьи — бот анонсирует сам, когда кто-то выходит в эфир"}),
        ("text", "appeals", "📬・руководству", {"topic": "Жалоба, вопрос или просьба к руководству — нажми кнопку. Можно анонимно."}),
    ]},
    {"name": "📝 ВСТУПЛЕНИЕ", "view": "all", "send": "all", "channels": [
        ("text", "apply", "📨・подать-заявку", {"send": LEAD, "topic": "Нажми кнопку, чтобы подать заявку в семью"}),
        ("text", "guest_q", "❓・вопросы-гостей", {"slowmode": 10}),
        ("voice", "waiting", "⏳ Ожидание собеседования", {}),
        ("voice", "interview1", "🎤 Собеседование 1", {"limit": 3, "connect": HR, "speak": "all"}),
        ("voice", "interview2", "🎤 Собеседование 2", {"limit": 3, "connect": HR, "speak": "all"}),
    ]},
    {"name": "🤝 ДИПЛОМАТИЯ", "key": "diplo_cat", "view": "all", "send": DIPLO, "channels": [
        ("text", "diplo_request", "🕊・переговоры", {"topic": "Представители других семей — нажмите кнопку, чтобы начать переговоры"}),
        ("text", "relations", "🌐・отношения", {"view": FAMILY_RANKS, "send": DIPLO}),
        ("text", "treaties", "📜・договоры", {"view": FAMILY_RANKS, "send": DIPLO}),
        ("text", "diplo_archive", "🗄・архив-переговоров", {"view": DIPLO, "send": DIPLO}),
        ("voice", "diplo_voice", "🤝 Переговорная", {}),
    ]},
    {"name": "💬 СЕМЬЯ", "view": FAMILY_RANKS, "send": "all", "channels": [
        ("text", "chat", "💬・общий-чат", {}),
        ("text", "flood", "😂・флуд-и-мемы", {}),
        ("text", "screens", "📸・скриншоты", {}),
        ("text", "clips", "🎬・клипы", {}),
        ("text", "gather", "📣・сборы", {"send": ORGANIZERS, "topic": "Сборы создаются командой /сбор"}),
        ("text", "ideas", "💡・предложения", {"topic": "Идеи для семьи. Ставьте 👍/👎"}),
        ("text", "achievements", "🏆・достижения", {"topic": "Хвастаемся: повышения, покупки, победы"}),
        ("text", "blacklist_ch", "⛔・чёрный-список", {"send": LEAD + ["hr_head"], "topic": "Люди, которых мы не принимаем. Ведётся командой /чс"}),
    ]},
    {"name": "🛒 РЫНОК", "view": FAMILY_PLUS, "send": LEAD, "channels": [
        ("text", "market", "🛒・объявления", {"topic": "Объявления создаются командой /продать. Торг — в ветке под объявлением"}),
        ("text", "market_chat", "💬・торг-и-вопросы", {"send": "all"}),
    ]},
    {"name": "🎓 ОБУЧЕНИЕ", "view": FAMILY_RANKS, "send": "all", "channels": [
        ("text", "guides", "📚・гайды", {"send": LEAD + ["mentor", "hr_head"]}),
        ("text", "mentoring", "🧑‍🏫・наставничество", {"send": MENTORS, "topic": "Личные ветки наставник ↔ новичок создаёт бот"}),
        ("text", "lawyer", "⚖️・юрист", {"topic": "Пиши вопрос — ИИ-юрист ответит по законам сервера и правилам проекта"}),
        ("text", "newbie_q", "❔・вопросы-новичков", {}),
        ("voice", "training", "🎓 Обучение", {}),
    ]},
    {"name": "🎙 ОБЩЕНИЕ", "view": FAMILY_PLUS, "send": "all", "channels": [
        ("text", "friends_chat", "🍻・с-друзьями", {"topic": "Общий чат семьи и друзей семьи — болтаем, зовём в войс, делимся новостями"}),
        ("text", "music_text", "🎶・музыка", {"topic": "/играть — музыка по ссылке или названию · /радио — радиостанции"}),
        ("voice", "creator", "➕ Создать комнату", {}),
        ("voice", "main_voice", "🏠 Общий сбор", {}),
        ("voice", "lounge", "🛋 Гостиная", {}),
        ("voice", "kitchen", "☕ Кухня", {}),
        ("voice", "balcony", "🚬 Балкон", {"limit": 4}),
        ("voice", "duo", "👫 На двоих", {"limit": 2}),
        ("voice", "music", "🎵 Музыка", {}),
        ("voice", "other_games", "🎮 Другие игры", {}),
        ("voice", "stream", "📺 Стримы и кино", {}),
        ("voice", "afk", "💤 AFK", {"speak": []}),
    ]},
    {"name": "🚗 В ИГРЕ", "view": FAMILY_RANKS, "send": "all", "channels": [
        ("voice", "trip1", "🚗 Выезд №1", {}),
        ("voice", "trip2", "🚗 Выезд №2", {}),
        ("voice", "trip3", "🚗 Выезд №3", {}),
        ("voice", "work", "💼 На работе", {}),
        ("voice", "event_voice", "🎯 Мероприятие", {"speak": ORGANIZERS + ["member", "newbie", "mentor", "recruiter", "hr_head", "diplomat"]}),
        ("voice", "business", "🤫 Дела семьи", {"limit": 5, "view": CORE}),
        ("voice", "silent", "🔇 Тихий режим", {}),
    ]},
    {"name": "🔊 ЛИЧНЫЕ КОМНАТЫ", "key": "temp_cat", "view": FAMILY_PLUS, "send": "all", "channels": []},
    {"name": "🗂 ОТДЕЛ КАДРОВ", "view": HR, "send": "all", "channels": [
        ("text", "review", "📥・заявки", {"topic": "Сюда приходят заявки с результатами экзамена. Жмите кнопки под карточкой."}),
        ("text", "hrlog", "📈・кадровый-журнал", {"topic": "Приёмы, повышения, увольнения, наставничество — пишет бот"}),
        ("text", "hr_chat", "🗣・обсуждение-кадров", {}),
        ("voice", "hr_voice", "🗂 Отдел кадров", {}),
    ]},
    {"name": "🔒 РУКОВОДСТВО", "view": LEAD, "send": "all", "channels": [
        ("text", "council_chat", "🏛・совет", {}),
        ("text", "modqueue", "🛡・модерация", {"topic": "ИИ-модерация: подозрительные сообщения с кнопками"}),
        ("text", "warns", "⚖️・выговоры", {}),
        ("text", "reports", "📊・отчёты-активности", {"topic": "Еженедельный отчёт бота"}),
        ("voice", "council_voice", "🏛 Совет семьи", {}),
    ]},
    {"name": "🛠 СЛУЖЕБНОЕ", "view": LEAD, "send": "all", "channels": [
        ("text", "logs", "🤖・логи", {}),
        ("text", "botcmd", "🔧・бот-команды", {}),
    ]},
]

# ─────────────────────────────── ТЕКСТЫ ───────────────────────────────
RULES_TEXT = f"""**1. Общие правила**
1.1. Уважение ко всем участникам. Оскорбления, травля, унижение — запрещены.
1.2. Запрещена дискриминация по национальности, религии, полу и т.п.
1.3. Запрещены реклама, спам, флуд вне #флуда, NSFW-контент, политика.
1.4. Запрещено сливать внутреннюю информацию семьи (скрины совета, планы, состав) третьим лицам.

**2. Поведение в игре**
2.1. Ты носишь фамилию {FAMILY} — веди себя так, чтобы её не позорить.
2.2. Соблюдай правила проекта «Россия Онлайн». Нарушение правил проекта = нарушение правил семьи.
2.3. Конфликты с другими семьями/организациями решаются через руководство, а не самостоятельно.
2.4. На сборы приходим вовремя. Не можешь — отметь «Не смогу».
2.5. Уходишь надолго — оформи отпуск командой `/отпуск`.
2.6. Сделки внутри семьи — через 🛒 рынок, честно. Кидалово = исключение и чёрный список.

**3. Иерархия**
3.1. Распоряжения Главы, Заместителя и Совета обязательны к исполнению.
3.2. Не согласен с решением — обсуди лично или в #предложения, но не устраивай публичных разборок.
3.3. Новичок проходит испытательный срок под присмотром наставника.

**4. Наказания**
Устное предупреждение → Выговор → Понижение → Исключение.
3 активных выговора = рассмотрение исключения из семьи.
Руководство вправе наказать за нарушения, не описанные прямо, если они вредят семье."""

WELCOME_TEXT = f"""Добро пожаловать на сервер семьи **{FAMILY}** — проект «Россия Онлайн», сервер **{GAME_SERVER}**.

**Как вступить**
1. Прочитай #📜・правила
2. Нажми кнопку в #📨・подать-заявку: анкета → короткий тест по правилам → 2 вопроса о себе
3. Дождись ответа рекрутера — бот напишет тебе в ЛС
4. Если вызовут на собеседование — зайди в «⏳ Ожидание собеседования»
5. После принятия за тобой закрепят наставника на испытательный срок

**Ранги**
🆕 Новичок — испытательный срок · 👥 Член семьи · 🛡 Старший состав
🎓 Наставник · 🎉 Организатор · 🕊 Дипломат · 📨 Рекрутер · 📋 Нач. отдела кадров
🏛 Совет · ⭐ Заместитель · 👑 Глава

**Полезное**
🔊 «➕ Создать комнату» — своя голосовая комната (`/комната ...`)
⚖️ #юрист — ИИ ответит по законам сервера и правилам проекта
🛒 `/продать` — объявление на семейном рынке
🎶 `/играть` и `/радио` — музыка в голосовом канале
🪪 `/профиль` — твой семейный паспорт"""

# ─────────────────────────────── ЭКЗАМЕН ───────────────────────────────
SITUATIONAL = [
    ("Тебя оскорбил игрок другой семьи", "Что сделаешь в игре и вне её?"),
    ("Предлагают вирты за реальные деньги", "Что ответишь и что сделаешь?"),
    ("Член семьи нарушает правила проекта", "Ты это видишь. Как поступишь?"),
    ("Чем ты будешь полезен семье?", "Навыки, опыт, онлайн, идеи"),
    ("Просят слить инфу о семье за деньги", "Твои действия?"),
    ("Не согласен с решением руководства", "Как будешь действовать?"),
]

MENTOR_CHECKLIST = [
    "Прочитал правила семьи",
    "Знает ООС-правила проекта (DM, MG, PG, NonRP…)",
    "Принят в семью в игре",
    "Посетил минимум 2 сбора",
    "Прошёл обучение с наставником в войсе",
    "Знает, к кому обращаться по вопросам",
]

# ─────────────────────────────── БАЗА ЗНАНИЙ ───────────────────────────────
KB_CATEGORIES = {"ooc": "📘 ООС-правила проекта", "laws": "⚖️ Законы сервера (IC)", "family": "🏛 Семья",
                 "other": "📎 Прочее"}
PROJECT_RULE_URLS = [
    "https://forum.russia.online/threads/osnovnyye-pravila-proyekta.1/",
    "https://forum.russia.online/threads/pravila-dlya-liderov-fraktsii.2/",
    "https://forum.russia.online/threads/pravila-perekhvata-postavok.3/",
    "https://forum.russia.online/threads/pravila-ogrableniya-bankov.4/",
    "https://forum.russia.online/threads/pravila-ograblenii-i-pokhishchenii.5/",
    "https://forum.russia.online/threads/pravila-ogrableniya-biznesov.6/",
    "https://forum.russia.online/threads/forumnyye-pravila.7/",
    "https://forum.russia.online/threads/pravila-proverki-na-storonneye-po.10/",
    "https://forum.russia.online/threads/pravila-voyennogo-polozheniya.14/",
    "https://forum.russia.online/threads/pravila-napadeniya-oborony-territorii-forta.15/",
    "https://forum.russia.online/threads/pravila-zakhvata-tsekhov-i-dilerov.1047/",
    "https://forum.russia.online/threads/pravila-predvybornoi-gonki.32010/",
    "https://forum.russia.online/threads/pravila-napadeniya-na-voyennuyu-bazu-pravila-postavok-pravila-vza.5327/",
]

# ─────────────────────────────── ДИПЛОМАТИЯ ───────────────────────────────
RELATIONS = {
    "ally": ("🤝", "Союз", 0x43A047),
    "pact": ("📜", "Пакт о ненападении", 0x26A69A),
    "neutral": ("⚪", "Нейтралитет", 0x9E9E9E),
    "tension": ("⚠️", "Напряжённость", 0xFB8C00),
    "war": ("⚔️", "Война", 0xE53935),
}

# ─────────────────────────────── РЫНОК ───────────────────────────────
MARKET_CATEGORIES = {
    "auto": "🚗 Транспорт", "realty": "🏠 Недвижимость", "business": "🏢 Бизнес", "clothes": "👕 Одежда",
    "acc": "💎 Аксессуары", "tuning": "🔧 Тюнинг", "items": "📦 Предметы", "services": "🛠 Услуги",
    "other": "❓ Другое",
}
MARKET_KINDS = {"sell": ("🟢", "Продаю", 0x43A047), "buy": ("🔵", "Куплю", 0x1E88E5), "trade": ("🟡", "Обмен", 0xFDD835)}

# ─────────────────────────────── РАДИО ───────────────────────────────
# Если станция перестала играть — просто замени ссылку на поток.
RADIO = {
    "Европа Плюс": "https://ep256.hostingradio.ru:8052/europaplus256.mp3",
    "Русское Радио": "https://rusradio.hostingradio.ru/rusradio96.aacp",
    "Радио Рекорд": "https://radiorecord.hostingradio.ru/rr_main96.aacp",
    "Record Russian Mix": "https://radiorecord.hostingradio.ru/rus96.aacp",
    "Record Phonk": "https://radiorecord.hostingradio.ru/phonk96.aacp",
    "Record Chill-Out": "https://radiorecord.hostingradio.ru/chil96.aacp",
    "Хит FM": "https://hitfm.hostingradio.ru/hitfm96.aacp",
    "DFM": "https://dfm.hostingradio.ru/dfm96.aacp",
    "Ретро FM": "https://retro.hostingradio.ru:8043/retro256.mp3",
}

EXTENSIONS = [
    "cogs.server_setup", "cogs.applications", "cogs.mentorship", "cogs.blacklist", "cogs.voice_rooms",
    "cogs.gatherings", "cogs.staff", "cogs.activity", "cogs.moderation", "cogs.lawyer", "cogs.chronicle",
    "cogs.diplomacy", "cogs.market", "cogs.security", "cogs.music", "cogs.context_menus",
    "cogs.streams", "cogs.appeals", "cogs.promo", "cogs.interviews", "cogs.alliance", "cogs.boosts", "cogs.assistant", "cogs.jarvis", "cogs.daily", "cogs.persona",
    "cogs.maintenance", "cogs.families", "cogs.control", "cogs.hub",
]
