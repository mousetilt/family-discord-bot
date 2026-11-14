# -*- coding: utf-8 -*-
"""Летопись семьи: бот сам записывает важные события, а раз в месяц пишет «главу» в летописном стиле."""
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, MONTHS_NOM, now, now_iso, parse_dt, ru_date, staff_only

log = logging.getLogger("family-bot.chronicle")
MILESTONES = [5, 10, 15, 25, 50, 75, 100, 150, 200, 300]
ANNIVERSARIES = {30: "месяц", 182: "полгода", 365: "год", 730: "два года", 1095: "три года"}


class Chronicle(commands.Cog):
    group = app_commands.Group(name="летопись", description="Летопись семьи", default_permissions=discord.Permissions(create_events=True))

    def __init__(self, bot):
        self.bot = bot
        self.daily.start()

    def cog_unload(self):
        self.daily.cancel()

    async def record(self, guild, kind, text, user_id=None, image=None):
        bot = self.bot
        text = bot.brand(guild, text)
        bot.db.q("INSERT INTO chronicle(guild_id, kind, text, user_id, created_at) VALUES(?,?,?,?,?)", guild.id, kind,
                 text, user_id, now_iso())
        c = bot.ch(guild, "chronicle")
        if not c:
            return
        e = bot.embed(f"📖 {ru_date(now())}", text, config.GOLD, guild)
        if image:
            e.set_image(url=image)
        try:
            await c.send(embed=e)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_family_event(self, guild, kind, data):
        m = data.get("member")
        text = None
        # приём новичков и конец испытательного срока в летопись не пишем — только достижения семьи
        if kind == "rank" and m and data.get("rank") == "senior":
            text = f"🛡 {m.mention} возведён в Старший состав семьи."
        elif kind == "post" and m and data.get("added"):
            text = f"🎖 {m.mention} назначен на должность: **{config.STAFF_POSTS.get(data.get('post'), data.get('post'))}**."
        elif kind == "relation":
            emoji, name, _ = config.RELATIONS.get(data.get("status"), ("", "", 0))
            if data.get("status") in ("ally", "pact", "war"):
                text = f"{emoji} С семьёй **{data.get('family')}**: {name.lower()}."
            elif data.get("status") == "neutral" and data.get("previous") == "war":
                text = f"🕊 Война с семьёй **{data.get('family')}** окончена."
        elif kind == "treaty":
            text = f"📜 Заключён договор «{data.get('title')}» с семьёй **{data.get('family')}**."
        if text:
            await self.record(guild, kind, text, m.id if m else None)
        if kind == "accepted":
            await self.check_milestone(guild)

    async def check_milestone(self, guild):
        n = len(self.bot.family_members(guild))
        for t in MILESTONES:
            if n >= t and not self.bot.db.gget(guild, f"milestone:{t}"):
                self.bot.db.gset(guild, f"milestone:{t}", 1)
                await self.record(guild, "milestone", f"🎉 Нас уже **{t}**! Семья {config.FAMILY_GEN} растёт.")

    # ---------- ежедневное ----------
    @tasks.loop(hours=6)
    @guarded
    async def daily(self):
        t = now()
        for guild in self.bot.home_guilds:
            await self.anniversaries(guild, t)
            if t.day == 1 and t.hour >= 8:
                prev = (t.replace(day=1) - dt.timedelta(days=1))
                key = f"chapter:{prev.year}-{prev.month:02d}"
                if not self.bot.db.gget(guild, key):
                    self.bot.db.gset(guild, key, 1)
                    await self.post_chapter(guild, prev.year, prev.month)

    @daily.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def anniversaries(self, guild, t):
        db = self.bot.db
        rows = db.all("SELECT user_id, MIN(COALESCE(decided_at, created_at)) d FROM applications "
                      "WHERE guild_id=? AND status='accepted' GROUP BY user_id", guild.id)
        for r in rows:
            m = guild.get_member(r["user_id"])
            if not m or not self.bot.in_family(m):
                continue
            days = (t.date() - parse_dt(r["d"]).date()).days
            label = ANNIVERSARIES.get(days)
            if label and not db.gget(guild, f"anniv:{m.id}:{days}"):
                db.gset(guild, f"anniv:{m.id}:{days}", 1)
                await self.record(guild, "anniversary", f"🎂 {m.mention} — уже **{label}** в семье {config.FAMILY_GEN}!",
                                  m.id)

    async def post_chapter(self, guild, year, month, channel=None):
        db, bot = self.bot.db, self.bot
        start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
        end = (start + dt.timedelta(days=32)).replace(day=1)
        rows = db.all("SELECT * FROM chronicle WHERE guild_id=? AND created_at>=? AND created_at<? ORDER BY id",
                      guild.id, start.isoformat(), end.isoformat())
        title = f"📜 Глава: {MONTHS_NOM[month - 1]} {year}"
        if not rows:
            text = "В этом месяце летопись молчит. Пусть следующий будет громче!"
        else:
            events = "\n".join(f"{r['created_at'][:10]} — {r['text']}" for r in rows)
            text = None
            if bot.ai.on:
                system = bot.brand(guild, f"Ты летописец семьи «{config.FAMILY}» (RP-проект «Россия Онлайн»). Напиши главу летописи за "
                          f"{MONTHS_NOM[month - 1].lower()} {year} в торжественном стиле старинной летописи («В лето "
                          f"{year}-е, месяца …»), но живо и понятно, 700-1500 символов. Упомяни события из списка, не "
                          "выдумывай новых. Упоминания вида <@123> оставляй как есть.")
                try:
                    text = await bot.ai.ask(system, events[:12000], max_tokens=900)
                except Exception as e:
                    log.warning("Глава летописи: %s", e)
            if not text:
                text = f"Главные события месяца ({len(rows)}):\n" + "\n".join(f"• {r['text']}" for r in rows[-30:])
        c = channel or bot.ch(guild, "chronicle")
        if c:
            e = bot.embed(title, text[:4000], config.GOLD, guild)
            e.set_thumbnail(url="attachment://emblem.png")
            await c.send(embed=e, file=discord.File(config.ASSETS / "emblem.png", filename="emblem.png"))

    # ---------- команды ----------
    @group.command(name="запись", description="Записать событие в летопись (победа, мероприятие, достижение)")
    @app_commands.rename(text="событие", image="картинка")
    @staff_only(config.LEAD + ["events"])
    async def add(self, inter: discord.Interaction, text: str, image: discord.Attachment = None):
        await inter.response.defer(ephemeral=True)
        url = None
        if image and (image.content_type or "").startswith("image/"):
            arch = self.bot.ch(inter.guild, "chronicle")
            if arch:
                f = await image.to_file()
                bot = self.bot
                bot.db.q("INSERT INTO chronicle(guild_id, kind, text, user_id, created_at) VALUES(?,?,?,?,?)",
                         inter.guild.id, "manual", f"✍️ {text}", inter.user.id, now_iso())
                e = bot.embed(f"📖 {ru_date(now())}", f"✍️ {text}", config.GOLD, inter.guild)
                e.set_image(url=f"attachment://{f.filename}")
                await arch.send(embed=e, file=f)
                return await inter.followup.send("✅ Записано в летопись.", ephemeral=True)
        await self.record(inter.guild, "manual", f"✍️ {text}", inter.user.id, url)
        await inter.followup.send("✅ Записано в летопись.", ephemeral=True)

    @group.command(name="глава", description="Показать главу летописи за текущий месяц")
    @staff_only(config.LEAD)
    async def chapter(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        t = now()
        await self.post_chapter(inter.guild, t.year, t.month, channel=inter.channel)
        await inter.followup.send("📖 Готово.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Chronicle(bot))
