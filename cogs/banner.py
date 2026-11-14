# -*- coding: utf-8 -*-
"""Баннер-табло сервера: численность семьи, руководство, старший состав и главная новость.
• Раз в сутки (BANNER_HOUR UTC) бот сам перерисовывает баннер. /баннер — обновить сейчас (руководство).
• /новость текст — задать главную новость, «сбросить» — убрать.
• Рисует отдельный процесс (core/banner_render.py): память после отрисовки возвращается системе.
• Баннер ставится только при 2+ уровне бустов."""
import asyncio
import logging
import os
import sys
import tempfile

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now

log = logging.getLogger("family-bot.banner")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "core", "banner_render.py")
BANNER_HOUR = 6          # UTC: 09:00 по Москве
MAX_NEWS = 60


class Banner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.hourly.start()

    def cog_unload(self):
        self.hourly.cancel()

    @tasks.loop(hours=1)
    @guarded
    async def hourly(self):
        for g in self.bot.home_guilds:
            t = now()
            key = f"banner:{t.date()}"
            if t.hour < BANNER_HOUR or self.bot.db.get(key):
                continue
            self.bot.db.set(key, 1)
            await self.refresh(g)

    @hourly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def stats(self, guild):
        fam = self.bot.family_members(guild)
        lead_ids = self.bot.role_ids(config.LEAD)
        senior_ids = self.bot.role_ids(["senior"])
        return (len(fam), sum(1 for m in fam if any(r.id in lead_ids for r in m.roles)),
                sum(1 for m in fam if any(r.id in senior_ids for r in m.roles)))

    async def render(self, stats, news):
        path = os.path.join(tempfile.gettempdir(), "family_banner.png")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, SCRIPT, path, *map(str, stats), news,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("отрисовка зависла")
        if proc.returncode:
            raise RuntimeError(err.decode("utf-8", "replace")[-300:])
        try:
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.remove(path)

    async def refresh(self, guild):
        async with self.lock:
            news = self.bot.db.get("news:text") or ""
            stats = self.stats(guild)
            try:
                data = await self.render(stats, news)
            except Exception as e:
                log.warning("Не смог нарисовать баннер: %s", e)
                return False
            try:
                await guild.edit(banner=data, reason="Обновление баннера семьи")
            except discord.HTTPException as e:
                log.warning("Баннер не поставлен: %s", e)
                await self.bot.log(guild, f"🖼 Не смог поставить баннер: {e}. Нужен 2+ уровень бустов сервера и право "
                                          "«Управлять сервером» у бота.", color=0xFB8C00)
                return False
            return True

    @app_commands.command(name="баннер", description="Обновить баннер сервера сейчас (руководство)")
    async def banner_cmd(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok = await self.refresh(inter.guild)
        await inter.followup.send("🖼 Баннер обновлён." if ok else "❌ Не получилось — смотри лог.", ephemeral=True)

    @app_commands.command(name="новость", description="Главная новость в баннере сервера (руководство)")
    @app_commands.describe(текст="Короткая новость, до 60 символов. «сбросить» — убрать.")
    async def news_cmd(self, inter: discord.Interaction, текст: str):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        text = " ".join(текст.split())
        self.bot.db.set("news:text", "" if text.lower() == "сбросить" else text[:MAX_NEWS])
        await inter.response.defer(ephemeral=True)
        ok = await self.refresh(inter.guild)
        await inter.followup.send(("📰 Готово, баннер обновлён." if ok
                                   else "📰 Новость записана, но баннер не обновился — смотри лог."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Banner(bot))
