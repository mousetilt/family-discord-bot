# -*- coding: utf-8 -*-
"""Несколько семей на одном боте.
• Бота добавили на новый сервер → приветствие владельцу (в ЛС и в системный канал) с кнопкой «Подключить семью».
/семья подключить — владелец сервера включает полный функционал семьи (данные у каждой семьи свои).
/семья профиль    — название семьи, родительный падеж, игровой сервер, титул для пиара.
/семья роли       — привязать УЖЕ существующие роли сервера к ролям бота (бот подсказывает по названиям);
                    привязанные роли /настройка не пересоздаёт и их права не трогает.
/семья отключить  — выключить (данные не удаляются, можно подключить обратно)."""
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.family import FIELDS

log = logging.getLogger("family-bot.families")

# подсказки: какие названия ролей на чужом сервере похожи на роль бота
HINTS = {
    "leader": ["глава", "лидер", "босс", "основатель", "leader", "owner"],
    "deputy": ["зам", "deputy"],
    "council": ["совет", "council"],
    "hr_head": ["кадр"],
    "treasurer": ["казн", "финанс", "бухгалт"],
    "diplomat": ["диплом"],
    "recruiter": ["рекрут", "набор", "recruit"],
    "events": ["организ", "мероприят", "ивент", "event"],
    "mentor": ["настав", "mentor"],
    "senior": ["старш", "senior"],
    "member": ["член семьи", "участник", "состав", "member"],
    "newbie": ["нович", "новен", "испыт", "newbie"],
    "vacation": ["отпуск"],
    "friend": ["друг", "друз", "friend"],
    "guest": ["гост", "guest"],
    "live": ["эфир", "стрим", "live"],
}
MAP_KEYS = [k for k, *_ in config.ROLES if not k.startswith("n_")]  # роли-уведомления привязывать незачем
PAGE = 4


def _clean(s, n):
    return " ".join(str(s or "").split())[:n]


def norm(s):
    return re.sub(r"[^a-zа-я0-9 ]", "", (s or "").lower().replace("ё", "е")).strip()


def suggest(guild, key, taken):
    """Существующая роль сервера, похожая на роль бота key (или None)."""
    words = HINTS.get(key, [])
    for r in sorted(guild.roles, key=lambda r: -r.position):
        if r.is_default() or r.managed or r.id in taken:
            continue
        n = norm(r.name)
        if any(w in n for w in words):
            return r
    return None


def can_manage(bot, inter):
    return inter.user.id == inter.guild.owner_id or bot.has(inter.user, ["leader", "deputy"])


# ─────────────── профиль ───────────────
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
            f"титул «{vals['title']}».\nДальше: `/семья роли` (если роли уже есть) → `/настройка`.", ephemeral=True)


# ─────────────── привязка ролей ───────────────
class RoleMapSelect(discord.ui.RoleSelect):
    def __init__(self, bot, guild, key):
        self.bot, self.key = bot, key
        rid = bot.db.gget(guild, f"role:{key}") if bot.db.gget(guild, f"rolemap:{key}") else None
        cur = guild.get_role(int(rid)) if rid else None
        super().__init__(placeholder=f"{config.ROLE_NAMES[key]} — какая роль у вас?"[:150], min_values=0, max_values=1,
                         default_values=[discord.Object(cur.id, type=discord.Role)] if cur else [])

    async def callback(self, inter: discord.Interaction):
        if not can_manage(self.bot, inter):
            return await inter.response.send_message("⛔ Только для владельца, Главы или заместителя.", ephemeral=True)
        db, g = self.bot.db, inter.guild
        if self.values:
            role = self.values[0]
            if role.managed or role.is_default():
                return await inter.response.send_message("❌ Эту роль привязать нельзя (служебная).", ephemeral=True)
            db.gset(g, f"role:{self.key}", role.id)
            db.gset(g, f"rolemap:{self.key}", 1)
            msg = f"✅ {config.ROLE_NAMES[self.key]} → {role.mention}"
        else:
            db.gdel(g, f"role:{self.key}")
            db.gdel(g, f"rolemap:{self.key}")
            msg = f"↩️ {config.ROLE_NAMES[self.key]}: привязка снята — `/настройка` создаст свою роль"
        await inter.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class RoleMapView(discord.ui.View):
    def __init__(self, bot, guild, page=0):
        super().__init__(timeout=600)
        self.bot, self.guild, self.page = bot, guild, page
        pages = (len(MAP_KEYS) + PAGE - 1) // PAGE
        for key in MAP_KEYS[page * PAGE:(page + 1) * PAGE]:
            self.add_item(RoleMapSelect(bot, guild, key))
        prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=page == 0, row=4)
        nxt = discord.ui.Button(label=f"▶ {page + 1}/{pages}", style=discord.ButtonStyle.secondary,
                                disabled=page >= pages - 1, row=4)
        auto = discord.ui.Button(label="Применить подсказки", emoji="✨", style=discord.ButtonStyle.primary, row=4)
        prev.callback = self._go(page - 1)
        nxt.callback = self._go(page + 1)
        auto.callback = self._auto
        for b in (prev, nxt, auto):
            self.add_item(b)

    def _go(self, page):
        async def cb(inter: discord.Interaction):
            await inter.response.edit_message(content=render_map(self.bot, inter.guild, page),
                                              view=RoleMapView(self.bot, inter.guild, page),
                                              allowed_mentions=discord.AllowedMentions.none())
        return cb

    async def _auto(self, inter: discord.Interaction):
        if not can_manage(self.bot, inter):
            return await inter.response.send_message("⛔ Только для владельца, Главы или заместителя.", ephemeral=True)
        g, db = inter.guild, self.bot.db
        n = 0
        for key, role in suggestions(self.bot, g).items():
            db.gset(g, f"role:{key}", role.id)
            db.gset(g, f"rolemap:{key}", 1)
            n += 1
        await inter.response.edit_message(content=render_map(self.bot, g, self.page) + f"\n\n✨ Привязано по подсказкам: {n}",
                                          view=RoleMapView(self.bot, g, self.page),
                                          allowed_mentions=discord.AllowedMentions.none())


def mapped(bot, guild):
    out = {}
    for key in MAP_KEYS:
        if bot.db.gget(guild, f"rolemap:{key}"):
            r = guild.get_role(int(bot.db.gget(guild, f"role:{key}") or 0))
            if r:
                out[key] = r
    return out


def suggestions(bot, guild):
    """Подсказки для ещё не привязанных ролей."""
    have = mapped(bot, guild)
    taken = {r.id for r in have.values()}
    out = {}
    for key in MAP_KEYS:
        if key in have:
            continue
        r = suggest(guild, key, taken)
        if r:
            out[key] = r
            taken.add(r.id)
    return out


def render_map(bot, guild, page=0):
    have, sug = mapped(bot, guild), suggestions(bot, guild)
    lines = ["**🎭 Привязка ролей** — если на сервере уже есть свои роли, укажи, какая чему соответствует.",
             "Непривязанные роли `/настройка` создаст сама. Права привязанных ролей бот не меняет.", ""]
    for key in MAP_KEYS:
        if key in have:
            lines.append(f"✅ {config.ROLE_NAMES[key]} → {have[key].mention}")
        elif key in sug:
            lines.append(f"💡 {config.ROLE_NAMES[key]} → похоже на {sug[key].mention}")
    if len(lines) == 3:
        lines.append("Пока ничего не привязано и подсказок нет — `/настройка` создаст все роли сама.")
    return "\n".join(lines)[:1900]


# ─────────────── подключение ───────────────
class FamilyDecision(discord.ui.DynamicItem[discord.ui.Button], template=r"fam:(?P<a>ok|no):(?P<g>\d+)"):
    """Владелец бота разрешает или нет подключить семью на новом сервере."""

    def __init__(self, a, gid):
        super().__init__(discord.ui.Button(label="Разрешить" if a == "ok" else "Отказать",
                                           emoji="✅" if a == "ok" else "✖️",
                                           style=discord.ButtonStyle.success if a == "ok" else discord.ButtonStyle.danger,
                                           custom_id=f"fam:{a}:{gid}"))
        self.a, self.gid = a, gid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], int(match["g"]))

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Families").decide(inter, self.a == "ok", self.gid)



class ConnectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fam:connect"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Подключить семью", emoji="🏠", style=discord.ButtonStyle.success,
                                           custom_id="fam:connect"))

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls()

    async def callback(self, inter: discord.Interaction):
        if not inter.guild:
            return await inter.response.send_message("Нажми эту кнопку на своём сервере, а не в ЛС: "
                                                     "или просто введи там `/семья подключить`.", ephemeral=True)
        await inter.client.get_cog("Families").connect(inter)


class Families(commands.Cog):
    group = app_commands.Group(name="семья", description="Подключение семьи к боту и её профиль",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    # ---------- первая встреча ----------
    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.bot.is_family(guild):
            return
        e = discord.Embed(
            title="🎩 Швейцар на месте",
            description=(f"Привет, **{guild.name}**! Я бот для RP-семей «России Онлайн»: заявки с экзаменом, наставники, "
                         "ЧС, рынок, дипломатия, летопись, музыка, голосовой помощник и ИИ-юрист.\n\n"
                         "**Как запустить (владелец сервера):**\n"
                         "1. Нажми «Подключить семью» ниже (или `/семья подключить`).\n"
                         "2. `/настройка` — мастер проведёт по шагам: профиль семьи, привязка своих ролей и каналов "
                         "(чтобы бот не плодил дубли), модули, бесплатный ключ ИИ и сборка сервера.\n"
                         "3. `/помощь` — что умеет бот; `/панель` — кнопки руководства.\n\n"
                         "Перетащи роль бота повыше в списке ролей — иначе он не сможет выдавать роли.\n\n"
                         f"**🤝 Вы только союзники по «{config.ALLIANCE_NAME}»?** Тогда семью подключать не нужно — "
                         "администратор пишет `/альянс подключить`, совет альянса голосует за вступление. "
                         "Будущий штаб альянса: `/альянс штаб`."),
            color=config.COLOR)
        v = discord.ui.View(timeout=None)
        v.add_item(ConnectButton())
        ch = guild.system_channel
        if not ch or not ch.permissions_for(guild.me).send_messages:
            ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if ch:
            try:
                await ch.send(embed=e, view=v)
            except discord.HTTPException:
                pass
        owner = guild.owner
        if owner is None:
            try:
                owner = await self.bot.fetch_user(guild.owner_id)
            except discord.HTTPException:
                owner = None
        if owner:
            try:
                await owner.send(content=f"Меня добавили на твой сервер **{guild.name}**. Команды — на сервере:",
                                 embed=e)
            except discord.HTTPException:
                pass

    async def connect(self, inter: discord.Interaction):
        g, bot = inter.guild, self.bot
        if bot.is_family(g):
            return await inter.response.send_message(
                "✅ Семья на этом сервере уже подключена. Дальше: `/семья профиль`, `/семья роли`, `/настройка`.",
                ephemeral=True)
        if inter.user.id != g.owner_id:
            return await inter.response.send_message("⛔ Подключить семью может только владелец сервера.", ephemeral=True)
        home = bot.get_guild(bot.home_id) if bot.home_id else None
        if home and home.id != g.id and not bot.db.gget(g, "family:approved"):
            # бот один на всех и живёт на хостинге владельца — новую семью сначала одобряет он
            return await self.request_approval(inter, home)
        await inter.response.defer(ephemeral=True, thinking=True)
        await self.enable_family(g, inter.user)
        sug = suggestions(bot, g)
        hint = (f"\n\n💡 Вижу на сервере похожие роли ({len(sug)}) — привяжи их в `/настройка` → «Роли», чтобы не было "
                f"дублей." if sug else "")
        await inter.followup.send(
            "🎉 Семья подключена! Дальше — `/настройка`: мастер проведёт по шагам (профиль семьи, свои роли и каналы, "
            "модули, ключ ИИ, сборка сервера).\n"
            "Перетащи роль бота повыше в списке ролей сервера." + hint, ephemeral=True)

    # ---------- одобрение новой семьи владельцем бота ----------
    async def request_approval(self, inter, home):
        g, bot = inter.guild, self.bot
        last = bot.db.gget(g, "family:requested")
        if last and last.isdigit() and time.time() - int(last) < 86400:
            return await inter.response.send_message("⏳ Заявка уже у владельца бота — как только он ответит, я напишу "
                                                     "тебе в ЛС и сюда.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        bot.db.gset(g, "family:requested", int(time.time()))
        e = discord.Embed(title="🏠 Новая семья просит подключить бота",
                          description=f"Сервер **{g.name}** (`{g.id}`) · участников: {g.member_count}\n"
                                      f"Владелец: {inter.user} (`{inter.user.id}`)\n\n"
                                      "Разрешить — у семьи заработает полный функционал (данные отдельные, ИИ — на "
                                      "её ключах). Отказать — бот останется там только для альянса.",
                          color=config.COLOR)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        sent = False
        for target in (bot.ch(home, "botcmd"), home.owner):
            if not target:
                continue
            v = discord.ui.View(timeout=None)
            v.add_item(FamilyDecision("ok", g.id))
            v.add_item(FamilyDecision("no", g.id))
            try:
                await target.send(embed=e, view=v)
                sent = True
            except discord.HTTPException:
                pass
        if not sent:  # некуда спросить — одобрять сами не будем (иначе одобрение можно обойти), ждём
            bot.db.gdel(g, "family:requested")  # можно повторить, не дожидаясь суток
            log.error("Заявку семьи «%s» (%s) некуда доставить: ни канала бот-команд, ни ЛС владельца домашнего "
                      "сервера", g.name, g.id)
            return await inter.followup.send("⏳ Не смог достучаться до владельца бота. Напиши ему сам — пусть "
                                             "откроет ЛС; потом снова `/семья подключить`.", ephemeral=True)
        await inter.followup.send("📨 Заявка отправлена владельцу бота. Как только он разрешит — я напишу тебе в ЛС, "
                                  "и можно будет запускать `/настройка`.", ephemeral=True)

    async def decide(self, inter, ok, gid):
        bot = self.bot
        home = bot.get_guild(bot.home_id) if bot.home_id else None
        m = home.get_member(inter.user.id) if home else None
        if not m or not (m.id == home.owner_id or bot.has(m, ["leader", "deputy"])):
            return await inter.response.send_message("⛔ Решает владелец бота (Глава или заместитель домашнего сервера).",
                                                     ephemeral=True)
        g = bot.get_guild(gid)
        if not g:
            return await inter.response.edit_message(content="Бота уже убрали с этого сервера.", view=None)
        if bot.is_family(g) and ok:
            return await inter.response.edit_message(content=f"✅ Семья «{g.name}» уже подключена.", view=None)
        await inter.response.edit_message(content=f"{'✅ Разрешено' if ok else '❌ Отказано'} — {inter.user.mention}",
                                          view=None)
        owner = g.owner or await self._fetch(g.owner_id)
        if ok:
            bot.db.gset(g, "family:approved", 1)
            await self.enable_family(g, owner)
            text = (f"🎉 Владелец бота разрешил подключить семью на сервере **{g.name}**! Дальше — `/настройка`: мастер "
                    "проведёт по шагам.")
        else:
            bot.db.gdel(g, "family:requested")
            text = f"😔 Владелец бота не разрешил подключить семью на сервере **{g.name}**."
        if owner:
            try:
                await owner.send(text)
            except discord.HTTPException:
                pass
        ch = g.system_channel
        if ch and ch.permissions_for(g.me).send_messages:
            try:
                await ch.send(text)
            except discord.HTTPException:
                pass

    async def _fetch(self, uid):
        try:
            return await self.bot.fetch_user(uid)
        except discord.HTTPException:
            return None

    async def enable_family(self, g, who):
        bot = self.bot
        bot.set_family(g, True)
        if not bot.is_home(g) and not bot.db.gget(g, "family:name"):
            # пока профиль не заполнен, чужая семья не должна везде зваться «Ульяновы» — берём имя сервера
            nm = (g.name or "").strip()[:40]
            if nm:
                bot.db.gset(g, "family:name", nm)
                bot.db.gset(g, "family:gen", nm)
                bot.db.gset(g, "family:title", nm)
        try:
            await bot.sync_commands(g)
        except discord.HTTPException as e:
            log.warning("Семья «%s»: команды не зарегистрировались: %s", g.name, e)
        log.warning("Подключена семья на сервере «%s» (%s), владелец %s", g.name, g.id, who)
        home = bot.get_guild(bot.home_id) if bot.home_id else None
        if home and home.id != g.id:
            await bot.log(home, f"🏠 Бота подключила новая семья: сервер **{g.name}** (`{g.id}`), владелец {who}.")

    # ---------- команды ----------
    @group.command(name="подключить", description="Включить бота для семьи на этом сервере (владелец сервера)")
    async def enable(self, inter: discord.Interaction):
        await self.connect(inter)

    @group.command(name="профиль", description="Название семьи, родительный падеж, игровой сервер, титул")
    async def profile(self, inter: discord.Interaction):
        if not self.bot.is_family(inter.guild):
            return await inter.response.send_message("Сначала подключи семью: `/семья подключить`.", ephemeral=True)
        if not can_manage(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца сервера.",
                                                     ephemeral=True)
        await inter.response.send_modal(ProfileModal(self.bot, inter.guild))

    @group.command(name="роли", description="Привязать существующие роли сервера к ролям бота")
    async def roles(self, inter: discord.Interaction):
        if not self.bot.is_family(inter.guild):
            return await inter.response.send_message("Сначала подключи семью: `/семья подключить`.", ephemeral=True)
        if not can_manage(self.bot, inter):
            return await inter.response.send_message("⛔ Только для Главы, заместителя или владельца сервера.",
                                                     ephemeral=True)
        await inter.response.send_message(render_map(self.bot, inter.guild), view=RoleMapView(self.bot, inter.guild),
                                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

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
    bot.add_dynamic_items(ConnectButton, FamilyDecision)
    await bot.add_cog(Families(bot))
