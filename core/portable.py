"""Экспорт и импорт настроек семьи файлом (/семья экспорт, /семья импорт).

В файл идёт только то, что имеет смысл на другом сервере: модули, числа, тексты, профиль, ставки, взносы, шаблон ника,
настройки форума, голос озвучки, прозвища и отказы от визитов. НЕ идут: ключи ИИ и любые секреты, ID ролей/каналов/
сообщений этого сервера (на новом сервере их создаст/привяжет мастер), служебные отметки «уже сделано».
"""
import json
import time

FORMAT = "family-bot-settings"
VERSION = 1
PREFIXES = ("mod:", "set:", "text:", "salary:", "recruit:rate:", "alias:", "persona:novisit:", "persona:catchup:", "relay:filter",
            "relay:mode", "relay:rss")
KEYS = {"family:name", "family:gen", "family:server", "family:title", "family:project", "nick:template",
        "probation:days", "tts:voice", "ai:primary", "dues:amount", "dues:period", "forum:titles", "forum:ignore",
        "forum:interval", "forum:surname", "music:autoplay"}
SECRET_WORDS = ("pass", "cookie", "token", "ai:groq", "ai:gemini", "ai:claude")
MAX_BYTES = 512 * 1024


def portable(key):
    if any(w in key for w in SECRET_WORDS):
        return False
    return key in KEYS or key.startswith(PREFIXES)


def export(db, gid, name=""):
    rows = db.all("SELECT key, value FROM guild_settings WHERE guild_id=? ORDER BY key", gid)
    data = {r["key"]: r["value"] for r in rows if portable(r["key"])}
    return {"format": FORMAT, "version": VERSION, "family": name, "exported": int(time.time()), "settings": data}


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def parse(raw: bytes):
    """Файл → {ключ: значение} (только переносимые). ValueError — с понятной причиной."""
    if len(raw) > MAX_BYTES:
        raise ValueError("файл слишком большой")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("это не файл настроек (не JSON)") from None
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("это не файл настроек бота")
    if int(payload.get("version") or 0) > VERSION:
        raise ValueError("файл от более новой версии бота — сначала обновите бота")
    data = payload.get("settings")
    if not isinstance(data, dict):
        raise ValueError("в файле нет настроек")
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and portable(k) and isinstance(v, (str, int, float)) and len(str(v)) <= 20000:
            out[k] = str(v)
    return out, str(payload.get("family") or "")[:60]


def apply(db, gid, data):
    """Записывает настройки (поверх текущих). Возвращает число записанных."""
    n = 0
    for k, v in data.items():
        if portable(k):
            db.gset(gid, k, v)
            n += 1
    return n


def describe(data):
    groups = {"mod:": "модули", "set:": "числа", "text:": "тексты", "family:": "профиль", "salary:": "зарплаты",
              "alias:": "прозвища"}
    counts = {}
    for k in data:
        name = next((v for p, v in groups.items() if k.startswith(p)), "прочее")
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{v}: {n}" for v, n in sorted(counts.items()))
