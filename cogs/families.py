# -*- coding: utf-8 -*-
"""Несколько семей на одном боте: подключение семьи на своём сервере и её профиль (название, сервер в игре).
/семья подключить — владелец сервера включает полный функционал семьи (данные у каждой семьи свои).
/семья профиль    — название семьи, родительный падеж, игровой сервер, титул для пиара.
/семья отключить  — выключить (данные не удаляются, можно подключить обратно)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.family import FIELDS

log = logging.getLogger("family-bot.families")


def _clean(s, n):
    return " ".join(str(s or "").split())[:n]


class ProfileModal(discord.ui.Modal, title="Профиль семьи"):
    def __init__(self, bot, guild):
        super().__init__()
        self.bot, self.guild = bot, guild
        p = bot.family(guild)
        self.inputs = {}
        for key, (label, _default) in FIELDS.items():
            ti = discord.ui.TextInput(label=label, default=p[key], max_length=40, required=True)
            self.inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, inter: discord.Interaction):
        vals = {k: _clean(ti.value, 40) for k, ti in self.inputs.items()}
        if not all(vals.values()):
            return await inter.response.send_message("❌ Все поля обязательны.", ephemeral=True)
        for k, v in vals.items():
            self.bot.db.gset(self.guild, f"family:{k}", v)
        await inter.response.send_message(
            f"✅ Профиль сохранён: семья **{vals['name']}** (семья {vals['gen']}), сервер **{vals['server']}**, "
            f"титул «{vals['title']}».\nПанели с текстом обновятся после `/настройка`.", ephemeral=True)


class Families(commands.Cog):
    group = app_commands.Group(name="семья", description="Подключение семьи к боту и её профиль",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    def _owner_or_lead(self, inter):
        return inter.user.id == inter.guild.owner_id or self.bot.has(inter.user, ["leader", "deputy"])

    @group.command(name="подключить", description="Включить бота для семьи на этом сервере (владелец сервера)")
    async def enable(self, inter: discord.Interaction):
        g, bot = inter.guild, self.bot
        if bot.is_family(g):
            return await inter.response.send_message(
                "✅ Семья на этом сервере уже подключена. Профиль: `/семья профиль`, структура: `/настройка`.",
                ephemeral=True)
        if inter.user.id != g.owner_id:
            return await inter.response.send_message("⛔ Подключить семью может только владелец сервера.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        bot.set_family(g, True)
        try:
            await bot.sync_commands(g)
        except discord.HTTPException as e:
            log.warning("Семья «%s»: команды не зарегистрировались: %s", g.name, e)
        log.warning("Подключена семья на сервере «%s» (%s), владелец %s", g.name, g.id, inter.user)
        home = bot.get_guild(bot.home_id) if bot.home_id else None
        if home and home.id != g.id:
            await bot.log(home, f"🏠 Бота подключила новая семья: сервер **{g.name}** (`{g.id}`), владелец {inter.user}.")
        await inter.followup.send(
            "🎉 Семья подключена! Что дальше:\n"
            "1. `/семья профиль` — название семьи и игровой сервер (их бот подставит в тексты).\n"
            "2. `/настройка` — бот создаст роли, каналы и панели (или подхватит уже существующие с такими же названиями).\n"
            "3. Перетащи роль бота повыше в списке ролей сервера — иначе он не сможет выдавать роли.\n\n"
            "Данные вашей семьи (заявки, ЧС, рынок, досье) хранятся отдельно от других семей.", ephemeral=True)

    @group.command(name="профиль", description="Название семьи, родительный падеж, игровой сервер, титул")
    async def profile(self, inter: discord.Interaction):
        if not self.bot.is_family(inter.guild):
            return await inter.response.send_message("Сначала подключи семью: `/семья подключить`.", ephemeral=True)
        if not self._owner_or_lead(inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца сервера.",
                                                     ephemeral=True)
        await inter.response.send_modal(ProfileModal(self.bot, inter.guild))

    @group.command(name="отключить", description="Выключить бота для семьи на этом сервере (данные сохранятся)")
    async def disable(self, inter: discord.Interaction):
        g, bot = inter.guild, self.bot
        if bot.is_home(g):
            return await inter.response.send_message("⛔ Это домашний сервер бота — его отключить нельзя.", ephemeral=True)
        if inter.user.id != g.owner_id:
            return await inter.response.send_message("⛔ Только владелец сервера.", ephemeral=True)
        if not bot.is_family(g):
            return await inter.response.send_message("Семья здесь и так не подключена.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        bot.set_family(g, False)
        bot._synced_ids.discard(g.id)
        ally = bot.get_cog("Alliance")
        if ally:
            await ally.sync_foreign(g)
        await inter.followup.send("🔌 Семья отключена. Данные сохранены — подключить обратно: `/семья подключить`.",
                                  ephemeral=True)


async def setup(bot):
    await bot.add_cog(Families(bot))
