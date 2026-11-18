# -*- coding: utf-8 -*-
"""Дни рождения — только если человек сам указал (/др или кнопка «🎂 Мой день рождения» на панели участника).
• Храним день и месяц, БЕЗ года (guild_settings bday:<user_id> = «ДД.ММ»), у каждой семьи свой.
• В день рождения около 10:00 по Москве — поздравление в общем чате в характере Швейцара (один раз в год).
• /др убрать или /забудь_меня — стереть. Тумблер persona:birthdays."""
import datetime as dt
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now

log = logging.getLogger("family-bot.bday")
MSK = dt.timezone(dt.timedelta(hours=config.GATHER_UTC_OFFSET))
WISHES = [
    "🎂 {m}, с днём рождения! Старый швейцар тебя помнит — живи долго, играй честно, выговоров не собирай.",
    "🎉 Сегодня у {m} день рождения! Семья, налетай с поздравлениями, а я пока дверь подержу.",
    "🥳 {m}, с днюхой! Желаю, чтобы казна была полной, а неактив — никогда.",
    "🎂 Внимание всем: {m} сегодня именинник. Кто не поздравит — тот у меня в досье.",
]


def valid(day, month):
    try:
        dt.date(2024, month, day)  # високосный — чтобы 29.02 тоже можно
        return True
    except ValueError:
        return False


class Birthdays(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @app_commands.command(name="др", description="Мой день рождения — поздравлю в чате (только день и месяц)")
    @app_commands.rename(day="день", month="месяц", remove="убрать")
    @app_commands.describe(remove="Стереть мой день рождения")
    @app_commands.guild_only()
    async def bday(self, inter: discord.Interaction, day: app_commands.Range[int, 1, 31] = None,
                   month: app_commands.Range[int, 1, 12] = None, remove: bool = False):
        if not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("Это для семьи.", ephemeral=True)
        key = f"bday:{inter.user.id}"
        if remove:
            self.bot.db.gdel(inter.guild, key)
            return await inter.response.send_message("🧹 Стёр. Поздравлять не буду — живи тайно.", ephemeral=True)
        if not day or not month:
            cur = self.bot.db.gget(inter.guild, key)
            return await inter.response.send_message(
                f"🎂 Записан: **{cur}**." if cur else "Укажи день и месяц, например `/др день:5 месяц:3`.", ephemeral=True)
        if not valid(day, month):
            return await inter.response.send_message("Такой даты не бывает.", ephemeral=True)
        self.bot.db.gset(inter.guild, key, f"{day:02d}.{month:02d}")
        await inter.response.send_message(f"🎂 Записал: {day:02d}.{month:02d}. Год не храню. Передумаешь — `/др убрать:True`.",
                                          ephemeral=True)

    def forget(self, guild, user_id):
        self.bot.db.gdel(guild, f"bday:{user_id}")

    @tasks.loop(minutes=30)
    @guarded
    async def loop(self):
        t = now().astimezone(MSK)
        if t.hour < 10:
            return
        today = t.strftime("%d.%m")
        for g in list(self.bot.home_guilds):
            if not self.bot.feature(g, "persona:birthdays"):
                continue
            c = self.bot.ch(g, "chat")
            if not c:
                continue
            rows = self.bot.db.all("SELECT key, value FROM guild_settings WHERE guild_id=? AND key LIKE 'bday:%' "
                                   "AND value=?", g.id, today)
            for r in rows:
                try:
                    uid = int(r["key"].split(":")[1])
                except (IndexError, ValueError):
                    continue
                done = f"bdaydone:{uid}"
                if self.bot.db.gget(g, done) == str(t.year):
                    continue
                m = g.get_member(uid)
                if not m:
                    continue
                self.bot.db.gset(g, done, str(t.year))
                try:
                    await c.send(random.choice(WISHES).format(m=m.mention),
                                 allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    pass

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Birthdays(bot))
