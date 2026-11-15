# -*- coding: utf-8 -*-
"""Настройки семьи без правки кода.
/ии ключ     — семья вставляет СВОЙ бесплатный ключ Groq и/или Gemini (модалка; бот проверяет ключ перед сохранением)
/ии статус   — какой ИИ работает у семьи и сколько запросов потрачено сегодня
/ии удалить  — убрать ключи семьи
/настройки   — дерево модулей: модуль целиком и его отдельные действия (mod:<модуль>:<действие>)
На домашнем сервере ИИ работает на ключах хоста; у остальных семей — только на своих (без ключа ИИ выключен)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.ai import AI, NAMES
from core.family import MODULES, action_on, module_on, set_action, set_module

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


# ─────────────── дерево модулей (/настройки и шаг «Модули» мастера) ───────────────
def render(bot, guild, mod=None):
    if mod:
        title, acts = MODULES[mod]
        on = module_on(bot.db, guild.id, mod)
        lines = [f"**{title}** — модуль {'🟢 включён' if on else '⚫ выключен целиком'}",
                 "Отметь в списке, какие действия должны работать:" if on else
                 "Включи модуль кнопкой, чтобы настроить его действия.", ""]
        for a, label in acts.items():
            mark = "🟢" if on and action_on(bot.db, guild.id, mod, a) else "⚫"
            lines.append(f"{mark} {label}")
        return "\n".join(lines)
    lines = ["**⚙️ Настройки семьи** — выбери модуль в списке, чтобы включить или выключить его действия.", ""]
    for m, (title, acts) in MODULES.items():
        if not module_on(bot.db, guild.id, m):
            lines.append(f"⚫ **{title}** — выключен")
            continue
        n = sum(action_on(bot.db, guild.id, m, a) for a in acts)
        lines.append(f"{'🟢' if n == len(acts) else '🟡' if n else '⚫'} **{title}** — {n}/{len(acts)}: "
                     + ", ".join(label.split(" (")[0] for a, label in acts.items()
                                 if action_on(bot.db, guild.id, m, a))[:150])
    return "\n".join(lines)


class ModulePick(discord.ui.Select):
    def __init__(self, bot, guild):
        self.bot = bot
        opts = [discord.SelectOption(label=title[:100], value=m,
                                     description=", ".join(acts.values())[:100])
                for m, (title, acts) in MODULES.items()]
        super().__init__(placeholder="Выбери модуль…", options=opts)

    async def callback(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        m = self.values[0]
        await inter.response.edit_message(content=render(self.bot, inter.guild, m),
                                          view=ModulesView(self.bot, inter.guild, m, self.view.back))


class ActionSelect(discord.ui.Select):
    def __init__(self, bot, guild, mod):
        self.bot, self.mod = bot, mod
        acts = MODULES[mod][1]
        opts = [discord.SelectOption(label=label[:100], value=a, default=action_on(bot.db, guild.id, mod, a))
                for a, label in acts.items()]
        super().__init__(placeholder="Отметь, что должно работать", min_values=0, max_values=len(opts), options=opts)

    async def callback(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        on = set(self.values)
        for a in MODULES[self.mod][1]:
            set_action(self.bot.db, inter.guild.id, self.mod, a, a in on)
        log.warning("Настройки «%s»: %s → %s (%s)", inter.guild.name, self.mod, sorted(on), inter.user)
        await inter.response.edit_message(content=render(self.bot, inter.guild, self.mod) + "\n\n✅ Сохранено.",
                                          view=ModulesView(self.bot, inter.guild, self.mod, self.view.back))


class ModulesView(discord.ui.View):
    """mod=None — список модулей; mod — один модуль: рубильник + действия. back — колбэк «назад в мастер» (или None)."""

    def __init__(self, bot, guild, mod=None, back=None):
        super().__init__(timeout=600)
        self.bot, self.mod, self.back = bot, mod, back
        if mod is None:
            self.add_item(ModulePick(bot, guild))
            if back:
                b = discord.ui.Button(label="Назад в мастер", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
                b.callback = back
                self.add_item(b)
            return
        on = module_on(bot.db, guild.id, mod)
        if on:
            self.add_item(ActionSelect(bot, guild, mod))
        tg = discord.ui.Button(label="Выключить модуль целиком" if on else "Включить модуль",
                               emoji="⏻", style=discord.ButtonStyle.danger if on else discord.ButtonStyle.success, row=4)
        tg.callback = self._toggle
        up = discord.ui.Button(label="Ко всем модулям", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
        up.callback = self._up
        self.add_item(tg)
        self.add_item(up)

    async def _toggle(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        on = not module_on(self.bot.db, inter.guild.id, self.mod)
        set_module(self.bot.db, inter.guild.id, self.mod, on)
        log.warning("Настройки «%s»: модуль %s → %s (%s)", inter.guild.name, self.mod, on, inter.user)
        await inter.response.edit_message(content=render(self.bot, inter.guild, self.mod),
                                          view=ModulesView(self.bot, inter.guild, self.mod, self.back))

    async def _up(self, inter: discord.Interaction):
        await inter.response.edit_message(content=render(self.bot, inter.guild),
                                          view=ModulesView(self.bot, inter.guild, None, self.back))


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

    @app_commands.command(name="настройки", description="Модули бота и их отдельные действия: что включить, что выключить")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings_cmd(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        await inter.response.send_message(render(self.bot, inter.guild), view=ModulesView(self.bot, inter.guild),
                                          ephemeral=True)


async def setup(bot):
    await bot.add_cog(Control(bot))
