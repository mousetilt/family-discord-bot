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
from core.family import MODULES, NUMBER_GROUPS, NUMBERS, TEXTS, action_on, module_on, set_action, set_module

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


# ─────────────── числа и тексты семьи (/настройки → «🔢 Числа», «📜 Тексты») ───────────────
async def refresh_after_settings(bot, guild):
    """Поменяли тексты или числа — обновить то, что их показывает: правила, «о семье», панель заявок, базу юриста."""
    bot.kb.invalidate()
    ss, apps = bot.get_cog("ServerSetup"), bot.get_cog("Applications")
    try:
        if ss:
            await ss.refresh_texts(guild)
        if apps:
            await apps.post_panels(guild, True)
    except Exception:
        log.exception("Обновление панелей после настроек «%s»", guild.name)


class NumbersModal(discord.ui.Modal):
    def __init__(self, bot, guild, group):
        title, keys = NUMBER_GROUPS[group]
        super().__init__(title=title[:45])
        self.bot, self.keys, self.inputs = bot, keys, {}
        for k in keys:
            label, default, lo, hi = NUMBERS[k]
            ti = discord.ui.TextInput(label=label[:45], default=str(bot.num(guild, k)), max_length=6,
                                      placeholder=f"{lo}–{hi}, по умолчанию {default}")
            self.inputs[k] = ti
            self.add_item(ti)

    async def on_submit(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        out, bad = [], []
        for k, ti in self.inputs.items():
            label, _, lo, hi = NUMBERS[k]
            try:
                v = int(str(ti.value).strip())
            except ValueError:
                bad.append(label)
                continue
            v = max(lo, min(hi, v))
            self.bot.db.gset(inter.guild, f"set:{k}", v)
            if k == "probation_days":
                self.bot.db.gset(inter.guild, "probation:days", v)  # старый ключ /наставник срок — в ногу
            out.append(f"• {label}: **{v}**")
        if self.bot.num(inter.guild, "exam_pass") > self.bot.num(inter.guild, "exam_questions"):
            out.append("⚠️ Верных ответов нужно больше, чем вопросов — бот будет требовать все вопросы.")
        await inter.response.send_message("✅ Сохранено:\n" + "\n".join(out) +
                                          (f"\n❌ Не число: {', '.join(bad)}" if bad else ""), ephemeral=True)
        log.warning("Настройки «%s»: числа %s (%s)", inter.guild.name, [k for k in self.inputs], inter.user)
        await refresh_after_settings(self.bot, inter.guild)


def text_default(bot, guild, key):
    """Текущий текст семьи для правки (свой или стандартный)."""
    if key == "rules":
        return bot.rules_text(guild)
    if key == "welcome":
        return bot.welcome_text(guild)
    if key == "checklist":
        return "\n".join(bot.checklist(guild))
    return "\n".join(f"{q} | {h}" for q, h in bot.situational(guild))


class TextModal(discord.ui.Modal):
    def __init__(self, bot, guild, key):
        title, hint = TEXTS[key]
        super().__init__(title=title[:45])
        self.bot, self.key = bot, key
        self.body = discord.ui.TextInput(label=hint[:45], style=discord.TextStyle.paragraph, max_length=4000,
                                         default=text_default(bot, guild, key)[:4000], required=False,
                                         placeholder="Пусто — вернуть стандартный текст")
        self.add_item(self.body)

    async def on_submit(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        val = str(self.body.value or "").strip()
        title = TEXTS[self.key][0]
        if self.key == "situational" and val and len([ln for ln in val.splitlines() if ln.strip()]) < 2:
            return await inter.response.send_message("❌ Нужно хотя бы 2 вопроса — по одному в строке.", ephemeral=True)
        if val:
            self.bot.db.gset(inter.guild, f"text:{self.key}", val)
        else:
            self.bot.db.gdel(inter.guild, f"text:{self.key}")
        await inter.response.send_message(f"✅ {title}: {'сохранено' if val else 'вернул стандартный'}. "
                                          "Панели и база юриста обновятся сами.", ephemeral=True)
        log.warning("Настройки «%s»: текст %s (%s)", inter.guild.name, self.key, inter.user)
        await refresh_after_settings(self.bot, inter.guild)


class SettingPick(discord.ui.Select):
    """Выбор: какую группу чисел или какой текст править."""

    def __init__(self, bot, kind):
        self.bot, self.kind = bot, kind
        if kind == "num":
            opts = [discord.SelectOption(label=t[:100], value=g, description=", ".join(NUMBERS[k][0] for k in ks)[:100])
                    for g, (t, ks) in NUMBER_GROUPS.items()]
        else:
            opts = [discord.SelectOption(label=t[:100], value=k, description=h[:100]) for k, (t, h) in TEXTS.items()]
        super().__init__(placeholder="Что настроить?", options=opts)

    async def callback(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        v = self.values[0]
        modal = NumbersModal(self.bot, inter.guild, v) if self.kind == "num" else TextModal(self.bot, inter.guild, v)
        await inter.response.send_modal(modal)


def numbers_text(bot, guild):
    lines = ["**🔢 Числа семьи** — выбери группу, откроется окно с текущими значениями:"]
    for g, (t, keys) in NUMBER_GROUPS.items():
        lines.append(f"\n{t}")
        lines += [f"• {NUMBERS[k][0]}: **{bot.num(guild, k)}**" for k in keys]
    return "\n".join(lines)


class ModulesView(discord.ui.View):
    """mod=None — список модулей; mod — один модуль: рубильник + действия. back — колбэк «назад в мастер» (или None)."""

    def __init__(self, bot, guild, mod=None, back=None):
        super().__init__(timeout=600)
        self.bot, self.mod, self.back = bot, mod, back
        if mod is None:
            self.add_item(ModulePick(bot, guild))
            for label, emoji, kind in (("Числа", "🔢", "num"), ("Тексты", "📜", "text")):
                b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=4)
                b.callback = self._settings(kind)
                self.add_item(b)
            if back:
                b = discord.ui.Button(label="Назад в мастер", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
                b.callback = back
                self.add_item(b)
            return
        on = module_on(bot.db, guild.id, mod)
        if on:
            self.add_item(ActionSelect(bot, guild, mod))
        tg = discord.ui.Button(label="Выключить модуль целиком" if on else "Включить модуль",
                               emoji="🔌", style=discord.ButtonStyle.danger if on else discord.ButtonStyle.success, row=4)
        tg.callback = self._toggle
        up = discord.ui.Button(label="Ко всем модулям", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
        up.callback = self._up
        self.add_item(tg)
        self.add_item(up)

    def _settings(self, kind):
        async def cb(inter: discord.Interaction):
            if not allowed(self.bot, inter):
                return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.",
                                                         ephemeral=True)
            v = discord.ui.View(timeout=600)
            v.add_item(SettingPick(self.bot, kind))
            text = numbers_text(self.bot, inter.guild) if kind == "num" else (
                "**📜 Тексты семьи** — выбери, откроется окно с текущим текстом. Очистить поле — вернуть стандартный.\n"
                + "\n".join(f"• {t} — {'свой' if self.bot.db.gget(inter.guild, f'text:{k}') else 'стандартный'}"
                            for k, (t, _) in TEXTS.items()))
            await inter.response.send_message(text, view=v, ephemeral=True)
        return cb

    async def _toggle(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца.", ephemeral=True)
        on = not module_on(self.bot.db, inter.guild.id, self.mod)
        set_module(self.bot.db, inter.guild.id, self.mod, on)
        log.warning("Настройки «%s»: модуль %s → %s (%s)", inter.guild.name, self.mod, on, inter.user)
        await inter.response.edit_message(content=render(self.bot, inter.guild, self.mod),
                                          view=ModulesView(self.bot, inter.guild, self.mod, self.back))

    async def on_error(self, inter: discord.Interaction, error: Exception, item):
        log.exception("Настройки «%s»: ошибка кнопки", getattr(inter.guild, "name", "?"), exc_info=error)
        text = "⚠️ Не получилось применить настройку. Открой `/настройки` заново и попробуй ещё раз."
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

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
        await inter.response.send_modal(KeyModal(self.bot))

    @ai_group.command(name="статус", description="Какой ИИ работает у семьи и сколько запросов сегодня")
    async def ai_status(self, inter: discord.Interaction):
        if not allowed(self.bot, inter):
            return await self._deny(inter)
        g, ai = inter.guild, self.bot.ai_for(inter.guild)
        lines = [f"🤖 ИИ семьи: **{ai.name if ai.on else 'выключен'}**"
                 + (f", резерв — {NAMES[ai.backup]}" if ai.on and ai.backup else "")]
        home = self.bot.is_home(g)
        for prov, how in PROVIDERS.items():
            k = self.bot.db.gget(g, f"ai:{prov}")
            host = home and self.bot.ai.keys.get(prov)
            state = mask(k) + " (свой)" if k else (f"{mask(host)} (ключ хостинга)" if host else "нет")
            lines.append(f"{'🔑' if k or host else '▫️'} {NAMES[prov]}: {state}  ·  {how}")
        if home:
            lines.append("🏠 Домашний сервер: ключ из `/ии ключ` главнее ключа хостинга, ключ хостинга — запасной.")
        else:
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
