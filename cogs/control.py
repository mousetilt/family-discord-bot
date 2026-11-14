# -*- coding: utf-8 -*-
"""Настройки семьи без правки кода.
/ии ключ     — семья вставляет СВОЙ бесплатный ключ Groq и/или Gemini (модалка; бот проверяет ключ перед сохранением)
/ии статус   — какой ИИ работает у семьи и сколько запросов потрачено сегодня
/ии удалить  — убрать ключи семьи
/настройки   — включить/выключить модули (ИИ-модерация, Швейцар в войсе, сводка, бэкап…)
На домашнем сервере ИИ работает на ключах хоста; у остальных семей — только на своих (без ключа ИИ выключен)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.ai import AI, NAMES
from core.family import FEATURES

log = logging.getLogger("family-bot.control")
PROVIDERS = {"groq": "Groq — console.groq.com → API Keys", "gemini": "Gemini — aistudio.google.com → Get API key"}


def mask(key):
    key = key or ""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "задан"


def allowed(bot, inter):
    return bool(inter.guild) and (inter.user.id == inter.guild.owner_id or bot.has(inter.user, ["leader", "deputy"]))


class KeyModal(discord.ui.Modal, title="Ключи ИИ семьи (бесплатные)"):
    groq = discord.ui.TextInput(label="Groq API key (console.groq.com)", required=False, max_length=200,
                                placeholder="gsk_… · пусто — не менять · «-» — удалить")
    gemini = discord.ui.TextInput(label="Gemini API key (aistudio.google.com)", required=False, max_length=200,
                                  placeholder="AIza… · пусто — не менять · «-» — удалить")

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        g, db, out = inter.guild, self.bot.db, []
        for prov, field in (("groq", self.groq), ("gemini", self.gemini)):
            val = "".join((field.value or "").split())
            if not val:
                continue
            if val == "-":
                db.gdel(g, f"ai:{prov}")
                out.append(f"🗑 {NAMES[prov]}: ключ удалён")
                continue
            err = await AI(db, keys={prov: val}, scope=g.id).probe(prov)
            if err:
                out.append(f"❌ {NAMES[prov]}: ключ не сохранён — {err}")
                continue
            db.gset(g, f"ai:{prov}", val)
            out.append(f"✅ {NAMES[prov]}: ключ проверен и сохранён ({mask(val)})")
        if not out:
            out.append("Ничего не изменилось — оба поля пустые.")
        ai = self.bot.ai_for(g)
        out.append(f"\nСейчас у семьи: **{ai.name if ai.on else 'ИИ выключен'}**"
                   + (f" (резерв — {NAMES[ai.backup]})" if ai.on and ai.backup else ""))
        log.warning("ИИ-ключи семьи на «%s» изменены (%s)", g.name, inter.user)
        await inter.followup.send("\n".join(out), ephemeral=True)


class FeatureSelect(discord.ui.Select):
    def __init__(self, bot, guild):
        self.bot = bot
        opts = [discord.SelectOption(label=label[:100], value=key, default=bot.feature(guild, key))
                for key, label in FEATURES.items()]
        super().__init__(placeholder="Отметь, что должно работать", min_values=0, max_values=len(opts), options=opts)

    async def callback(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        on = set(self.values)
        for key in FEATURES:
            self.bot.db.gset(inter.guild, f"feat:{key}", "1" if key in on else "0")
        await inter.response.edit_message(content=render(self.bot, inter.guild) + "\n\n✅ Сохранено.",
                                          view=FeaturesView(self.bot, inter.guild))


class FeaturesView(discord.ui.View):
    def __init__(self, bot, guild):
        super().__init__(timeout=300)
        self.add_item(FeatureSelect(bot, guild))


def render(bot, guild):
    lines = ["**⚙️ Модули семьи** — отметь в списке, что должно работать:"]
    for key, label in FEATURES.items():
        lines.append(f"{'🟢' if bot.feature(guild, key) else '⚫'} {label}")
    return "\n".join(lines)


class Control(commands.Cog):
    ai_group = app_commands.Group(name="ии", description="ИИ семьи: свои бесплатные ключи и статус",
                                  default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def _deny(self, inter):
        await inter.response.send_message("⛔ Только для Главы, заместителя или владельца сервера.", ephemeral=True)

    @ai_group.command(name="ключ", description="Вставить свой бесплатный ключ Groq/Gemini (бот проверит его)")
    async def ai_key(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        if self.bot.is_home(inter.guild):
            return await inter.response.send_message(
                "🏠 Это домашний сервер: ИИ работает на ключах хоста (вкладка «Окружение» на хостинге).", ephemeral=True)
        await inter.response.send_modal(KeyModal(self.bot))

    @ai_group.command(name="статус", description="Какой ИИ работает у семьи и сколько запросов сегодня")
    async def ai_status(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        g, ai = inter.guild, self.bot.ai_for(inter.guild)
        lines = [f"🤖 ИИ семьи: **{ai.name if ai.on else 'выключен'}**"
                 + (f", резерв — {NAMES[ai.backup]}" if ai.on and ai.backup else "")]
        if self.bot.is_home(g):
            lines.append("🏠 Домашний сервер — ключи хоста.")
        else:
            for prov, how in PROVIDERS.items():
                k = self.bot.db.gget(g, f"ai:{prov}")
                lines.append(f"{'🔑' if k else '▫️'} {NAMES[prov]}: {mask(k) if k else 'нет'}  ·  {how}")
            if not ai.on:
                lines.append("\nБез ключа ИИ-функции (Швейцар, юрист, ИИ-модерация, голос) не работают. Ключ бесплатный — "
                             "`/ии ключ`. Голосовым командам нужен именно Groq.")
        lines.append(f"📈 Запросов сегодня: {ai.used_today()}/{config.AI_DAILY_LIMIT}")
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @ai_group.command(name="удалить", description="Удалить ключи ИИ семьи")
    async def ai_del(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        for prov in PROVIDERS:
            self.bot.db.gdel(inter.guild, f"ai:{prov}")
        await inter.response.send_message("🗑 Ключи ИИ семьи удалены.", ephemeral=True)

    @app_commands.command(name="настройки", description="Включить или выключить модули бота для семьи")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings_cmd(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        await inter.response.send_message(render(self.bot, inter.guild), view=FeaturesView(self.bot, inter.guild),
                                          ephemeral=True)


async def setup(bot):
    await bot.add_cog(Control(bot))
