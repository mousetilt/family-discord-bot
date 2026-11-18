# -*- coding: utf-8 -*-
"""Выгрузка данных семьи в CSV (открывается в Excel / Google Таблицах): /выгрузка что:…
• казна, штрафы — руководство и казна (config.TREASURY); активность, выговоры, заявки, журнал — руководство.
• Только данные этого сервера (guild_id). Файл — в ответ только тому, кто просил (ephemeral).
• Ключи ИИ, анонимные обращения и прочее служебное сюда не попадают — только перечисленные таблицы."""
import csv
import datetime as dt
import io
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config

KINDS = {
    "treasury": ("💰 Казна", config.TREASURY),
    "fines": ("🧾 Штрафы", config.TREASURY + ["hr_head"]),
    "activity": ("📊 Активность", config.LEAD + ["hr_head"]),
    "warns": ("⚖️ Выговоры", config.LEAD + ["hr_head"]),
    "applications": ("📨 Заявки", config.HR),
    "journal": ("📜 Журнал действий (30 дн.)", config.LEAD + ["hr_head"]),
}


def ts_text(v):
    """unix или ISO → «2026-11-18 20:15» (UTC)."""
    if v in (None, ""):
        return ""
    try:
        d = dt.datetime.fromtimestamp(int(v), dt.timezone.utc) if str(v).isdigit() else dt.datetime.fromisoformat(str(v))
        return d.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(v)


def rows_for(db, guild, kind):
    """(заголовок, строки) для CSV. Имена — как сейчас на сервере (ушедшие — по ID)."""
    gid = guild.id

    def name(uid):
        m = guild.get_member(int(uid)) if uid else None
        return m.display_name if m else (str(uid) if uid else "")

    if kind == "treasury":
        head = ["№", "Когда (UTC)", "Тип", "Сумма", "Участник", "Провёл", "Причина"]
        kinds = {"in": "приход", "out": "расход", "due": "взнос"}
        return head, [[r["id"], ts_text(r["ts"]), kinds.get(r["kind"], r["kind"]),
                       -r["amount"] if r["kind"] == "out" else r["amount"], name(r["user_id"]), name(r["actor_id"]),
                       r["reason"] or ""]
                      for r in db.all("SELECT * FROM treasury_tx WHERE guild_id=? ORDER BY id", gid)]
    if kind == "fines":
        head = ["№", "Когда", "Кому", "Сумма", "Причина", "Статус", "Выписал"]
        st = {0: "должен", 1: "оплачен", -1: "отменён"}
        return head, [[r["id"], ts_text(r["ts"]), name(r["user_id"]), r["amount"], r["reason"] or "",
                       st.get(r["paid"], r["paid"]), name(r["actor_id"])]
                      for r in db.all("SELECT * FROM fines WHERE guild_id=? ORDER BY id", gid)]
    if kind == "activity":
        head = ["Участник", "ID", "Войс всего, ч", "Сообщений всего", "Войс за неделю, ч", "Сообщений за неделю",
                "Последний раз в сети"]
        return head, [[name(r["user_id"]), r["user_id"], round((r["voice_seconds"] or 0) / 3600, 1), r["messages"] or 0,
                       round((r["week_voice"] or 0) / 3600, 1), r["week_messages"] or 0, ts_text(r["last_seen"])]
                      for r in db.all("SELECT * FROM activity WHERE guild_id=? ORDER BY voice_seconds DESC", gid)]
    if kind == "warns":
        head = ["№", "Когда", "Кому", "Выдал", "Причина", "Активен"]
        return head, [[r["id"], ts_text(r["created_at"]), name(r["user_id"]), name(r["moderator_id"]), r["reason"] or "",
                       "да" if r["active"] else "нет"]
                      for r in db.all("SELECT * FROM warns WHERE guild_id=? ORDER BY id", gid)]
    if kind == "applications":
        head = ["№", "Подана", "Кандидат", "Ник в игре", "Static", "Статус", "Рекрутёр", "Решение", "Причина отказа"]
        return head, [[r["id"], ts_text(r["created_at"]), name(r["user_id"]), r["nick"] or "", r["static_id"] or "",
                       r["status"], name(r["reviewer_id"]), ts_text(r["decided_at"]), r["deny_reason"] or ""]
                      for r in db.all("SELECT * FROM applications WHERE guild_id=? ORDER BY id", gid)]
    if kind == "journal":
        head = ["Когда", "Раздел", "Участник", "Кто сделал", "Что"]
        since = int(time.time()) - 30 * 86400
        try:
            rows = db.all("SELECT * FROM member_log WHERE guild_id=? AND ts>=? ORDER BY id", gid, since)
        except Exception:  # noqa: BLE001 — журнал ещё не создан
            rows = []
        return head, [[ts_text(r["ts"]), r["kind"], name(r["user_id"]), name(r["actor_id"]), r["text"] or ""]
                      for r in rows]
    return [], []


def to_csv(head, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # «;» — Excel в русской локали открывает сразу по столбцам
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel не ломал кириллицу


class Export(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="выгрузка", description="Скачать данные семьи в CSV (Excel): казна, активность, выговоры…")
    @app_commands.rename(kind="что")
    @app_commands.choices(kind=[app_commands.Choice(name=v[0], value=k) for k, v in KINDS.items()])
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def export(self, inter: discord.Interaction, kind: app_commands.Choice[str]):
        title, who = KINDS[kind.value]
        if not (inter.user.id == inter.guild.owner_id or self.bot.has(inter.user, who)):
            return await inter.response.send_message("⛔ Эти данные выгружает руководство.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        head, rows = rows_for(self.bot.db, inter.guild, kind.value)
        if not rows:
            return await inter.followup.send(f"{title}: пока пусто.", ephemeral=True)
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        await inter.followup.send(f"{title}: {len(rows)} строк.",
                                  file=discord.File(io.BytesIO(to_csv(head, rows)), filename=f"{kind.value}-{day}.csv"),
                                  ephemeral=True)


async def setup(bot):
    await bot.add_cog(Export(bot))
