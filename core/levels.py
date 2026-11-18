# -*- coding: utf-8 -*-
"""Уровни, очки и достижения семьи — только из «настоящей» активности, без флуда сообщениями:
• войс — 1 очко за 10 минут; явка на сбор (бот проверил войс) — 50; сданный взнос — 30 за операцию;
  стаж — 2 в день (с принятия в семью, иначе со входа на сервер); активный выговор — минус 100.
• уровень = ⌊√(очки / 100)⌋: 1 — 100 очков, 2 — 400, 3 — 900, 5 — 2500…
• магазин тратит очки (заказы в shop_orders) — уровень от покупок не падает.
Всё по guild_id: считаем по одной семье за раз (пачкой — без запроса на каждого человека)."""
import json
import math

from .botcore import now, parse_dt

ACHIEVEMENTS = {  # ключ → (эмодзи, название, проверка по статам)
    "month": ("🌱", "Месяц в семье", lambda s: s["days"] >= 30),
    "year": ("🎖", "Год в семье", lambda s: s["days"] >= 365),
    "voice100": ("🎙", "100 часов в войсе", lambda s: s["voice_h"] >= 100),
    "voice500": ("📻", "500 часов в войсе", lambda s: s["voice_h"] >= 500),
    "gather10": ("📣", "10 сборов", lambda s: s["came"] >= 10),
    "gather50": ("🏟", "50 сборов", lambda s: s["came"] >= 50),
    "dues1": ("💰", "Первый взнос", lambda s: s["dues"] >= 1),
    "dues20": ("🏦", "20 взносов", lambda s: s["dues"] >= 20),
    "lvl5": ("⭐", "5 уровень", lambda s: s["level"] >= 5),
    "lvl10": ("🌟", "10 уровень", lambda s: s["level"] >= 10),
}


def level_of(xp):
    return int(math.sqrt(max(0, xp) / 100))


def next_at(level):
    return (level + 1) ** 2 * 100


def guild_stats(bot, guild, members=None):
    """{user_id: {"xp","level","voice_h","came","dues","days","warns","spent","points"}} для людей семьи."""
    db, gid = bot.db, guild.id
    members = members if members is not None else bot.family_members(guild)
    voice = {r["user_id"]: r["voice_seconds"] or 0 for r in db.all(
        "SELECT user_id, voice_seconds FROM activity WHERE guild_id=?", gid)}
    came = {}
    for r in db.all("SELECT data FROM events WHERE guild_id=?", gid):
        try:
            d = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        if d.get("checked") is True:
            for u in d.get("came") or []:
                came[u] = came.get(u, 0) + 1
    try:
        dues = {r["user_id"]: r["n"] for r in db.all(
            "SELECT user_id, COUNT(*) n FROM treasury_tx WHERE guild_id=? AND kind='due' GROUP BY user_id", gid)}
    except Exception:  # noqa: BLE001 — казны ещё нет
        dues = {}
    warns = {r["user_id"]: r["n"] for r in db.all(
        "SELECT user_id, COUNT(*) n FROM warns WHERE guild_id=? AND active=1 GROUP BY user_id", gid)}
    joined = {}
    for r in db.all("SELECT user_id, MIN(decided_at) d FROM applications WHERE guild_id=? AND status='accepted' "
                    "GROUP BY user_id", gid):
        try:
            joined[r["user_id"]] = parse_dt(r["d"]) if r["d"] else None
        except (TypeError, ValueError):
            pass
    try:
        spent = {r["user_id"]: r["s"] or 0 for r in db.all(
            "SELECT user_id, SUM(price) s FROM shop_orders WHERE guild_id=? AND status<>'rejected' GROUP BY user_id", gid)}
    except Exception:  # noqa: BLE001 — магазина ещё нет
        spent = {}
    out, t = {}, now()
    for m in members:
        start = joined.get(m.id) or getattr(m, "joined_at", None)
        days = max(0, (t - start).days) if start else 0
        xp = (voice.get(m.id, 0) // 600 + came.get(m.id, 0) * 50 + dues.get(m.id, 0) * 30 + days * 2
              - warns.get(m.id, 0) * 100)
        xp = max(0, int(xp))
        out[m.id] = {"xp": xp, "level": level_of(xp), "voice_h": voice.get(m.id, 0) / 3600, "came": came.get(m.id, 0),
                     "dues": dues.get(m.id, 0), "days": days, "warns": warns.get(m.id, 0),
                     "spent": int(spent.get(m.id, 0)), "points": max(0, xp - int(spent.get(m.id, 0)))}
    return out


def earned(stats):
    return [k for k, (_, _, ok) in ACHIEVEMENTS.items() if ok(stats)]


def bar(xp):
    lv = level_of(xp)
    lo, hi = lv ** 2 * 100, next_at(lv)
    filled = int(10 * (xp - lo) / max(1, hi - lo))
    return "▰" * filled + "▱" * (10 - filled)
