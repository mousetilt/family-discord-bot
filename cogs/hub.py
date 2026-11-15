# -*- coding: utf-8 -*-
"""Упрощение UX (Фаза 1, docs/КАРТА_УПРОЩЕНИЯ_КОМАНД.md).
/помощь — интерактивная «что я умею»: что сказать Швейцару, какую кнопку нажать, статус ИИ и голоса.
Команды помнить не нужно: почти всё — речью («Швейцар, …») или кнопками панелей."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.family import MODULES, module_on, action_on

log = logging.getLogger("family-bot.hub")

STAFF = config.LEAD + ["hr_head", "recruiter", "diplomat", "events", "mentor", "senior"]

# раздел помощи: (эмодзи, название, только для руководства?, [строки])
HELP = {
    "music": ("🎵", "Музыка", False, [
        "🗣 «Швейцар, включи Кино — Группа крови» · «пауза» · «дальше» · «громкость 60» · «что играет?» · «выключи музыку»",
        "⌨️ Ярлыки: `/играть`, `/радио`, `/музыка`",
    ]),
    "voice": ("🎙", "Голос и войс", False, [
        "🗣 «Швейцар, зайди и слушай» — дальше можно командовать голосом, начиная со слова «Швейцар»",
        "🗣 «позови Дашу» · «зайди к Пете и спроси, как дела» · «выйди из войса»",
        "🗣 Руководство: «перенеси нас всех в балкон» · «выключи всем микрофоны» · «объяви сбор голосом»",
    ]),
    "market": ("🛒", "Рынок", False, [
        "🗣 «Швейцар, что есть на рынке по Элегии?»",
        "🔘 Панель участника → **Рынок**: поиск, мои объявления, продать, узнать цену",
    ]),
    "me": ("👤", "Я и мой профиль", False, [
        "🔘 Панель участника → **Моё**: профиль, что Швейцар о тебе помнит, мои выговоры",
        "🔘 Панель участника → **Отпуск**: уйти в отпуск / вернуться (в отпуске не считаешься неактивным)",
        "🗣 «Швейцар, забудь меня» — стереть досье · «напомни через 20 минут зайти в игру» · «брось кубик»",
    ]),
    "rooms": ("🔊", "Комнаты, эфиры, бусты", False, [
        "🔘 Панель участника → **Моя комната**: открыть, закрыть, имя, лимит, выгнать, передать",
        "🔘 Панель участника → **Эфиры и бусты**: объявить свой эфир, кто сейчас в эфире, бусты",
    ]),
    "law": ("⚖️", "Юрист и правила", False, [
        "🗣 «Швейцар, какой штраф за езду без прав?» — ответит по законам и правилам проекта",
        "✍️ Или просто напиши вопрос в канал юриста",
        "🔘 Панель участника → **Обучение**: найти в базе знаний, тренировка экзамена",
    ]),
    "staff": ("🎖", "Ранги и выговоры", True, [
        "🗣 «выговор Пете за опоздание» · «сними выговоры Пете» · «назначь Петю рекрутёром» · «сделай Петю старшим»",
        "🗣 «мут Пете на 30 минут за флуд» · «исключи Петю из семьи» (с подтверждением)",
        "🔘 `/панель` → **Кадры**",
    ]),
    "apps": ("📋", "Заявки и экзамен", True, [
        "Заявки подаются кнопкой в канале заявок — бот сам проводит экзамен и зовёт на собеседование",
        "🔘 `/панель` → **Заявки**: открытые заявки, обращения, наставники, вопросы экзамена",
    ]),
    "activity": ("📊", "Активность и сборы", True, [
        "🗣 «кто неактив за неделю?» · «топ активности» · «сводка» · «объяви сбор на 20:00 у банка»",
        "🗣 «опрос: во сколько едем? 19:00, 20:00» · «напиши всем в ЛС, что сегодня сбор»",
        "🔘 `/панель` → **Активность**",
    ]),
    "guard": ("🛡", "Модерация и ЧС", True, [
        "Бот сам ловит спам, рекламу, скам и сделки за реал — решения приходят в канал модерации с кнопками",
        "🗣 «пробей Петю по ЧС» · «внеси Петю в ЧС за кидок» · «включи режим рейда»",
        "🔘 `/панель` → **Модерация**: защита, рейд, ЧС",
    ]),
    "diplo": ("🤝", "Дипломатия и альянс", True, [
        "🔘 `/панель` → **Дипломатия**: договоры, отношения, позвать на переговоры",
        "🔘 `/панель` → **Альянс**: состав, устав, летопись, тревога",
    ]),
    "setup": ("⚙️", "Настройка бота", True, [
        "🧙 `/настройка` — мастер по шагам: семья → роли → каналы → модули → ИИ → собрать сервер",
        "⚙️ `/настройки` — что включено: каждый модуль и его отдельные действия",
        "🔘 `/панель` — все кнопки руководства в одном месте",
    ]),
}


def is_staff(bot, member):
    return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or bot.has(member, STAFF))


def status_lines(bot, guild):
    """Короткий статус: ИИ, голос, модули."""
    ai = bot.ai_for(guild)
    out = [f"🤖 ИИ: **{ai.name}**" if ai.on else "🤖 ИИ: **выключен** — руководство может подключить бесплатный ключ "
                                                  "(`/настройка` → шаг «ИИ»)"]
    jv = bot.get_cog("Jarvis")
    if jv:
        try:
            voice = bool(jv.whisper_key(guild))
        except Exception:
            voice = False
        here = guild.id in getattr(jv, "listeners", {})
        out.append(f"🎙 Голосовые команды: **{'работают' if voice else 'нужен ключ Groq'}**"
                   + (" · Швейцар сейчас в войсе" if here else ""))
    total = sum(len(a) for _, a in MODULES.values())
    on = sum(1 for m, (_, acts) in MODULES.items() if module_on(bot.db, guild.id, m)
             for a in acts if action_on(bot.db, guild.id, m, a))
    out.append(f"⚙️ Включено функций: **{on}/{total}**")
    return out


def help_home(bot, member):
    g = member.guild
    lines = ["Ничего учить не надо — есть три способа:",
             "🗣 **Сказать** — напиши в чат «**Швейцар, …**» (или упомяни меня) и попроси своими словами. "
             "В войсе — то же самое голосом.",
             "🔘 **Нажать** — панель участника в канале «о семье»" + (", для руководства — `/панель`"
                                                                       if is_staff(bot, member) else "") + ".",
             "⌨️ **Команда** — только для редкого; набери `/` и увидишь, что тебе доступно.", "",
             *status_lines(bot, g), "", "Выбери раздел ниже, чтобы увидеть примеры."]
    e = bot.embed("🎩 Что я умею", "\n".join(lines), config.COLOR, g)
    return e


def help_section(bot, member, key):
    emoji, title, lead, lines = HELP[key]
    e = bot.embed(f"{emoji} {title}" + (" · руководство" if lead else ""), "\n".join(lines), config.COLOR,
                  member.guild)
    return e


class HelpSelect(discord.ui.Select):
    def __init__(self, bot, member, current=None):
        self.bot = bot
        staff = is_staff(bot, member)
        opts = [discord.SelectOption(label="Главное", value="home", emoji="🏠", default=current is None)]
        for key, (emoji, title, lead, _) in HELP.items():
            if lead and not staff:
                continue
            opts.append(discord.SelectOption(label=title, value=key, emoji=emoji, default=current == key))
        super().__init__(placeholder="Раздел помощи…", options=opts[:25])

    async def callback(self, inter: discord.Interaction):
        key = self.values[0]
        e = help_home(self.bot, inter.user) if key == "home" else help_section(self.bot, inter.user, key)
        await inter.response.edit_message(embed=e, view=HelpView(self.bot, inter.user, None if key == "home" else key))


class HelpView(discord.ui.View):
    def __init__(self, bot, member, current=None):
        super().__init__(timeout=600)
        self.add_item(HelpSelect(bot, member, current))


class Hub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def send_help(self, inter: discord.Interaction):
        await inter.response.send_message(embed=help_home(self.bot, inter.user), view=HelpView(self.bot, inter.user),
                                          ephemeral=True)

    @app_commands.command(name="помощь", description="Что умеет бот: что сказать, куда нажать")
    @app_commands.guild_only()
    async def help_cmd(self, inter: discord.Interaction):
        await self.send_help(inter)


async def setup(bot):
    await bot.add_cog(Hub(bot))
