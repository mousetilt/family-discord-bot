"""/семья проверка: всё, что мешает боту работать у семьи, — одним экраном и с кнопкой «исправить».

check(bot, guild) → [Item]. Только чтение: ничего не меняет, сеть не трогает (кроме кэша Discord).
fix — что предложить: profile · roles · build · ai · wizard · None (исправить можно только руками/словами).
"""
from dataclasses import dataclass

from . import config

OK, WARN, BAD = "ok", "warn", "bad"
ICON = {OK: "✅", WARN: "⚠️", BAD: "❌"}


@dataclass
class Item:
    level: str
    text: str
    fix: str = None


def check(bot, guild):
    from cogs.server_setup import CH_CATS, OPTIONAL_CHANNELS, audit  # ког импортируем лениво (core не зависит от cogs)
    db, out = bot.db, []
    feat = lambda key: bot.feature(guild, key)  # noqa: E731

    # 1. права и роль бота, дубли, интенты — общий аудит мастера
    lines, _ = audit(bot, guild)
    for ln in lines:
        lvl = BAD if ln.startswith("❌") else WARN if ln.startswith("⚠️") else OK
        if ln.startswith("💡"):
            continue
        out.append(Item(lvl, ln.lstrip("✅⚠️❌ ").strip()))

    # 2. собран ли сервер
    if not db.gget(guild, "role:leader"):
        out.append(Item(BAD, "Сервер ещё не собран: нет ролей и каналов семьи", "wizard"))
        return out

    # 3. профиль
    if not bot.is_home(guild) and not db.gget(guild, "family:server"):
        out.append(Item(WARN, "Профиль семьи не заполнен (название, игровой сервер, проект)", "profile"))

    # 4. роли семьи
    miss = [config.ROLE_NAMES.get(k, k) for k, *_ in config.ROLES if not bot.role(guild, k)]
    if miss:
        out.append(Item(WARN, f"Нет ролей ({len(miss)}): " + ", ".join(miss[:6]) + ("…" if len(miss) > 6 else ""),
                        "roles"))
    else:
        out.append(Item(OK, "Все роли семьи на месте"))

    # 5. каналы
    lost = []
    for cat in CH_CATS:
        for _, key, name, _ in cat["channels"]:
            if key in OPTIONAL_CHANNELS and not feat(OPTIONAL_CHANNELS[key]):
                continue
            if not bot.ch(guild, key):
                lost.append(name)
    if lost:
        out.append(Item(WARN, f"Нет каналов ({len(lost)}): " + ", ".join(lost[:5]) + ("…" if len(lost) > 5 else ""),
                        "build"))
    else:
        out.append(Item(OK, "Все каналы на месте"))

    # 6. ИИ
    ai = bot.ai_for(guild)
    if not ai.on:
        out.append(Item(BAD, "ИИ не подключён — Швейцар, юрист, ИИ-модерация и голос молчат. Ключ бесплатный", "ai"))
    else:
        out.append(Item(OK, f"ИИ: {ai.name}" + (f", запасные: {', '.join(ai.backups)}" if ai.backups else "")))
        if "groq" not in ai.keys:
            out.append(Item(WARN, "Нет ключа Groq — голосовые команды в войсе не работают (распознавание речи)", "ai"))
        if "gemini" not in ai.keys and feat("lawyer"):
            out.append(Item(WARN, "Нет ключа Gemini — юрист ищет только по словам (без смыслового поиска)", "ai"))
        if ai.is_claude and not ai.claude_limit:
            out.append(Item(WARN, "Claude без дневного лимита — расходы ничем не ограничены "
                                  "(/настройки → Числа → ИИ)"))

    # 7. модули, которым нужно что-то ещё
    from . import projects
    forum_ok = projects.forum_ok(db, guild.id)
    if feat("forum") and not forum_ok:
        out.append(Item(WARN, "Суды и жалобы включены, но для вашего проекта/сервера форум не читается: "
                              + projects.why_no_forum(db, guild.id), "profile"))
    if feat("forum") and forum_ok and not db.gget(guild, "forum:login"):
        out.append(Item(WARN, "Суды и жалобы включены, но нет аккаунта форума — разделы закрыты для гостей. "
                              "Владелец сервера: /форум вход"))
    if feat("forum") and forum_ok and db.gget(guild, "forum:auth_fail"):
        out.append(Item(BAD, "Форум не пускает аккаунт семьи — введите данные заново: /форум вход"))
    if feat("lawyer"):
        n = db.one("SELECT COUNT(*) AS n FROM kb_docs WHERE guild_id IN (?, 0)", guild.id)
        if not n or not n["n"]:
            out.append(Item(WARN, "База юриста пустая — загрузите правила и законы: /база"))
    if feat("treasury") and not bot.role(guild, "treasurer"):
        out.append(Item(WARN, "Казна включена, но нет роли казначея", "roles"))
    if feat("hr:nick") and not db.gget(guild, "nick:template"):
        out.append(Item(WARN, "Контроль ника включён, но шаблон не задан: /ник шаблон"))
    return out


def summary(items):
    bad = sum(i.level == BAD for i in items)
    warn = sum(i.level == WARN for i in items)
    if bad:
        return f"❌ Есть проблемы: {bad}" + (f", замечаний: {warn}" if warn else "")
    if warn:
        return f"⚠️ Работает, замечаний: {warn}"
    return "✅ Всё в порядке"


def render(items, limit=3900):
    order = {BAD: 0, WARN: 1, OK: 2}
    lines = [f"{ICON[i.level]} {i.text}" for i in sorted(items, key=lambda i: order[i.level])]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"
