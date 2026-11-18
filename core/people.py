# -*- coding: utf-8 -*-
"""Гибкие вопросы-списки по семье: «кто без экзамена», «кто должен взнос», «кто в войсе», «кто с выговорами»…
Одно read-only действие ИИ `people` (cogs/assistant.py): ИИ заполняет фильтры, здесь — отбор СТРОГО по реальным
данным сервера (роли, БД по guild_id) и ответ шаблоном. Имена и цифры берутся только из посчитанного списка —
ИИ их не пишет, поэтому выдумать человека не может."""
import asyncio
import datetime as dt
import json
import random

import discord

from core import config
from core.botcore import now, parse_dt, safe_dm

INACTIVE_DAYS = 5   # «неактив» без числа — как отчёт «💤 Неактив»
LIMIT, LIMIT_MAX = 30, 60

HEAD_SOME = ["Ну, держи список, раз сам не знаешь:", "Вот они, голубчики:", "Записывай, дважды повторять не буду:",
             "Я тут всех знаю. Вот кто:", "Глаза разуй — вот они:"]
HEAD_NONE = ["Таких нет. Не выдумывай.", "Никого. Чисто, как у меня в каморке.", "Пусто. Ни одной души.",
             "Нету таких, хоть обыщись."]


def is_inactive(bot, guild, member, days=INACTIVE_DAYS):
    """Мерка «💤 Неактив»: отпускник или сидит в войсе — активен, иначе по last_seen (activity, по guild_id)."""
    vac = bot.role(guild, "vacation")
    if (vac and vac in member.roles) or member.voice:
        return False
    last = last_seen(bot, guild, member)
    return not last or last < now() - dt.timedelta(days=days)


def last_seen(bot, guild, member):
    r = bot.db.one("SELECT last_seen FROM activity WHERE guild_id=? AND user_id=?", guild.id, member.id)
    try:
        d = parse_dt(r["last_seen"]) if r and r["last_seen"] else None
    except (TypeError, ValueError):
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def week_elapsed_days(t=None):
    """Сколько дней прошло текущей «недели активности» (она обнуляется в воскресенье 18:00 UTC, cogs/activity.py)."""
    t = t or now()
    start = (t - dt.timedelta(days=(t.weekday() + 1) % 7)).replace(hour=18, minute=0, second=0, microsecond=0)
    if start > t:
        start -= dt.timedelta(days=7)
    return (t - start).total_seconds() / 86400


def at_risk(bot, guild, member):
    """«На грани неактива» → причина строкой или None. Ещё не неактив, но гаснет: давно не заходил, в войсе намного
    реже своего обычного, пропустил два последних сбора, на которые записывался. Без ИИ — только по данным."""
    vac = bot.role(guild, "vacation")
    if (vac and vac in member.roles) or is_inactive(bot, guild, member):
        return None
    why = []
    last = last_seen(bot, guild, member)
    if last and not member.voice and (now() - last).days >= 3:
        why.append(f"не заходил {(now() - last).days} дн.")
    r = bot.db.one("SELECT voice_seconds, week_voice FROM activity WHERE guild_id=? AND user_id=?", guild.id, member.id)
    joined = getattr(member, "joined_at", None)
    if r and joined:
        weeks = max(1.0, min(52.0, (now() - joined).days / 7))
        avg = (r["voice_seconds"] or 0) / weeks
        elapsed = week_elapsed_days()
        if avg >= 3600 and elapsed >= 2 and (r["week_voice"] or 0) < 0.3 * avg * elapsed / 7:
            why.append("в войсе втрое реже обычного")
    seen = 0
    for e in bot.db.all("SELECT data FROM events WHERE guild_id=? ORDER BY created_at DESC LIMIT 15", guild.id):
        try:
            d = json.loads(e["data"])
        except (TypeError, ValueError):
            continue
        if d.get("checked") is True and member.id in (d.get("yes") or []):
            seen += 1
            if member.id in (d.get("came") or []):
                break
            if seen == 2:
                why.append("не пришёл на 2 последних сбора, хотя записался")
                break
    return "; ".join(why) or None


def _bool(v):
    """true/false/«да»/«нет» от ИИ → True/False, иначе None (фильтр не задан)."""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower() if v is not None else ""
    return True if s in ("true", "1", "да", "yes") else False if s in ("false", "0", "нет", "no") else None


def _int(v, lo, hi):
    try:
        return None if v in (None, "", False, True) else max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return None


def _key(v, allowed):
    s = str(v or "").strip().lower()
    return s if s in allowed else None


def filters(a):
    """Сырые поля ИИ → чистые фильтры (мусор и пустые — выкидываем)."""
    f = {"rank": _key(a.get("rank"), config.ROLE_NAMES), "post": _key(a.get("post"), config.STAFF_POSTS),
         "exam": _bool(a.get("exam")), "inactive_days": _int(a.get("inactive_days"), 1, 365),
         "active": _bool(a.get("active")), "probation_done": _bool(a.get("probation_done")),
         "in_voice": _bool(a.get("in_voice")), "warns": _bool(a.get("warns")),
         "dues": _key(a.get("dues"), ("paid", "unpaid")), "at_risk": _bool(a.get("at_risk")),
         "in_family": _bool(a.get("in_family")) is not False,
         "limit": _int(a.get("limit"), 1, LIMIT_MAX) or LIMIT}
    return f


def criteria(f):
    """Фильтры словами — для заголовка и окна подтверждения."""
    out = []
    if f["rank"]:
        out.append(config.ROLE_NAMES.get(f["rank"], f["rank"]))
    if f["post"] and f["post"] != f["rank"]:
        out.append(config.STAFF_POSTS[f["post"]])
    if f["exam"] is not None:
        out.append("сдали экзамен" if f["exam"] else "без экзамена")
    if f["inactive_days"]:
        out.append(f"неактив {f['inactive_days']}+ дн.")
    if f["active"] is not None:
        out.append("активные" if f["active"] else f"неактив {INACTIVE_DAYS}+ дн.")
    if f["probation_done"] is not None:
        out.append("отбыли срок" if f["probation_done"] else "не отбыли срок")
    if f["in_voice"] is not None:
        out.append("в войсе" if f["in_voice"] else "не в войсе")
    if f["warns"] is not None:
        out.append("с выговорами" if f["warns"] else "без выговоров")
    if f["dues"]:
        out.append("сдали взнос" if f["dues"] == "paid" else "должны взнос")
    if f["at_risk"] is not None:
        out.append("на грани неактива" if f["at_risk"] else "не на грани")
    if not f["in_family"]:
        out.append("весь сервер")
    return ", ".join(out) or "вся семья"


def query(bot, guild, a):
    """→ {"rows": [(участник, пояснение)], "total": N, "notes": [...], "filters": f}. Только чтение, по guild_id."""
    f, notes = filters(a), []
    pool = bot.family_members(guild) if f["in_family"] else [m for m in guild.members if not m.bot]
    for key in (f["rank"], f["post"]):
        if key:
            role = bot.role(guild, key)
            pool = [m for m in pool if role and role in m.roles]
            if not role:
                notes.append(f"роли «{config.ROLE_NAMES.get(key, key)}» на сервере нет")
    # «отбыл срок» имеет смысл только у новичков
    if f["probation_done"] is not None and not f["rank"]:
        nb = bot.role(guild, "newbie")
        pool = [m for m in pool if nb and nb in m.roles]
    info = {m.id: [] for m in pool}
    men = bot.get_cog("Mentorship")

    def keep(pred):
        nonlocal pool
        pool = [m for m in pool if pred(m)]

    if f["exam"] is not None:
        if not bot.feature(guild, "hr:exam"):
            notes.append("экзамен в семье выключен — считаю, что сдали все")
        if men:
            keep(lambda m: men.exam_ok(guild, m.id) == f["exam"])
        else:
            notes.append("наставничество не загружено — экзамен не проверил")
    idle_days = f["inactive_days"] or (INACTIVE_DAYS if f["active"] is False else None)
    if idle_days:
        keep(lambda m: is_inactive(bot, guild, m, idle_days))
        if f["in_voice"]:
            notes.append("кто сейчас в войсе — тот уже не неактив (мерка «💤 Неактив»)")
    if f["active"] is True:
        keep(lambda m: not is_inactive(bot, guild, m, f["inactive_days"] or INACTIVE_DAYS))
    if idle_days or f["active"] is not None:
        for m in pool:
            last = last_seen(bot, guild, m)
            info[m.id].append(f"не был {(now() - last).days} дн." if last else "ни разу не видел")
    if f["probation_done"] is not None:
        if men:
            keep(lambda m: men.served(guild, m) == f["probation_done"])
            term = men.days(guild)
            for m in pool:
                start, _ = men.newbie_since(guild, m)
                if start:
                    info[m.id].append(f"в семье {(now() - start).days} дн. из {term}")
        else:
            notes.append("наставничество не загружено — срок не проверил")
    if f["in_voice"] is not None:
        keep(lambda m: bool(m.voice and m.voice.channel) == f["in_voice"])
        if f["in_voice"]:
            for m in pool:
                start = getattr(bot, "voice_sessions", {}).get((guild.id, m.id))
                mins = int((now() - start).total_seconds() // 60) if start else None
                info[m.id].append(f"🎙 {m.voice.channel.name}" + (f", {mins} мин" if mins is not None else ""))
    if f["warns"] is not None:
        cnt = {r["user_id"]: r["n"] for r in bot.db.all(
            "SELECT user_id, COUNT(*) n FROM warns WHERE guild_id=? AND active=1 GROUP BY user_id", guild.id)}
        keep(lambda m: bool(cnt.get(m.id)) == f["warns"])
        if f["warns"]:
            limit = bot.num(guild, "warn_limit")
            for m in pool:
                info[m.id].append(f"выговоров {cnt[m.id]}/{limit}")
    if f["dues"]:
        tr = bot.get_cog("Treasury")
        need = tr.dues(guild)[0] if tr else 0
        if not tr or not need:
            notes.append("членский взнос не настроен" if tr else "казна не загружена")
            pool = []
        else:
            from core.parse import fmt_amount
            owe = {m.id: got for m, got in tr.debtors(guild)}
            free = [r for r in (bot.role(guild, "vacation"), bot.role(guild, "newbie")) if r]
            if f["dues"] == "unpaid":
                keep(lambda m: m.id in owe)
                for m in pool:
                    info[m.id].append(f"сдал {fmt_amount(owe[m.id])} из {fmt_amount(need)}")
            else:
                keep(lambda m: m.id not in owe and not any(r in m.roles for r in free))
            if any(any(r in m.roles for r in free) for m in bot.family_members(guild)):
                notes.append("новички и отпускники взнос не платят")
    if f["at_risk"] is not None:
        risk = {m.id: at_risk(bot, guild, m) for m in pool}
        keep(lambda m: bool(risk[m.id]) == f["at_risk"])
        if f["at_risk"]:
            for m in pool:
                info[m.id].append(risk[m.id])
    pool.sort(key=lambda m: (bot.rank_index(m), m.display_name.lower()))
    return {"rows": [(m, "; ".join(info.get(m.id, []))) for m in pool], "total": len(pool), "notes": notes,
            "filters": f}


def render(res):
    """Ответ в характере, но имена и числа — только из res (шаблон, без ИИ)."""
    f, rows = res["filters"], res["rows"]
    head = f"👥 {criteria(f)} — {res['total']} чел.\n"
    if not rows:
        text = head + random.choice(HEAD_NONE)
    else:
        lines = [f"• {m.display_name}" + (f" — {extra}" if extra else "") for m, extra in rows[:f["limit"]]]
        if len(rows) > f["limit"]:
            lines.append(f"…и ещё {len(rows) - f['limit']}")
        text = head + random.choice(HEAD_SOME) + "\n" + "\n".join(lines)
    if res["notes"]:
        text += "\n-# " + " · ".join(res["notes"])
    return text


# ---------- что сделать с найденными (кнопки под ответом) ----------
ACTORS = config.LEAD + ["hr_head"]  # писать всем в ЛС и тегать списком — только руководство и кадры
MASS_MAX = 40


class MassDM(discord.ui.Modal, title="Написать им всем в ЛС"):
    text = discord.ui.TextInput(label="Текст", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, view):
        super().__init__()
        self.v = view

    async def on_submit(self, inter: discord.Interaction):
        g = inter.guild
        members = [m for m in (g.get_member(i) for i in self.v.ids[:MASS_MAX]) if m and not m.bot]
        await inter.response.send_message(f"✉️ Пишу {len(members)} чел…", ephemeral=True)
        ok = 0
        for m in members:
            if await safe_dm(m, f"✉️ От руководства семьи ({inter.user.display_name}):\n{self.text.value}"):
                ok += 1
            await asyncio.sleep(1)
        await inter.edit_original_response(content=f"✉️ Написал {ok} из {len(members)} (у остальных закрыта личка).")


class PeopleView(discord.ui.View):
    """Под ответом «кто…»: написать всем найденным в ЛС или упомянуть их в чате."""

    def __init__(self, author_id, ids):
        super().__init__(timeout=900)
        self.author_id, self.ids = author_id, list(ids)

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id or not inter.client.has(inter.user, ACTORS):
            await inter.response.send_message("⛔ Это может только тот, кто спрашивал, и только руководство/кадры.",
                                              ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Написать им в ЛС", emoji="✉️", style=discord.ButtonStyle.primary)
    async def dm(self, inter: discord.Interaction, _):
        await inter.response.send_modal(MassDM(self))

    @discord.ui.button(label="Упомянуть в чате", emoji="📣", style=discord.ButtonStyle.secondary)
    async def ping(self, inter: discord.Interaction, _):
        ids = self.ids[:MASS_MAX]
        await inter.response.send_message("📣 " + " ".join(f"<@{i}>" for i in ids),
                                          allowed_mentions=discord.AllowedMentions(users=True))
        self.stop()


def view_for(bot, author, res):
    """Кнопки действий — если есть кого и автор вправе (иначе None)."""
    if not res["rows"] or not bot.has(author, ACTORS):
        return None
    return PeopleView(author.id, [m.id for m, _ in res["rows"]])
