# -*- coding: utf-8 -*-
"""/настройка — собирает сервер: роли, каналы, права, оформление, панели. Приветствие новичков."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import config, images
from core.botcore import safe_dm

log = logging.getLogger("family-bot.setup")
PO = discord.PermissionOverwrite

# Каналы из прошлой версии бота: переименуем, а не создадим дубли
LEGACY = {
    "chronicle": ["🧬・история-семьи"],
    "selfroles": ["🎭・выбор-уведомлений"],
    "lawyer": ["🤖・спросить-бота"],
    "market": ["🛒・барахолка"],
}


def _keys(x):
    return set() if x == "all" else set(x or [])


def build_overwrites(guild, R, view, send, kind="text", connect=None, speak=None):
    """Права канала по ролям. kind: text / voice / both (категория).
    view — кто видит, send — кто пишет, connect — кто заходит в войс, speak — кто говорит."""
    connect = view if connect is None else connect
    speak = connect if speak is None else speak
    text, voice = kind in ("text", "both"), kind in ("voice", "both")
    all_view = view == "all"
    e = PO(view_channel=all_view, mention_everyone=False, create_public_threads=False, create_private_threads=False)
    if text:
        e.update(send_messages=all_view and send == "all", send_messages_in_threads=all_view, add_reactions=all_view)
    if voice:
        conn = all_view and connect == "all"
        talk = all_view and speak == "all"  # перенесённый ботом кандидат должен говорить, даже без права входа
        e.update(connect=conn, speak=talk, stream=talk)
        if not text:
            e.send_messages = conn
    ow = {guild.default_role: e}
    for k in _keys(view) | _keys(send if text else []) | _keys(connect) | _keys(speak):
        po = PO(view_channel=True, read_message_history=True)
        if text:
            can_send = send == "all" or k in _keys(send)
            po.update(send_messages=can_send, send_messages_in_threads=True, add_reactions=True, attach_files=can_send,
                      embed_links=can_send)
        if voice:
            can_conn = connect == "all" or k in _keys(connect) or k in _keys(speak)
            can_speak = can_conn and (speak == "all" or k in _keys(speak))
            po.update(connect=can_conn, speak=can_speak, stream=can_speak, use_voice_activation=True)
            if not text:
                po.send_messages = can_conn
        ow[R[k]] = po
    ow[guild.me] = PO(view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True,
                      move_members=True, manage_messages=True, embed_links=True, attach_files=True,
                      manage_permissions=True, manage_threads=True, create_private_threads=True,
                      send_messages_in_threads=True, read_message_history=True)
    return ow


async def order_roles(guild, our_ids):
    """Роли семьи — в правильном порядке сразу под ролью бота (свежие позиции берём у Discord)."""
    await asyncio.sleep(1.5)
    roles = await guild.fetch_roles()
    by_id = {r.id: r for r in roles}
    my_ids = {r.id for r in guild.me.roles}
    bot_pos = max((by_id[i].position for i in my_ids if i in by_id), default=0)
    movable = [r for r in roles if 0 < r.position < bot_pos and not r.is_default()]
    ours = [by_id[i] for i in our_ids if i in by_id and 0 < by_id[i].position < bot_pos]
    stuck = [by_id[i].name for i in our_ids if i in by_id and by_id[i].position >= bot_pos]
    others = [r for r in sorted(movable, key=lambda r: r.position, reverse=True) if r.id not in set(our_ids)]
    desired = ours + others
    try:
        await guild.edit_role_positions(positions={r: len(desired) - i for i, r in enumerate(desired)})
    except discord.HTTPException as e:
        log.warning("Порядок ролей: %s", e)
        return "⚠️ Не удалось упорядочить роли — перетащи роль бота на самый верх и запусти /настройка ещё раз"
    if stuck:
        return ("⚠️ Роль бота ниже некоторых ролей (" + ", ".join(stuck[:3]) + "). Перетащи роль бота на самый верх "
                "и запусти /настройка ещё раз")
    return "✅ Роли упорядочены"


def find_category(bot, guild, cat):
    """Категория по сохранённому ID (переживает переименование), иначе по названию."""
    cid = bot.db.gget(guild, f"cat:{cat['name']}") or (bot.db.gget(guild, f"ch:{cat['key']}") if cat.get("key")
                                                       else None)
    c = guild.get_channel(int(cid)) if cid else None
    if isinstance(c, discord.CategoryChannel):
        return c
    return discord.utils.get(guild.categories, name=cat["name"])


# Каналы, добавленные после первой сборки сервера: при старте бот создаёт их сам (один раз, без «Собрать сервер»).
# Значение — модуль, без которого канал не нужен. Удалили канал руками — второй раз не создаём (флаг auto:<key>).
AUTO_CHANNELS = {"panel": None, "treasury_log": "treasury", "garage": "garage"}


async def ensure_new_channels(bot, guild):
    db = bot.db
    if not db.gget(guild, "role:leader"):
        return []  # сервер ещё не собирали — всё создаст /настройка
    R = {k: bot.role(guild, k) for k, *_ in config.ROLES}
    made = []
    for cat in config.STRUCTURE:
        for ctype, key, name, extra in cat["channels"]:
            if key not in AUTO_CHANNELS or bot.ch(guild, key) or db.gget(guild, f"auto:{key}"):
                continue
            mod = AUTO_CHANNELS[key]
            if mod and not bot.feature(guild, mod):
                continue
            category = find_category(bot, guild, cat)
            if not category:
                continue
            ch = discord.utils.get(category.channels, name=name)
            try:
                if not ch:
                    ow = build_overwrites(guild, R, extra.get("view", cat["view"]), extra.get("send", cat["send"]),
                                          kind=ctype)
                    ow = {t: p for t, p in ow.items() if t is not None}  # роль удалили — просто без неё
                    ch = await guild.create_text_channel(name, category=category, overwrites=ow,
                                                         topic=extra.get("topic"), reason="Новый канал бота")
                    made.append(name)
            except discord.HTTPException as e:
                log.warning("Новый канал %s на %s: %s", key, guild.name, e)
                continue
            db.gset(guild, f"ch:{key}", ch.id)
            db.gset(guild, f"auto:{key}", 1)
    return made


FRIEND_DENY = dict(view_channel=False, connect=False)


async def hide_voices_from_friends(bot, guild):
    """Явный запрет роли «Друг семьи» на войсы из config.FRIEND_HIDDEN_VOICES (в т.ч. привязанные каналы семьи).
    Идемпотентно: трогает только каналы, где запрета ещё нет. Возвращает число исправленных каналов."""
    friend = bot.role(guild, "friend")
    if not friend:
        return 0
    fixed = 0
    for key in config.FRIEND_HIDDEN_VOICES:
        ch = bot.ch(guild, key)
        if not isinstance(ch, discord.VoiceChannel):
            continue
        cur = ch.overwrites_for(friend)
        if cur.view_channel is False and cur.connect is False:
            continue
        try:
            await ch.set_permissions(friend, overwrite=PO(**FRIEND_DENY), reason="Друзья семьи не видят этот войс")
            fixed += 1
        except discord.HTTPException as e:
            log.warning("Скрыть войс %s от друзей на %s: %s", key, guild.name, e)
    return fixed


async def build_server(bot, guild, progress):
    db = bot.db
    R = {}
    for key, name, color, perms, hoist in config.ROLES:
        # сначала по сохранённому ID: если роль переименовали вручную, дубль не создаём
        rid = db.gget(guild, f"role:{key}")
        role = (guild.get_role(int(rid)) if rid else None) or discord.utils.get(guild.roles, name=name)
        if not role:
            role = await guild.create_role(
                name=name, colour=discord.Colour(color), permissions=discord.Permissions(**perms), hoist=hoist,
                mentionable=key.startswith("n_") or key in ("recruiter", "hr_head", "member", "mentor", "diplomat"),
                reason="Авто-настройка сервера")
        elif key != "leader" and not db.gget(guild, f"rolemap:{key}") \
                and role.permissions != discord.Permissions(**perms) and role < guild.me.top_role:
            # роли, которые семья сама привязала в /семья роли, не трогаем — у них свои права
            try:
                await role.edit(permissions=discord.Permissions(**perms), reason="Права роли по настройке")
            except discord.HTTPException:
                pass
        R[key] = role
        db.gset(guild, f"role:{key}", role.id)
    await progress(f"✅ Роли ({len(R)})")

    # Бережно к готовому серверу: порядок ролей, @everyone, AFK и системные уведомления меняем только на домашнем.
    # На подключённом сервере своя иерархия — её не переворачиваем; привязанные роли (/семья роли) не двигаем нигде.
    home = bot.is_home(guild)
    ours = [R[k].id for k, *_ in config.ROLES if not db.gget(guild, f"rolemap:{k}")]
    if home:
        await progress(await order_roles(guild, ours))
        try:
            p = guild.default_role.permissions
            p.update(mention_everyone=False)
            await guild.default_role.edit(permissions=p, reason="Только руководство может пинговать @everyone")
        except discord.HTTPException:
            pass
    else:
        low = [R[k].name for k, *_ in config.ROLES if R[k] >= guild.me.top_role]
        await progress("⚠️ Роль бота ниже ролей: " + ", ".join(low[:3]) + " — перетащи её выше, иначе бот не сможет их "
                       "выдавать" if low else "✅ Порядок ролей сервера не трогаю — роль бота выше ролей семьи")

    for cat in config.STRUCTURE:
        ow = build_overwrites(guild, R, cat["view"], cat["send"], kind="both")
        category = find_category(bot, guild, cat)
        if not category:
            category = await guild.create_category(cat["name"], overwrites=ow)
        else:
            await category.edit(overwrites=ow)
        db.gset(guild, f"cat:{cat['name']}", category.id)
        if cat.get("key"):
            db.gset(guild, f"ch:{cat['key']}", category.id)
        for ctype, key, name, extra in cat["channels"]:
            c_ow = build_overwrites(guild, R, extra.get("view", cat["view"]), extra.get("send", cat["send"]),
                                   kind=ctype, connect=extra.get("connect"), speak=extra.get("speak"))
            if key in config.FRIEND_HIDDEN_VOICES and R.get("friend"):
                c_ow[R["friend"]] = PO(**FRIEND_DENY)
            # сначала по сохранённому ID (канал могли переименовать или перенести), потом по названию
            ch = bot.ch(guild, key)
            want = discord.TextChannel if ctype == "text" else discord.VoiceChannel
            if not isinstance(ch, want):
                ch = discord.utils.get(category.channels, name=name)
            if not ch:
                for old in LEGACY.get(key, []):
                    ch = discord.utils.get(guild.channels, name=old)
                    if ch:
                        await ch.edit(name=name, category=category)
                        break
            mapped = bool(ch and db.gget(guild, f"chmap:{key}"))  # канал семьи, привязанный в мастере — не трогаем
            if ctype == "text":
                if not ch:
                    ch = await guild.create_text_channel(name, category=category, overwrites=c_ow,
                                                         topic=extra.get("topic"),
                                                         slowmode_delay=extra.get("slowmode", 0))
                elif not mapped:
                    await ch.edit(overwrites=c_ow, topic=extra.get("topic") or ch.topic)
            else:
                if not ch:
                    ch = await guild.create_voice_channel(name, category=category, overwrites=c_ow,
                                                          user_limit=extra.get("limit", 0),
                                                          bitrate=int(guild.bitrate_limit))
                elif not mapped:
                    await ch.edit(overwrites=c_ow)
            db.gset(guild, f"ch:{key}", ch.id)
        await progress(f"✅ {cat['name']}")
    await hide_voices_from_friends(bot, guild)  # привязанные каналы семьи сборка не трогает — им запрет отдельно
    await progress("✅ Права доступа выставлены по ролям")

    if not home:
        return
    afk = bot.ch(guild, "afk")
    try:
        flags = guild.system_channel_flags
        flags.join_notifications = False
        await guild.edit(afk_channel=afk, afk_timeout=900, system_channel_flags=flags)
    except discord.HTTPException:
        pass


async def apply_branding(bot, guild, progress, force=False):
    if not bot.is_home(guild):
        return  # герб и аватар бота — домашней семьи; чужой сервер не трогаем
    if bot.db.gget(guild, "branding_done") and not force:
        return
    emblem = (config.ASSETS / "emblem.png").read_bytes()
    try:
        await guild.edit(icon=emblem)
        await progress("✅ Иконка сервера — герб семьи")
    except discord.HTTPException as e:
        log.warning("Иконка: %s", e)
    if "BANNER" in guild.features:
        try:
            await guild.edit(banner=(config.ASSETS / "banner.png").read_bytes())
            await progress("✅ Баннер сервера")
        except discord.HTTPException:
            pass
    if "INVITE_SPLASH" in guild.features:
        try:
            await guild.edit(splash=(config.ASSETS / "splash.png").read_bytes())
        except discord.HTTPException:
            pass
    if force or not bot.db.get("bot_avatar_set"):
        try:
            await bot.user.edit(avatar=emblem)
            bot.db.set("bot_avatar_set", 1)
            await progress("✅ Аватар бота — герб семьи")
        except discord.HTTPException:
            pass
    bot.db.gset(guild, "branding_done", 1)


class SelfRoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"selfrole:(?P<key>n_\w+)"):
    def __init__(self, key):
        super().__init__(discord.ui.Button(label=config.ROLE_NAMES.get(key, key), style=discord.ButtonStyle.secondary,
                                           custom_id=f"selfrole:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["key"])

    async def callback(self, inter: discord.Interaction):
        role = inter.client.role(inter.guild, self.key)
        if not role:
            return await inter.response.send_message("Роль не найдена, запусти /настройка.", ephemeral=True)
        if role in inter.user.roles:
            await inter.user.remove_roles(role)
            await inter.response.send_message(f"🔕 {role.name} — выключено", ephemeral=True)
        else:
            await inter.user.add_roles(role)
            await inter.response.send_message(f"🔔 {role.name} — включено", ephemeral=True)


# ─────────────── мастер /настройка ───────────────
def can_manage(bot, inter):
    return bool(inter.guild) and (inter.user.id == inter.guild.owner_id or bot.has(inter.user, ["leader", "deputy"])
                                  or inter.user.guild_permissions.administrator)


async def deny(inter):
    await inter.response.send_message("⛔ Только для владельца сервера, Главы или заместителя.", ephemeral=True)


CH_CATS = [c for c in config.STRUCTURE if c["channels"]]  # категории, где есть каналы для привязки
CH_PAGE = 3


def render_chmap(bot, guild, ci):
    cat = CH_CATS[ci]
    lines = [f"**📺 Привязка каналов · {cat['name']}**",
             "Если у семьи уже есть такой канал — выбери его: бот будет им пользоваться и не тронет его права. "
             "Непривязанные `/настройка` создаст сама.", ""]
    for ctype, key, name, _ in cat["channels"]:
        c = bot.ch(guild, key)
        if c and bot.db.gget(guild, f"chmap:{key}"):
            lines.append(f"✅ {name} → {c.mention}")
        elif c:
            lines.append(f"🤖 {name} — канал бота {c.mention}")
        else:
            lines.append(f"▫️ {name} — бот создаст")
    return "\n".join(lines)[:1900]


class ChanCatSelect(discord.ui.Select):
    def __init__(self, ci):
        opts = [discord.SelectOption(label=c["name"][:100], value=str(i), default=i == ci) for i, c in enumerate(CH_CATS)]
        super().__init__(placeholder="Категория…", options=opts[:25], row=0)

    async def callback(self, inter: discord.Interaction):
        ci = int(self.values[0])
        await inter.response.edit_message(content=render_chmap(inter.client, inter.guild, ci),
                                          view=ChanMapView(inter.client, inter.guild, ci))


class ChanMapSelect(discord.ui.ChannelSelect):
    def __init__(self, bot, guild, key, name, ctype, row):
        self.key, self.name = key, name
        cur = bot.ch(guild, key) if bot.db.gget(guild, f"chmap:{key}") else None
        types = [discord.ChannelType.text, discord.ChannelType.news] if ctype == "text" else \
            [discord.ChannelType.voice, discord.ChannelType.stage_voice]
        super().__init__(placeholder=f"{name} — какой канал у вас?"[:150], channel_types=types, min_values=0,
                         max_values=1, row=row,
                         default_values=[discord.Object(cur.id, type=discord.abc.GuildChannel)] if cur else [])

    async def callback(self, inter: discord.Interaction):
        bot, g = inter.client, inter.guild
        if not can_manage(bot, inter):
            return await deny(inter)
        if self.values:
            c = self.values[0]
            bot.db.gset(g, f"ch:{self.key}", c.id)
            bot.db.gset(g, f"chmap:{self.key}", 1)
            msg = f"✅ {self.name} → <#{c.id}>"
        else:
            bot.db.gdel(g, f"chmap:{self.key}")
            bot.db.gdel(g, f"ch:{self.key}")
            msg = f"↩️ {self.name}: привязка снята — `/настройка` создаст свой канал"
        log.warning("Каналы «%s»: %s (%s)", g.name, msg, inter.user)
        await inter.response.send_message(msg, ephemeral=True)


class ChanMapView(discord.ui.View):
    def __init__(self, bot, guild, ci=0, page=0):
        super().__init__(timeout=600)
        self.ci, self.page = ci, page
        chans = CH_CATS[ci]["channels"]
        pages = max(1, (len(chans) + CH_PAGE - 1) // CH_PAGE)
        self.add_item(ChanCatSelect(ci))
        part = chans[page * CH_PAGE:(page + 1) * CH_PAGE]  # ряд 0 — категории, 1–3 — каналы, 4 — листалка
        for i, (ctype, key, name, _) in enumerate(part):
            self.add_item(ChanMapSelect(bot, guild, key, name, ctype, row=i + 1))
        if pages > 1:
            prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=page == 0, row=4)
            nxt = discord.ui.Button(label=f"▶ {page + 1}/{pages}", style=discord.ButtonStyle.secondary,
                                    disabled=page >= pages - 1, row=4)
            prev.callback, nxt.callback = self._go(page - 1), self._go(page + 1)
            self.add_item(prev)
            self.add_item(nxt)

    def _go(self, page):
        async def cb(inter: discord.Interaction):
            await inter.response.edit_message(content=render_chmap(inter.client, inter.guild, self.ci),
                                              view=ChanMapView(inter.client, inter.guild, self.ci, page))
        return cb


def audit(bot, guild):
    """Быстрая проверка: права бота, положение его роли, дубли ролей и каналов. Возвращает (строки, есть_проблемы)."""
    me, out, bad = guild.me, [], False
    p = me.guild_permissions
    if p.administrator:
        out.append("✅ У бота права администратора")
    else:
        need = ["manage_roles", "manage_channels", "moderate_members", "move_members", "manage_messages",
                "kick_members", "ban_members", "manage_guild"]
        miss = [n for n in need if not getattr(p, n)]
        out.append("⚠️ Боту не хватает прав: " + ", ".join(miss) if miss else "✅ Права бота в порядке")
        bad |= bool(miss)
    above = [r for r in guild.roles if r > me.top_role and not r.managed]
    ours = set()
    for key, *_ in config.ROLES:
        rid = bot.db.gget(guild, f"role:{key}")
        if rid:
            ours.add(int(rid))
    blocked = [r.name for r in above if r.id in ours]
    if blocked:
        out.append("❌ Роли семьи выше роли бота — их он выдать не сможет: " + ", ".join(blocked[:5])
                   + ". Перетащи роль бота выше в настройках сервера.")
        bad = True
    elif above:
        out.append(f"💡 Над ролью бота ещё {len(above)} роль(ей) — это нормально, если это не роли семьи")
    else:
        out.append("✅ Роль бота на самом верху")
    names = {}
    for r in guild.roles:
        names.setdefault(r.name, 0)
        names[r.name] += 1
    dup = [n for n in config.ROLE_NAMES.values() if names.get(n, 0) > 1]
    if dup:
        out.append("⚠️ Дубли ролей: " + ", ".join(dup[:5]) + " — лишние можно удалить")
        bad = True
    cnames = {}
    for c in guild.channels:
        cnames[c.name] = cnames.get(c.name, 0) + 1
    want = {name for cat in config.STRUCTURE for _, _, name, _ in cat["channels"]}
    cdup = [n for n in want if cnames.get(n, 0) > 1]
    if cdup:
        out.append("⚠️ Дубли каналов: " + ", ".join(sorted(cdup)[:5]))
        bad = True
    if not bot.intents.members:
        out.append("⚠️ Выключен интент участников (Server Members Intent) — роли и активность работать не будут")
        bad = True
    return out, bad


def wizard_embed(bot, guild):
    db, fam = bot.db, bot.family(guild)
    prof = bool(db.gget(guild, "family:name")) or bot.is_home(guild)
    fam_mod = bot.get_cog("Families")
    nroles = sum(1 for k, *_ in config.ROLES if db.gget(guild, f"rolemap:{k}"))
    nchan = sum(1 for cat in CH_CATS for _, key, *_ in cat["channels"] if db.gget(guild, f"chmap:{key}"))
    built = sum(1 for cat in CH_CATS for _, key, *_ in cat["channels"] if bot.ch(guild, key))
    total = sum(len(cat["channels"]) for cat in CH_CATS)
    from core.family import MODULES, module_on
    mods = sum(module_on(db, guild.id, m) for m in MODULES)
    ai = bot.ai_for(guild)
    _, bad = audit(bot, guild)
    steps = [
        ("1", "🏠 Семья", prof, f"«{fam['name']}», сервер {fam['server']}" if prof else "заполни название и сервер"),
        ("2", "🎭 Роли", True, f"привязано своих ролей: {nroles}" if nroles else "свои роли не привязаны — бот создаст"),
        ("3", "📺 Каналы", True, f"привязано своих каналов: {nchan}" if nchan else "свои каналы не привязаны — бот создаст"),
        ("4", "⚙️ Модули", True, f"включено модулей: {mods}/{len(MODULES)}"),
        ("5", "🤖 ИИ", ai.on, ai.name if ai.on else "нет ключа — ИИ-функции выключены"),
        ("6", "🔐 Проверка", not bad, "замечаний нет" if not bad else "есть замечания — открой"),
        ("7", "🏗 Собрать сервер", built >= total, f"каналов на месте: {built}/{total}"),
    ]
    lines = ["Проходи шаги по порядку, любой можно повторить. Выбор в шагах сохраняется сразу, а роли и каналы "
             "на сервере создаются только кнопкой «Собрать сервер».", ""]
    for n, title, ok, note in steps:
        lines.append(f"{'✅' if ok else '▫️'} **{n}. {title}** — {note}")
    if not fam_mod:
        lines.append("\n⚠️ Модуль семей не загружен — шаги 1–2 недоступны.")
    return bot.embed("🧙 Мастер настройки", "\n".join(lines), config.COLOR, guild)


class WizardView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=1800)
        self.cog = cog
        for label, emoji, fn, style, row in (
                ("Семья", "🏠", self.s_profile, discord.ButtonStyle.secondary, 0),
                ("Роли", "🎭", self.s_roles, discord.ButtonStyle.secondary, 0),
                ("Каналы", "📺", self.s_channels, discord.ButtonStyle.secondary, 0),
                ("Модули", "⚙️", self.s_modules, discord.ButtonStyle.secondary, 0),
                ("ИИ", "🤖", self.s_ai, discord.ButtonStyle.secondary, 1),
                ("Проверка", "🔐", self.s_audit, discord.ButtonStyle.secondary, 1),
                ("Собрать сервер", "🏗", self.s_build, discord.ButtonStyle.success, 1),
                ("Обновить", "🔄", self.s_refresh, discord.ButtonStyle.secondary, 1)):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
            b.callback = fn
            self.add_item(b)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if can_manage(inter.client, inter):
            return True
        await deny(inter)
        return False

    async def s_refresh(self, inter):
        await inter.response.edit_message(embed=wizard_embed(inter.client, inter.guild), view=self)

    async def s_profile(self, inter):
        from cogs.families import ProfileModal
        await inter.response.send_modal(ProfileModal(inter.client, inter.guild))

    async def s_roles(self, inter):
        from cogs.families import RoleMapView, render_map
        await inter.response.send_message(render_map(inter.client, inter.guild), view=RoleMapView(inter.client, inter.guild),
                                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def s_channels(self, inter):
        await inter.response.send_message(render_chmap(inter.client, inter.guild, 0),
                                          view=ChanMapView(inter.client, inter.guild, 0), ephemeral=True)

    async def s_modules(self, inter):
        from cogs.control import ModulesView, render
        await inter.response.send_message(render(inter.client, inter.guild), view=ModulesView(inter.client, inter.guild),
                                          ephemeral=True)

    async def s_ai(self, inter):
        bot, g = inter.client, inter.guild
        if bot.is_home(g):
            ai = bot.ai_for(g)
            return await inter.response.send_message(
                f"🏠 Домашний сервер: ИИ работает на ключах хоста — **{ai.name if ai.on else 'ключей нет'}**.",
                ephemeral=True)
        from cogs.control import KeyModal
        await inter.response.send_modal(KeyModal(bot))

    async def s_audit(self, inter):
        lines, _ = audit(inter.client, inter.guild)
        v = discord.ui.View(timeout=600)
        b = discord.ui.Button(label="Права по каналам", emoji="🔐", style=discord.ButtonStyle.secondary)

        async def perms(i):
            await self.cog.perms_cmd.callback(self.cog, i)
        b.callback = perms
        v.add_item(b)
        await inter.response.send_message("**🔐 Проверка сервера**\n" + "\n".join(lines), view=v, ephemeral=True)

    async def s_build(self, inter):
        if not inter.user.guild_permissions.administrator:
            return await inter.response.send_message("⛔ Собрать сервер может только администратор.", ephemeral=True)
        await self.cog.build(inter)


class ServerSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ensured = False

    @commands.Cog.listener()
    async def on_ready(self):
        """После обновления: досоздать новые каналы и обновить панели, не заставляя жать «Собрать сервер»."""
        if self._ensured:
            return
        self._ensured = True
        hub = self.bot.get_cog("Hub")
        for g in list(self.bot.home_guilds):
            try:
                made = await ensure_new_channels(self.bot, g)
                if made:
                    log.info("«%s»: созданы каналы %s", g.name, ", ".join(made))
                if self.bot.db.gget(g, "role:leader"):
                    hidden = await hide_voices_from_friends(self.bot, g)
                    if hidden:
                        log.info("«%s»: войсов скрыто от друзей семьи — %s", g.name, hidden)
                if hub and self.bot.db.gget(g, "role:leader"):
                    await hub.post_panels(g, False)  # панели с новой версией сами переставятся
            except Exception:
                log.exception("Досоздание каналов на %s", g.name)

    async def open_wizard(self, inter: discord.Interaction):
        if not can_manage(self.bot, inter):
            return await deny(inter)
        await inter.response.send_message(embed=wizard_embed(self.bot, inter.guild), view=WizardView(self),
                                          ephemeral=True)

    async def refresh_texts(self, guild):
        """Правила и «о семье» поменяли в /настройки — правим закреплённые сообщения на месте (картинки остаются)."""
        bot = self.bot
        for key, title, body, color in (("rules", f"📜 Правила семьи {config.FAMILY}", bot.rules_text(guild), config.COLOR),
                                        ("welcome", f"🏛 Семья {config.FAMILY}", bot.welcome_text(guild), config.GOLD)):
            c, mid = bot.ch(guild, key), bot.db.gget(guild, f"panel:{key}")
            if not c or not mid:
                continue
            try:
                msg = await c.fetch_message(int(mid))
                e = bot.embed(title, body[:4000], color, guild)
                if msg.embeds and msg.embeds[0].image:
                    e.set_image(url=msg.embeds[0].image.url)
                if msg.embeds and msg.embeds[0].thumbnail:
                    e.set_thumbnail(url=msg.embeds[0].thumbnail.url)
                await msg.edit(embed=e)
            except (discord.HTTPException, ValueError):
                pass

    async def post_panels(self, guild, repost):
        bot = self.bot

        async def rules():
            e = bot.embed(f"📜 Правила семьи {config.FAMILY}", bot.rules_text(guild)[:4000], config.COLOR, guild)
            e.set_image(url="attachment://banner.png")
            return {"embed": e, "file": discord.File(config.ASSETS / "banner.png", filename="banner.png")}

        async def welcome():
            e = bot.embed(f"🏛 Семья {config.FAMILY}", bot.welcome_text(guild)[:4000], config.GOLD, guild)
            e.set_thumbnail(url="attachment://emblem.png")
            return {"embed": e, "file": discord.File(config.ASSETS / "emblem.png", filename="emblem.png")}

        async def selfroles():
            v = discord.ui.View(timeout=None)
            for k in ("n_gather", "n_events", "n_news", "n_market", "n_streams"):
                v.add_item(SelfRoleButton(k))
            e = bot.embed("🔔 Уведомления",
                          "Нажми кнопку, чтобы включить или выключить пинги:\n\n"
                          "🔔 **Сборы** — когда собираемся в игре\n🔔 **Мероприятия** — ивенты, конкурсы, розыгрыши\n"
                          "🔔 **Новости** — важные объявления\n🔔 **Рынок** — новые объявления на рынке семьи\n"
                          "🔔 **Эфиры** — когда кто-то из семьи выходит в эфир",
                          config.COLOR, guild)
            return {"embed": e, "view": v}

        await bot.panel_once(guild, "rules", "rules", repost, rules, version=3)
        await bot.panel_once(guild, "welcome", "welcome", repost, welcome, version=3)
        await bot.panel_once(guild, "selfroles", "selfroles", repost, selfroles, version=4)

    @app_commands.command(name="настройка", description="Мастер настройки: семья, роли, каналы, модули, ИИ, сборка сервера")
    @app_commands.rename(repost="перепостить_панели", branding="обновить_оформление")
    @app_commands.describe(repost="Сразу собрать сервер и заново отправить все панели (без мастера)",
                           branding="Сразу собрать сервер и заново поставить герб на иконку и баннер (без мастера)")
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(self, inter: discord.Interaction, repost: bool = False, branding: bool = False):
        if repost or branding:  # старый путь: сразу собрать
            if not inter.user.guild_permissions.administrator:
                return await inter.response.send_message("⛔ Только для администратора.", ephemeral=True)
            return await self.build(inter, repost, branding)
        await self.open_wizard(inter)

    async def build(self, inter: discord.Interaction, repost=False, branding=False):
        """Сборка сервера (роли, каналы, права, оформление, панели) с прогрессом в эфемерном сообщении."""
        await inter.response.defer(ephemeral=True, thinking=True)
        lines = []

        async def progress(t):
            lines.append(t)
            try:
                await inter.edit_original_response(content="⚙️ Собираю сервер...\n" + "\n".join(lines[-25:]))
            except discord.HTTPException:
                pass

        try:
            await build_server(self.bot, inter.guild, progress)
            await apply_branding(self.bot, inter.guild, progress, force=branding)
            await self.post_panels(inter.guild, repost)
            for cog in self.bot.cogs.values():
                if cog is not self and hasattr(cog, "post_panels"):
                    try:
                        await cog.post_panels(inter.guild, repost)
                    except Exception:
                        log.exception("Панель модуля %s", type(cog).__name__)
            await progress("✅ Панели отправлены")
            await inter.edit_original_response(
                content="🎉 **Сервер готов!**\n" + "\n".join(lines[-25:]) +
                        "\n\nВыдай себе роль 👑 Глава семьи. Стандартные каналы Discord («основной» и т.п.) можно удалить."
                        "\nДля ИИ-юриста загрузи правила проекта: `/база правила-проекта`")
        except discord.Forbidden:
            await inter.edit_original_response(content="❌ Боту не хватает прав. Дай ему роль с правами Администратора "
                                                       "и перетащи её на самый верх списка ролей.")

    @app_commands.command(name="права", description="Проверить доступ: какие роли что видят, где пишут и говорят")
    @app_commands.default_permissions(administrator=True)
    async def perms_cmd(self, inter: discord.Interaction):
        g, bot = inter.guild, self.bot
        check = [("guest", "👤"), ("friend", "🤝"), ("newbie", "🆕"), ("member", "👥"), ("senior", "🛡"),
                 ("recruiter", "📨"), ("council", "🏛")]
        roles = [(e, bot.role(g, k)) for k, e in check]

        def who(ch, attr):
            out = "".join(e for e, r in roles if r and getattr(ch.permissions_for(r), attr))
            return out or "—"

        def line(ch):
            if isinstance(ch, discord.VoiceChannel):
                return f"👁 {who(ch, 'view_channel')} · 🎙 {who(ch, 'speak')}"
            return f"👁 {who(ch, 'view_channel')} · ✍️ {who(ch, 'send_messages')}"

        e = bot.embed("🔐 Права доступа по ролям",
                      "👤 Гость · 🤝 Друг · 🆕 Новичок · 👥 Член · 🛡 Старший · 📨 Рекрутер · 🏛 Совет\n"
                      "Глава и Заместитель видят всё (у них права администратора) — поэтому ты как Глава видишь все "
                      "каналы. Проверить глазами: Настройки сервера → Роли → ⋯ у роли → «Просмотреть сервер как роль».",
                      config.COLOR, g)
        for cat in config.STRUCTURE:
            category = find_category(bot, g, cat)
            if not category:
                continue
            base = line(category)
            extra = [f"`{c.name[:28]}` {line(c)}" for c in category.channels if line(c) != base]
            e.add_field(name=cat["name"], value=(base + ("\n" + "\n".join(extra) if extra else ""))[:1024],
                        inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        bot = self.bot
        if member.bot:
            return
        guest = bot.role(member.guild, "guest")
        if guest:
            try:
                await member.add_roles(guest)
            except discord.HTTPException:
                pass
        c = bot.ch(member.guild, "guest_q")
        apply = bot.ch(member.guild, "apply")
        if c:
            try:
                avatar = await member.display_avatar.replace(size=256, static_format="png").read()
            except discord.HTTPException:
                avatar = None
            card = await asyncio.to_thread(images.welcome_card, avatar, "Добро пожаловать", member.display_name,
                                           f"Гость №{member.guild.member_count} · семья "
                                           f"{self.bot.family(member.guild)['name']}")
            await c.send(content=f"👋 {member.mention}, рады видеть! Хочешь к нам — жми кнопку в "
                                 f"{apply.mention if apply else '#подать-заявку'}. Вопросы — пиши сюда.",
                         file=discord.File(card, filename="welcome.png"))
        days = (discord.utils.utcnow() - member.created_at).days
        await bot.log(member.guild, f"📥 {member.mention} зашёл на сервер (аккаунту {days} дн."
                                    f"{' ⚠️' if days < 7 else ''})", color=config.GREEN)


async def setup(bot):
    bot.add_dynamic_items(SelfRoleButton)
    await bot.add_cog(ServerSetup(bot))
