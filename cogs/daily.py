# -*- coding: utf-8 -*-
"""Ежедневные дела бота.
• Резервная копия базы — раз в сутки владельцу сервера в ЛС (в базе есть авторы анонимных обращений, поэтому
  в каналы сервера её не кладём).
• Сводка Главе в ЛС раз в config.DIGEST_DAYS суток: заявки, обращения, испытательные сроки, неактив, выговоры.
• Выговоры снимаются сами через config.WARN_DAYS дней без новых выговоров."""
import asyncio
import datetime as dt
import io
import logging
import os
import sqlite3
import tempfile
import zipfile

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, parse_dt, safe_dm

log = logging.getLogger("family-bot.daily")
BACKUP_HOUR = 2    # UTC: 05:00 по Москве
DIGEST_HOUR = 7    # UTC: 10:00 по Москве
INACTIVE_DAYS = 5


def make_backup(path):
    """Целостная копия базы (даже если бот в этот момент пишет) → zip в памяти."""
    tmp = os.path.join(tempfile.gettempdir(), "family_backup.db")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "family.db")
    os.remove(tmp)
    buf.seek(0)
    return buf


class Daily(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hourly.start()

    def cog_unload(self):
        self.hourly.cancel()

    @tasks.loop(hours=1)
    @guarded
    async def hourly(self):
        for g in self.bot.home_guilds:
            for job in (self.expire_warns, self.backup, self.digest):
                try:
                    await job(g)
                except Exception:
                    log.exception("Ежедневная задача %s", job.__name__)

    @hourly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- резервная копия ----------
    async def owner(self, guild):
        if guild.owner:
            return guild.owner
        try:
            return await self.bot.fetch_user(guild.owner_id)
        except discord.HTTPException:
            return None

    async def send_backup(self, guild):
        owner = await self.owner(guild)
        if not owner:
            return False
        buf = await asyncio.to_thread(make_backup, config.DB_PATH)
        size = buf.getbuffer().nbytes
        if size > 9 * 1024 * 1024:
            return await safe_dm(owner, f"💾 База семьи {config.FAMILY} выросла до {size // 1024 // 1024} МБ — в ЛС уже не "
                                        "помещается. Скачай файл family.db с хостинга вручную.")
        name = f"family-{now().strftime('%Y-%m-%d')}.zip"
        return await safe_dm(owner, f"💾 Резервная копия базы семьи {config.FAMILY} за {now().strftime('%d.%m.%Y')}. "
                                    "Храни её у себя: если хостинг потеряет данные, базу можно вернуть из этого файла. "
                                    "Никому не пересылай — внутри заявки, выговоры и обращения.",
                             file=discord.File(buf, filename=name))

    async def backup(self, guild):
        t = now()
        key = f"backup:{t.date()}"
        if t.hour < BACKUP_HOUR or self.bot.db.gget(guild, key):
            return
        self.bot.db.gset(guild, key, 1)
        if not await self.send_backup(guild):
            await self.bot.log(guild, "💾 Не смог отправить владельцу сервера резервную копию базы — открой ЛС от "
                                      "участников сервера. Вручную: `/копия`.", color=0xFB8C00)

    @app_commands.command(name="копия", description="Прислать резервную копию базы бота в ЛС (только владелец сервера)")
    @app_commands.default_permissions(administrator=True)
    async def backup_cmd(self, inter: discord.Interaction):
        if inter.user.id != inter.guild.owner_id:
            return await inter.response.send_message("⛔ Копию базы получает только владелец сервера.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok = await self.send_backup(inter.guild)
        await inter.followup.send("💾 Отправил в ЛС." if ok else "❌ Не смог написать в ЛС — открой личные сообщения "
                                  "от участников сервера.", ephemeral=True)

    @app_commands.command(name="ошибки", description="Последние ошибки бота из журнала (руководство)")
    @app_commands.default_permissions(administrator=True)
    async def errors_cmd(self, inter: discord.Interaction):
        if inter.user.id != inter.guild.owner_id and not self.bot.has(inter.user, ["leader", "deputy"]):
            return await inter.response.send_message("⛔ Только для Главы и заместителя.", ephemeral=True)
        try:
            with open(config.ERR_LOG, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 60_000))
                data = f.read()
        except OSError:
            data = b""
        if not data.strip():
            return await inter.response.send_message("✅ Журнал ошибок пуст.", ephemeral=True)
        tail = data.decode("utf-8", "replace")[-1500:]
        await inter.response.send_message(f"Последние записи журнала (полный — файлом):\n```\n{tail}\n```", ephemeral=True,
                                          file=discord.File(io.BytesIO(data), filename="errors.log"))

    # ---------- авто-снятие выговоров ----------
    async def expire_warns(self, guild):
        if config.WARN_DAYS <= 0:
            return
        db = self.bot.db
        border = (now() - dt.timedelta(days=config.WARN_DAYS)).isoformat()
        rows = db.all("SELECT user_id, COUNT(*) n, MAX(created_at) last FROM warns WHERE guild_id=? AND active=1 "
                      "GROUP BY user_id HAVING MAX(created_at)<?", guild.id, border)
        for r in rows:
            db.q("UPDATE warns SET active=0 WHERE guild_id=? AND user_id=? AND active=1", guild.id, r["user_id"])
            await self.bot.log(guild, f"⚪ С <@{r['user_id']}> автоматически сняты выговоры ({r['n']}): "
                                      f"{config.WARN_DAYS} дн. без новых нарушений.", "warns")
            m = guild.get_member(r["user_id"])
            if m:
                await safe_dm(m, f"⚪ Твои выговоры в семье {config.FAMILY} сняты: {config.WARN_DAYS} дн. без новых "
                                 "нарушений. Так держать!")

    # ---------- сводка Главе ----------
    def build_digest(self, guild):
        bot, db, t = self.bot, self.bot.db, now()
        since = (t - dt.timedelta(days=max(config.DIGEST_DAYS, 1))).isoformat()
        e = bot.embed(f"🗞 Сводка по семье {config.FAMILY}", None, config.GOLD, guild)
        fam = bot.family_members(guild)
        gid = guild.id
        joined = db.one("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status='accepted' AND decided_at>?",
                        gid, since)["n"]
        e.description = f"В семье **{len(fam)}** чел." + (f" · новых за период: **{joined}**" if joined else "")

        apps = db.all("SELECT id, user_id, status, created_at FROM applications WHERE guild_id=? AND "
                      "status IN ('new','taken','interview') ORDER BY id", gid)
        if apps:
            names = {"new": "🟡 ждёт", "taken": "🔵 взята", "interview": "🟣 собеседование"}
            e.add_field(name=f"📥 Открытые заявки ({len(apps)})",
                        value="\n".join(f"№{a['id']} <@{a['user_id']}> — {names[a['status']]} · "
                                        f"<t:{int(parse_dt(a['created_at']).timestamp())}:R>" for a in apps[:8])[:1024],
                        inline=False)
        appeals = db.all("SELECT id, topic, created_at FROM appeals WHERE guild_id=? AND status='open' ORDER BY id", gid)
        if appeals:
            e.add_field(name=f"📬 Обращения без ответа ({len(appeals)})",
                        value="\n".join(f"№{a['id']} {a['topic']} · <t:{int(parse_dt(a['created_at']).timestamp())}:R>"
                                        for a in appeals[:8])[:1024], inline=False)
        soon = (t + dt.timedelta(days=2)).isoformat()
        ms = db.all("SELECT newbie_id, mentor_id, due_at FROM mentorships WHERE guild_id=? AND status='active' "
                    "AND due_at<? ORDER BY due_at", gid, soon)
        if ms:
            e.add_field(name=f"🎓 Испытательный срок: пора решать ({len(ms)})",
                        value="\n".join(f"<@{m['newbie_id']}> (наставник <@{m['mentor_id']}>) — "
                                        f"<t:{int(parse_dt(m['due_at']).timestamp())}:R>" for m in ms[:8])[:1024],
                        inline=False)
        vac = bot.role(guild, "vacation")
        border = t - dt.timedelta(days=INACTIVE_DAYS)
        idle = []
        for m in fam:
            if (vac and vac in m.roles) or m.voice:
                continue
            a = db.one("SELECT last_seen FROM activity WHERE guild_id=? AND user_id=?", gid, m.id)
            last = parse_dt(a["last_seen"]) if a and a["last_seen"] else None
            if not last or last < border:
                idle.append(m)
        if idle:
            e.add_field(name=f"💤 Не появлялись {INACTIVE_DAYS}+ дн. ({len(idle)})",
                        value=(", ".join(m.mention for m in idle[:15]) + ("…" if len(idle) > 15 else ""))[:1024]
                        + "\nПодробно: `/неактив`", inline=False)
        warns = db.all("SELECT user_id, COUNT(*) n FROM warns WHERE guild_id=? AND active=1 GROUP BY user_id "
                       "ORDER BY n DESC", gid)
        if warns:
            e.add_field(name=f"⚖️ С активными выговорами ({len(warns)})",
                        value=", ".join(f"<@{w['user_id']}> ×{w['n']}" for w in warns[:12])[:1024], inline=False)
        flags = db.one("SELECT COUNT(*) n FROM flags WHERE guild_id=? AND status='open'", gid)["n"]
        market = db.one("SELECT COUNT(*) n FROM market WHERE guild_id=? AND status='active'", gid)["n"]
        e.add_field(name="Прочее", value=f"🛡 Жалоб и флагов модерации без решения: **{flags}**\n"
                                         f"🛒 Объявлений на рынке: **{market}**\n"
                                         f"💎 Бустов сервера: **{guild.premium_subscription_count or 0}**", inline=False)
        if len(e.fields) == 1 and not flags:
            e.description += "\n\n✅ Всё разобрано: открытых заявок, обращений и просроченных сроков нет."
        e.set_footer(text=f"Сводка приходит раз в {config.DIGEST_DAYS} сут. · {config.FOOTER}")
        return e

    async def digest(self, guild):
        if config.DIGEST_DAYS <= 0:
            return
        t = now()
        last = self.bot.db.gget(guild, "digest:last")
        if t.hour < DIGEST_HOUR or (last and (t - parse_dt(last)).total_seconds() < config.DIGEST_DAYS * 86400 - 3600):
            return
        self.bot.db.gset(guild, "digest:last", now_iso())
        role = self.bot.role(guild, "leader")
        targets = [m for m in (role.members if role else []) if not m.bot]
        if not targets and guild.owner:
            targets = [guild.owner]
        e = self.build_digest(guild)
        for m in targets:
            await safe_dm(m, embed=e)

    @app_commands.command(name="сводка", description="Сводка по семье: заявки, обращения, сроки, неактив, выговоры")
    @app_commands.default_permissions(manage_messages=True)
    async def digest_cmd(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        await inter.response.send_message(embed=self.build_digest(inter.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Daily(bot))
