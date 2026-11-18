# -*- coding: utf-8 -*-
"""Фаза F: инсайты — краткий нарратив по РЕАЛЬНЫМ цифрам семьи (активность, состав, заявки, рекрутинг, дисциплина).
collect() считает цифры из базы (никакого ИИ), render() — сухая сводка, narrate() — живой рассказ сильной моделью
поверх тех же цифр (smart; без ИИ — сухая сводка). ИИ запрещено добавлять числа и имена, которых нет в сводке."""
import datetime as dt
import logging

log = logging.getLogger("family-bot.insights")

TOPICS = ("activity", "recruit", "all")
SYSTEM = """Ты аналитик RP-семьи «{family}». Ниже — РЕАЛЬНЫЕ цифры бота за период. Напиши короткий живой разбор
(5–10 предложений или список): что хорошо, что проседает, на кого обратить внимание, 1–3 конкретных совета.
ПРАВИЛА: числа и имена — ТОЛЬКО из сводки, ничего не досчитывай и не придумывай; чего нет — не упоминай.
Без воды, по-русски, разметка Discord, до 1500 знаков."""


def _hours(sec):
    return round((sec or 0) / 3600, 1)


def collect(bot, guild, days=7, topic="all"):
    """→ словарь с цифрами (только из базы). days — период для заявок/выговоров/рекрутинга; активность — недельная."""
    db, gid = bot.db, guild.id
    days = max(1, min(90, int(days or 7)))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    out = {"days": days, "topic": topic if topic in TOPICS else "all"}
    members = bot.family_members(guild)
    ids = {m.id for m in members}
    ranks = {}
    for m in members:
        r = bot.rank_name(m) or "?"
        ranks[r] = ranks.get(r, 0) + 1
    out["members"] = len(members)
    out["ranks"] = ranks
    if out["topic"] in ("activity", "all"):
        rows = [r for r in db.all("SELECT user_id, week_voice, week_messages FROM activity WHERE guild_id=?", gid)
                if r["user_id"] in ids]
        out["week_voice_h"] = _hours(sum(r["week_voice"] or 0 for r in rows))
        out["week_messages"] = sum(r["week_messages"] or 0 for r in rows)
        active = {r["user_id"] for r in rows if (r["week_voice"] or 0) > 0 or (r["week_messages"] or 0) > 0}
        out["silent_week"] = len(ids - active)
        top = sorted(rows, key=lambda r: (r["week_voice"] or 0) / 60 + (r["week_messages"] or 0) * 2, reverse=True)[:5]
        out["top"] = [(getattr(guild.get_member(r["user_id"]), "display_name", None) or str(r["user_id"]),
                       _hours(r["week_voice"]), r["week_messages"] or 0) for r in top]
        out["warns"] = db.one("SELECT COUNT(*) n FROM warns WHERE guild_id=? AND created_at>=?", gid, since)["n"]
        try:
            from core.people import is_inactive
            out["inactive5"] = sum(1 for m in members if is_inactive(bot, guild, m, 5))
        except Exception:  # noqa: BLE001
            out["inactive5"] = None
    if out["topic"] in ("recruit", "all"):
        apps = {}
        for r in db.all("SELECT status, COUNT(*) n FROM applications WHERE guild_id=? AND created_at>=? GROUP BY status",
                        gid, since):
            apps[r["status"] or "?"] = r["n"]
        out["applications"] = apps
        rec = bot.get_cog("Recruit")
        try:
            out["recruit"] = rec.report_text(guild, days) if rec else None
        except Exception:  # noqa: BLE001
            out["recruit"] = None
    return out


APP_RU = {"exam": "на экзамене", "pending": "ждут решения", "accepted": "приняты", "denied": "отклонены",
          "interview": "на собеседовании"}


def render(d):
    """Сухая сводка по цифрам (её же видит ИИ)."""
    lines = [f"📊 **Цифры семьи** (заявки/выговоры — за {d['days']} дн., активность — за неделю)",
             f"👥 В семье: {d['members']} — " + ", ".join(f"{k}: {v}" for k, v in sorted(d["ranks"].items()))]
    if "week_voice_h" in d:
        lines.append(f"🎙 Войс за неделю: {d['week_voice_h']} ч · 💬 сообщений: {d['week_messages']} · "
                     f"молчали всю неделю: {d['silent_week']}" +
                     (f" · неактив 5+ дн.: {d['inactive5']}" if d.get("inactive5") is not None else ""))
        if d["top"]:
            lines.append("🏆 Топ недели: " + "; ".join(f"{n} — {h} ч, {m} сообщ." for n, h, m in d["top"]))
        lines.append(f"⚖️ Выговоров за период: {d['warns']}")
    if "applications" in d:
        apps = d["applications"]
        lines.append("📝 Заявки за период: " + (", ".join(f"{APP_RU.get(k, k)} — {v}" for k, v in apps.items())
                                               if apps else "не было"))
        if d.get("recruit"):
            lines.append(d["recruit"])
    return "\n".join(lines)


async def narrate(bot, guild, days=7, topic="all"):
    """Нарратив по цифрам: ИИ (smart) поверх render(); нет ИИ/ошибка — сама сводка."""
    d = collect(bot, guild, days, topic)
    facts = render(d)
    ai = bot.ai_for(guild)
    if not ai.on:
        return facts
    try:
        text = await ai.ask(SYSTEM.format(family=bot.family(guild).get("name")), facts, max_tokens=900, smart=True)
    except Exception as e:  # noqa: BLE001 — лимит/сеть: цифры всё равно покажем
        log.info("Инсайты: ИИ не ответил — %s", str(e)[:120])
        return facts
    text = (text or "").strip()
    return (text[:1500] + "\n\n-# По цифрам бота:\n" + facts[:350]) if text else facts
