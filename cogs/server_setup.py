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
        elif key != "leader" and role.permissions != discord.Permissions(**perms) and role < guild.me.top_role:
            try:
                await role.edit(permissions=discord.Permissions(**perms), reason="Права роли по настройке")
            except discord.HTTPException:
                pass
        R[key] = role
        db.gset(guild, f"role:{key}", role.id)
    await progress(f"✅ Роли ({len(R)})")

    await progress(await order_roles(guild, [R[k].id for k, *_ in config.ROLES]))

    try:
        p = guild.default_role.permissions
        p.update(mention_everyone=False)
        await guild.default_role.edit(permissions=p, reason="Только руководство может пинговать @everyone")
    except discord.HTTPException:
        pass

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
            if ctype == "text":
                if not ch:
                    ch = await guild.create_text_channel(name, category=category, overwrites=c_ow,
                                                         topic=extra.get("topic"),
                                                         slowmode_delay=extra.get("slowmode", 0))
                else:
                    await ch.edit(overwrites=c_ow, topic=extra.get("topic") or ch.topic)
            else:
                if not ch:
                    ch = await guild.create_voice_channel(name, category=category, overwrites=c_ow,
                                                          user_limit=extra.get("limit", 0),
                                                          bitrate=int(guild.bitrate_limit))
                else:
                    await ch.edit(overwrites=c_ow)
            db.gset(guild, f"ch:{key}", ch.id)
        await progress(f"✅ {cat['name']}")
    await progress("✅ Права доступа выставлены по ролям")

    afk = bot.ch(guild, "afk")
    try:
        flags = guild.system_channel_flags
        flags.join_notifications = False
        await guild.edit(afk_channel=afk, afk_timeout=900, system_channel_flags=flags)
    except discord.HTTPException:
        pass


async def apply_branding(bot, guild, progress, force=False):
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


class ServerSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def post_panels(self, guild, repost):
        bot = self.bot

        async def rules():
            e = bot.embed(f"📜 Правила семьи {config.FAMILY}", config.RULES_TEXT, config.COLOR, guild)
            e.set_image(url="attachment://banner.png")
            return {"embed": e, "file": discord.File(config.ASSETS / "banner.png", filename="banner.png")}

        async def welcome():
            e = bot.embed(f"🏛 Семья {config.FAMILY}", config.WELCOME_TEXT, config.GOLD, guild)
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

    @app_commands.command(name="настройка", description="Собрать/обновить сервер: роли, каналы, права, оформление, панели")
    @app_commands.rename(repost="перепостить_панели", branding="обновить_оформление")
    @app_commands.describe(repost="Заново отправить все панели (правила, заявки и т.д.)",
                           branding="Заново поставить герб на иконку сервера и баннер")
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(self, inter: discord.Interaction, repost: bool = False, branding: bool = False):
        if not inter.user.guild_permissions.administrator:
            return await inter.response.send_message("⛔ Только для администратора.", ephemeral=True)
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
            card = images.welcome_card(avatar, "Добро пожаловать", member.display_name,
                                       f"Гость №{member.guild.member_count} · семья {config.FAMILY}")
            await c.send(content=f"👋 {member.mention}, рады видеть! Хочешь к нам — жми кнопку в "
                                 f"{apply.mention if apply else '#подать-заявку'}. Вопросы — пиши сюда.",
                         file=discord.File(card, filename="welcome.png"))
        days = (discord.utils.utcnow() - member.created_at).days
        await bot.log(member.guild, f"📥 {member.mention} зашёл на сервер (аккаунту {days} дн."
                                    f"{' ⚠️' if days < 7 else ''})", color=config.GREEN)


async def setup(bot):
    bot.add_dynamic_items(SelfRoleButton)
    await bot.add_cog(ServerSetup(bot))
