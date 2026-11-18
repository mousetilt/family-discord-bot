# -*- coding: utf-8 -*-
"""Контроль ника по шаблону (Фаза 3). Модуль «Кадры» → действие nick (по умолчанию выкл).
• Шаблон словами, без регэкспов: «Имя Фамилия [ID]», «Имя_Фамилия | ID»… (nick:template). Пробел и «_» между
  частями взаимозаменяемы, лишние пробелы вокруг скобок не считаются ошибкой.
• Проверка: при смене ника и раз в 6 часов. Только члены семьи; не трогаем ботов, владельца и админов.
• Ник не по форме → ЛС с просьбой (не чаще раза в ASK_EVERY): кнопка «Указать ник» (модалка) или просто ответ
  сообщением в ЛС. Бот сам ставит ник (если хватает прав) и пишет в кадровый журнал.
• /ник шаблон — задать шаблон (и включить проверку); /ник проверка — кто сейчас не по форме + «написать им»."""
import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, staff_only

log = logging.getLogger("family-bot.nick")
NAME = r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)?"
TOKENS = {"Имя": NAME, "Фамилия": NAME, "ID": r"\d{1,10}"}
SAMPLE = {"Имя": "Иван", "Фамилия": "Иванов", "ID": "123456"}
DEFAULT = "Имя Фамилия [ID]"
PRESETS = ["Имя Фамилия [ID]", "Имя_Фамилия [ID]", "Имя Фамилия | ID", "Имя_Фамилия"]
ASK_EVERY = 3 * 86400  # повторная просьба — не чаще раза в 3 дня
SETTERS = config.LEAD + ["hr_head"]


def compile_template(t):
    """«Имя Фамилия [ID]» → регэксп. Без частей Имя/Фамилия/ID — None."""
    parts = re.split(r"(Имя|Фамилия|ID)", str(t or ""))
    if not any(p in TOKENS for p in parts):
        return None
    rx = ""
    for p in parts:
        if p in TOKENS:
            rx += TOKENS[p]
            continue
        if p and not p.strip(" _"):  # между частями только пробел/«_» — он обязателен (Имя Фамилия)
            rx += "[ _]+"
            continue
        for ch in p.strip(" _"):  # «[», «|», «(» — пробелы вокруг не важны
            rx += r"[ _]*" + re.escape(ch) + r"[ _]*"
    return re.compile(r"^\s*" + rx + r"\s*$")


def build_nick(template, game_nick, static=None):
    """Ник по шаблону семьи из анкеты: «Ivan_Ivanov» + 123456 + «Имя Фамилия [ID]» → «Ivan Ivanov [123456]».
    Не хватает частей (нет фамилии, нет Static) или результат не проходит шаблон — None."""
    words = [w for w in re.split(r"[ _]+", str(game_nick or "").strip()) if w]
    if not words:
        return None
    vals = {"Имя": words[0], "Фамилия": words[1] if len(words) > 1 else None,
            "ID": str(static).strip() if static and str(static).strip().isdigit() else None}
    parts = re.split(r"(Имя|Фамилия|ID)", str(template or DEFAULT))
    if any(p in vals and not vals[p] for p in parts):
        return None
    nick = "".join(vals[p] if p in vals else p for p in parts).strip()[:32]
    rx = compile_template(template)
    return nick if rx is None or rx.match(nick) else None


def example(t):
    return re.sub(r"Имя|Фамилия|ID", lambda m: SAMPLE[m.group(0)], str(t or DEFAULT))


class NickModal(discord.ui.Modal, title="Ник на сервере семьи"):
    def __init__(self, cog, gid, template):
        super().__init__()
        self.cog, self.gid = cog, gid
        self.nick = discord.ui.TextInput(label=f"Ник по форме: {template}"[:45], placeholder=example(template)[:100],
                                         max_length=32)
        self.add_item(self.nick)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.send_message(await self.cog.fix_from_dm(inter.user, self.nick.value, self.gid),
                                          ephemeral=inter.guild is not None)


class NickFixButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nick:fix:(?P<g>\d+)"):
    def __init__(self, gid):
        super().__init__(discord.ui.Button(label="Указать ник", emoji="✏️", style=discord.ButtonStyle.primary,
                                           custom_id=f"nick:fix:{gid}"))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["g"]))

    async def callback(self, inter: discord.Interaction):
        cog = inter.client.get_cog("NickCheck")
        g = inter.client.get_guild(self.gid)
        if not cog or not g:
            return await inter.response.send_message("Сервер семьи недоступен.", ephemeral=True)
        await inter.response.send_modal(NickModal(cog, self.gid, cog.template(g)))


class NickUseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nick:use:(?P<g>\d+)"):
    """«Поставить готовый ник» — собранный по шаблону из ника и Static, которые человек указал в анкете."""
    def __init__(self, gid, label="Поставить готовый ник"):
        super().__init__(discord.ui.Button(label=label[:80], emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id=f"nick:use:{gid}"))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["g"]))

    async def callback(self, inter: discord.Interaction):
        cog = inter.client.get_cog("NickCheck")
        g = inter.client.get_guild(self.gid)
        m = g.get_member(inter.user.id) if g else None
        nick = cog.suggest(m) if cog and m else None
        if not nick:
            return await inter.response.send_message("Не нашёл ник из анкеты — нажми «Указать ник».",
                                                     ephemeral=inter.guild is not None)
        await inter.response.send_message(await cog.apply(m, nick, "готовый ник из анкеты"),
                                          ephemeral=inter.guild is not None)


class AskAllView(discord.ui.View):
    def __init__(self, cog, author_id, members):
        super().__init__(timeout=300)
        self.cog, self.author_id, self.members = cog, author_id, members

    @discord.ui.button(label="Написать им в ЛС", emoji="✉️", style=discord.ButtonStyle.primary)
    async def go(self, inter: discord.Interaction, _):
        if inter.user.id != self.author_id:
            return await inter.response.send_message("Это не твоя проверка.", ephemeral=True)
        self.stop()
        await inter.response.edit_message(content=f"✉️ Пишу {len(self.members)} чел…", view=None)
        n = 0
        for m in self.members:
            if await self.cog.ask(m, force=True):
                n += 1
            await asyncio.sleep(1.5)
        await inter.edit_original_response(content=f"✉️ Написал {n} из {len(self.members)} (у остальных закрыта личка).")


class NickCheck(commands.Cog):
    group = app_commands.Group(name="ник", description="Контроль ников по шаблону семьи",
                               default_permissions=discord.Permissions(manage_nicknames=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.nick_loop.start()

    def cog_unload(self):
        self.nick_loop.cancel()

    # ---------- проверка ----------
    def template(self, guild):
        return self.bot.db.gget(guild, "nick:template") or DEFAULT

    def on(self, guild):
        return self.bot.feature(guild, "hr:nick")

    def ok(self, guild, nick):
        rx = compile_template(self.template(guild))
        return rx is None or bool(rx.match(nick or ""))

    def waiting(self, user_id):
        """Просили ли этого человека поправить ник (тогда его короткий ответ в ЛС — это ник)."""
        return any(self.on(g) and self.bot.db.gget(g, f"nick:asked:{user_id}") and g.get_member(user_id)
                   for g in self.bot.home_guilds)

    def exempt(self, m):
        return m.bot or m.id == m.guild.owner_id or m.guild_permissions.administrator or not self.bot.in_family(m)

    def suggest(self, m):
        """Готовый ник по шаблону из последней принятой анкеты (ник в игре + Static) или None."""
        a = self.bot.db.one("SELECT nick, static_id FROM applications WHERE guild_id=? AND user_id=? AND "
                            "status='accepted' ORDER BY id DESC LIMIT 1", m.guild.id, m.id)
        return build_nick(self.template(m.guild), a["nick"], a["static_id"]) if a else None

    def wrong(self, guild):
        return [m for m in self.bot.family_members(guild) if not self.exempt(m) and not self.ok(guild, m.display_name)]

    async def ask(self, m, force=False):
        """ЛС с просьбой поправить ник. Не чаще раза в ASK_EVERY (force — по кнопке руководства)."""
        g, db = m.guild, self.bot.db
        last = int(db.gget(g, f"nick:asked:{m.id}") or 0)
        if not force and time.time() - last < ASK_EVERY:
            return False
        t = self.template(g)
        v = discord.ui.View(timeout=None)
        ready = self.suggest(m)
        if ready and ready != m.display_name:
            v.add_item(NickUseButton(g.id, f"Поставить «{ready}»"))
        v.add_item(NickFixButton(g.id))
        fam = self.bot.family(g)["name"]
        try:
            await m.send(embed=self.bot.embed(
                "✏️ Поправь ник на сервере семьи",
                f"В семье **{fam}** ник должен быть по форме **`{t}`** — например `{example(t)}`.\n"
                f"Сейчас у тебя: `{m.display_name}`.\n\n" +
                ("Нажми «Поставить» — ник из твоей анкеты уже собран по форме. " if ready and ready != m.display_name
                 else "") +
                "Нажми кнопку и впиши правильный ник — или просто ответь сюда сообщением с ником. Поставлю сам.",
                config.GOLD, g), view=v)
        except discord.HTTPException:
            return False
        db.gset(g, f"nick:asked:{m.id}", int(time.time()))
        return True

    async def apply(self, m, nick, how):
        """Поставить ник. Возвращает текст для ответа человеку."""
        g, old = m.guild, m.display_name
        nick = " ".join(str(nick).split())[:32]
        if not self.ok(g, nick):
            t = self.template(g)
            return f"❌ «{nick}» не по форме **`{t}`**. Пример: `{example(t)}`. Попробуй ещё раз."
        self.bot.db.gdel(g, f"nick:asked:{m.id}")
        if nick == old:
            return "✅ Ник и так правильный."
        try:
            await m.edit(nick=nick, reason=f"Контроль ника: {how}")
        except discord.HTTPException:
            await self.bot.log(g, f"✏️ {m.mention} прислал ник «{nick}», но поставить не могу (роль бота ниже) — "
                                  "поменяйте вручную.", "hrlog", 0xFB8C00)
            return ("⚠️ Ник подходит, но поставить его сам не могу — не хватает прав. Поменяй сам: "
                    "правой кнопкой по серверу → «Изменить профиль сервера». Руководству я сообщил.")
        await self.bot.log(g, f"✏️ {m.mention}: ник «{old}» → «{nick}» ({how})", "hrlog", 0x26A69A)
        return f"✅ Готово: на сервере семьи теперь `{nick}`."

    async def fix_from_dm(self, user, nick, gid):
        g = self.bot.get_guild(gid)
        m = g.get_member(user.id) if g else None
        if not m:
            return "Не вижу тебя на сервере семьи."
        return await self.apply(m, nick, "сам, через ЛС")

    # ---------- события ----------
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        g = after.guild
        if before.display_name == after.display_name or not self.on(g) or self.exempt(after):
            return
        if self.ok(g, after.display_name):
            self.bot.db.gdel(g, f"nick:asked:{after.id}")
        else:
            await self.ask(after)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Ответ ником прямо в ЛС бота — если мы просили поправить ник."""
        if message.guild is not None or message.author.bot:
            return
        text = (message.content or "").strip()
        if not text or len(text) > 40 or "\n" in text:
            return
        for g in self.bot.home_guilds:
            m = g.get_member(message.author.id)
            if m and self.on(g) and self.bot.db.gget(g, f"nick:asked:{m.id}"):
                try:
                    await message.reply(await self.apply(m, text, "сам, ответом в ЛС"))
                except discord.HTTPException:
                    pass
                return

    @tasks.loop(hours=6)
    @guarded
    async def nick_loop(self):
        for g in list(self.bot.home_guilds):
            if not self.on(g):
                continue
            for m in self.wrong(g)[:20]:  # за проход не больше 20 ЛС с сервера
                if await self.ask(m):
                    await asyncio.sleep(2)

    @nick_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    # ---------- команды ----------
    @group.command(name="шаблон", description="Шаблон ника семьи — и включить проверку ников")
    @app_commands.rename(preset="готовый", custom="свой")
    @app_commands.describe(preset="Готовый шаблон", custom="Свой шаблон словами: Имя, Фамилия, ID — напр. «Имя Фамилия (ID)»")
    @app_commands.choices(preset=[app_commands.Choice(name=f"{p}  →  {example(p)}", value=p) for p in PRESETS])
    @staff_only(SETTERS)
    async def c_template(self, inter: discord.Interaction, preset: app_commands.Choice[str] = None,
                         custom: str = None):
        t = " ".join(str(custom or (preset.value if preset else "")).split())[:40]
        if not t:
            return await inter.response.send_message(
                f"Сейчас шаблон: **`{self.template(inter.guild)}`** (например `{example(self.template(inter.guild))}`), "
                f"проверка {'🟢 включена' if self.on(inter.guild) else '⚫ выключена'}. Выбери готовый или впиши свой.",
                ephemeral=True)
        if not compile_template(t):
            return await inter.response.send_message("❌ В шаблоне нужны слова **Имя**, **Фамилия** и/или **ID** — "
                                                     "например `Имя Фамилия [ID]`.", ephemeral=True)
        g = inter.guild
        self.bot.db.gset(g, "nick:template", t)
        self.bot.db.gset(g, "mod:hr", "1")
        self.bot.db.gset(g, "mod:hr:nick", "1")
        bad = self.wrong(g)
        await self.bot.log(g, f"⚙️ Шаблон ника: «{t}», проверка включена ({inter.user.mention})", "hrlog")
        await inter.response.send_message(
            f"✅ Шаблон ника: **`{t}`** — например `{example(t)}`. Проверка включена (выключить: `/настройки` → "
            f"«Кадры»).\nСейчас не по форме: **{len(bad)}** чел. Бот напишет им сам в течение нескольких часов — "
            "или сразу: `/ник проверка`.", ephemeral=True)

    @group.command(name="проверка", description="Кто в семье с ником не по шаблону")
    @staff_only(config.HR)
    async def c_check(self, inter: discord.Interaction):
        g = inter.guild
        bad = self.wrong(g)
        t = self.template(g)
        if not bad:
            return await inter.response.send_message(f"✅ У всех ники по форме `{t}`.", ephemeral=True)
        lines = [f"• {m.mention} — `{m.display_name}`" for m in bad[:30]]
        text = (f"✏️ Не по форме **`{t}`**: {len(bad)} чел." + ("" if self.on(g) else
                " (авто-проверка выключена — `/ник шаблон`)") + "\n" + "\n".join(lines))[:1900]
        await inter.response.send_message(text, view=AskAllView(self, inter.user.id, bad[:30]), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    bot.add_dynamic_items(NickFixButton, NickUseButton)
    await bot.add_cog(NickCheck(bot))
