# -*- coding: utf-8 -*-
"""Альянс семей Арбатского.
Бот стоит на серверах всех семей альянса. На чужих серверах работает только этот модуль (см. FamilyBot.dispatch).
• Общий чат: пишешь в #альянс-чат у себя — сообщение появляется у всех с ником и семьёй (через вебхуки).
• Совет альянса: главы семей голосуют кнопками, у каждой семьи один голос, решение — большинством.
• Общий чёрный список: виден всем, бот предупреждает при входе и при заявке.
• Тревога альянса, устав, летопись, вступление новой семьи через голосование.
• Автороль «🤝 <Семья>» у нас для участников союзных семей — бот сам видит их роль на их сервере.
• Семья БЕЗ нашего бота (external=1): живёт только в штабе. guild_id — условный отрицательный номер (EXT_BASE и ниже),
  участники — у кого роль «🤝 <Семья>» в штабе (её выдаёт руководство этой семьи: /альянс участники), руководство —
  список leaders (Discord ID), голосует из штаба. Вступает так же — голосованием совета (/альянс добавить-семью).
  ВАЖНО: bot.is_home(None) == True — для таких семей проверять is_ext() раньше, чем is_home(get_guild(...))."""
import asyncio
import datetime as dt
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, parse_dt

log = logging.getLogger("family-bot.alliance")
A = config.ALLIANCE_NAME
COLOR = config.ALLIANCE_COLOR
KINDS = {"join": "Принять семью", "kick": "Исключить семью", "war": "Объявить войну", "peace": "Заключить мир",
         "charter": "Изменить устав", "other": "Другое решение"}
CHOICES = {"yes": ("За", "✅", discord.ButtonStyle.success), "no": ("Против", "❌", discord.ButtonStyle.danger),
           "abs": ("Воздержаться", "➖", discord.ButtonStyle.secondary)}
DEFAULT_CHARTER = (
    f"**Устав «{A}»**\n\n"
    "1. Семьи альянса равны. У каждой семьи один голос в совете альянса.\n"
    "2. Решения совета принимаются большинством голосов семей.\n"
    "3. Семьи альянса не воюют друг с другом и решают споры через совет.\n"
    "4. Семьи помогают друг другу по тревоге альянса, если это не нарушает правил проекта.\n"
    "5. Общий чёрный список обязателен для всех: человека из ЧС альянса не принимают ни в одну семью.\n"
    "6. Новая семья вступает только после голосования совета.\n"
    "7. Семья может выйти из альянса, предупредив совет. Исключение — голосованием.")
BOT_PERMS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                                read_message_history=True, manage_channels=True, manage_roles=True,
                                manage_webhooks=True, mention_everyone=True, add_reactions=True, connect=True,
                                speak=True, move_members=True, create_instant_invite=True)
EXT_BASE = -1000  # условные номера семей без бота: -1000, -1001, … (не пересекаются с ID серверов и ORPHAN=-1)
ALARM_COOLDOWN = 600


def tally(rows):
    t = {"yes": 0, "no": 0, "abs": 0}
    for r in rows:
        t[r["choice"]] = t.get(r["choice"], 0) + 1
    return t


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"avote:(?P<c>yes|no|abs):(?P<id>\d+)"):
    def __init__(self, c, vid, disabled=False):
        label, emoji, style = CHOICES[c]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"avote:{c}:{vid}",
                                           disabled=disabled))
        self.c, self.vid = c, vid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["c"], int(match["id"]))

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Alliance").cast(inter, self.vid, self.c)


class CharterModal(discord.ui.Modal, title="📜 Новая редакция устава альянса"):
    text = discord.ui.TextInput(label="Текст устава", style=discord.TextStyle.paragraph, max_length=3800)

    def __init__(self, hours, current):
        super().__init__()
        self.hours = hours
        self.text.default = current[:3800]

    async def on_submit(self, inter: discord.Interaction):
        cog = inter.client.get_cog("Alliance")
        await inter.response.defer(ephemeral=True)
        vid = await cog.start_vote(inter.user, inter.guild, "charter", "Новая редакция устава", str(self.text),
                                   None, self.hours, fallback=inter.channel)
        await inter.followup.send(f"🗳 Голосование №{vid} по уставу запущено.\n{cog.where_sent()}", ephemeral=True)


class Alliance(commands.Cog):
    group = app_commands.Group(name="альянс", description=f"{A}: состав, совет, общий ЧС, тревога")
    blg = app_commands.Group(name="чс", description="Общий чёрный список альянса", parent=group)
    members_grp = app_commands.Group(name="участники", description="Семья без бота: кто в ней (роль в штабе)",
                                     parent=group)

    def __init__(self, bot):
        self.bot = bot
        self.hooks = {}
        self.relay = {}  # id исходного сообщения → [(сервер, id копии)] — чтобы править и удалять копии
        self.alarm_at = {}
        self.hub_lock = asyncio.Lock()
        self.vote_loop.start()
        self.sync_loop.start()

    def cog_unload(self):
        self.vote_loop.cancel()
        self.sync_loop.cancel()

    # ================= данные =================
    def fam(self, gid):
        return self.bot.db.one("SELECT * FROM alliance_families WHERE guild_id=?", gid)

    def active(self):
        return self.bot.db.all("SELECT * FROM alliance_families WHERE status='active' ORDER BY joined_at")

    # ---------- семьи без бота (живут в штабе) ----------
    @staticmethod
    def is_ext(row):
        return bool(row and row["external"])

    @staticmethod
    def ext_leaders(row):
        try:
            return {int(x) for x in json.loads(row["leaders"] or "[]")}
        except (TypeError, ValueError):
            return set()

    def hub_fam_role(self, row):
        hub = self.hub()
        rid = self.bot.db.get(f"hub:fam:{row['guild_id']}")
        return hub.get_role(int(rid)) if hub and rid else None

    def ext_member(self, row, uid):
        """В семье без бота: руководство или роль семьи в штабе."""
        if uid in self.ext_leaders(row):
            return True
        r = self.hub_fam_role(row)
        return bool(r and any(m.id == uid for m in r.members))

    def new_ext_id(self):
        r = self.bot.db.one("SELECT MIN(guild_id) m FROM alliance_families WHERE guild_id<=?", EXT_BASE)
        return min(EXT_BASE, (r["m"] or EXT_BASE + 1) - 1) if r and r["m"] is not None else EXT_BASE

    def home(self):
        return self.bot.get_guild(self.bot.home_id) if self.bot.home_id else None

    # ---------- штаб альянса (отдельный Discord-сервер) ----------
    def hub_row(self):
        return self.bot.db.one("SELECT * FROM alliance_families WHERE status='hub'")

    def hub(self):
        r = self.hub_row()
        return self.bot.get_guild(r["guild_id"]) if r else None

    def is_hub(self, guild):
        r = self.hub_row()
        return bool(guild and r and guild.id == r["guild_id"])

    def nodes(self):
        """Куда рассылать: серверы семей альянса + штаб."""
        rows = list(self.active())
        h = self.hub_row()
        return rows + [h] if h else rows

    def member_families(self, uid):
        """Семьи альянса, в которых состоит человек (бот видит это на их серверах)."""
        out = []
        for row in self.active():
            if self.is_ext(row):
                if self.ext_member(row, uid):
                    out.append(row)
                continue
            g = self.bot.get_guild(row["guild_id"])
            m = g.get_member(uid) if g else None
            if not m:
                continue
            if self.bot.is_family(g):  # сервер семьи с нашим ботом — роли семьи из её настроек
                ok = self.bot.in_family(m)
            else:
                ok = any(r.id in (row["family_role_id"], row["lead_role_id"]) for r in m.roles)
            if ok:
                out.append(row)
        return out

    def leader_family(self, uid):
        """Семья, в руководстве которой человек (для голосования из штаба)."""
        for row in self.active():
            if self.is_ext(row):
                if uid in self.ext_leaders(row):
                    return row
                continue
            g = self.bot.get_guild(row["guild_id"])
            m = g.get_member(uid) if g else None
            if not m:
                continue
            if self.bot.is_family(g):
                if self.bot.has(m, config.LEAD):
                    return row
            elif m.id == g.owner_id or any(r.id == row["lead_role_id"] for r in m.roles):
                return row
        return None

    def is_founder(self, user):
        """Глава/зам (или владелец сервера) семьи-основателя — может принимать семьи без голосования."""
        home = self.home()
        m = home.get_member(user.id) if home else None
        return bool(m and (m.id == home.owner_id or self.bot.has(m, ["leader", "deputy"])))

    async def force_accept(self, row, user):
        """Принять семью из заявки без голосования: открытые голосования по ней закрываются как принятые."""
        db = self.bot.db
        for v in db.all("SELECT id FROM alliance_votes WHERE status='open' AND kind='join' AND target=?",
                        row["guild_id"]):
            db.q("UPDATE alliance_votes SET status='passed' WHERE id=?", v["id"])
            await self.refresh(v["id"], closed=True)
        await self.activate(row)
        await self.record(f"⚡ Семья **{row['family']}** принята решением семьи-основателя без голосования "
                          f"({user.display_name}).")

    def exists(self):
        h = self.bot.home_id
        return bool(h and self.bot.db.one("SELECT 1 FROM alliance_families WHERE guild_id=? AND status='active'", h))

    def is_leader(self, member):
        if not isinstance(member, discord.Member):
            return False
        g = member.guild
        if self.bot.is_family(g):
            return self.bot.has(member, config.LEAD)
        if self.is_hub(g):
            return self.leader_family(member.id) is not None
        row = self.fam(g.id)
        if member.id == g.owner_id or member.guild_permissions.administrator:
            return True
        return bool(row and row["lead_role_id"] and any(r.id == row["lead_role_id"] for r in member.roles))

    def family_label(self, member):
        """Из какой семьи автор сообщения (у нас — союзник с ролью своей семьи подписывается своей семьёй)."""
        g = member.guild
        if self.is_hub(g):
            fams = self.member_families(member.id)
            return ", ".join(f["family"] for f in fams) or "гость штаба"
        if self.bot.is_family(g):
            for row in self.active():
                if row["ally_role_id"] and any(r.id == row["ally_role_id"] for r in member.roles):
                    return row["family"]
            return self.bot.family(g)["name"]
        row = self.fam(g.id)
        return row["family"] if row else g.name

    def members_count(self, row):
        if self.is_ext(row):
            r = self.hub_fam_role(row)
            return len({m.id for m in (r.members if r else []) if not m.bot} | self.ext_leaders(row))
        g = self.bot.get_guild(row["guild_id"])
        if not g:
            return 0
        if self.bot.is_family(g):
            return len(self.bot.family_members(g))
        r = g.get_role(row["family_role_id"]) if row["family_role_id"] else None
        return len([m for m in r.members if not m.bot]) if r else 0

    # ================= каналы =================
    async def ensure_channels(self, row):
        g = self.bot.get_guild(row["guild_id"])
        if not g:
            return None
        bot = self.bot
        PO = discord.PermissionOverwrite
        if bot.is_family(g):
            fam_roles = [bot.role(g, k) for k in config.FAMILY_RANKS]
            fam_roles += [g.get_role(r["ally_role_id"]) for r in self.active() if r["ally_role_id"]]
            lead_roles = [bot.role(g, k) for k in config.LEAD]
        else:
            fam_roles = [g.get_role(row["family_role_id"]), g.get_role(row["lead_role_id"])]
            lead_roles = [g.get_role(row["lead_role_id"])]
        fam_roles = [r for r in fam_roles if r]
        lead_roles = [r for r in lead_roles if r]
        me = PO(view_channel=True, send_messages=True, manage_channels=True, manage_webhooks=True,
                embed_links=True, attach_files=True, read_message_history=True, mention_everyone=True,
                manage_permissions=True)

        def ow(read, write):
            o = {g.default_role: PO(view_channel=False), g.me: me}
            for r in read:
                o[r] = PO(view_channel=True, read_message_history=True, send_messages=r in write,
                          attach_files=r in write, embed_links=r in write, add_reactions=True)
            return o

        spec = [("chat_id", "💬・альянс-чат", ow(fam_roles, fam_roles),
                 f"Общий чат {A}: сообщения видят все семьи альянса"),
                ("news_id", "📢・альянс-новости", ow(fam_roles, []), f"Новости, решения совета и тревоги {A}"),
                ("council_id", "🏛・совет-альянса", ow(lead_roles, lead_roles),
                 "Голосования совета альянса: один голос от семьи"),
                ("bl_id", "⛔・чс-альянса", ow(lead_roles, []), "Общий чёрный список альянса")]
        try:
            cat = g.get_channel(row["cat_id"]) if row["cat_id"] else None
            if not cat:
                cat = await g.create_category(f"🤝 {A}", overwrites={g.default_role: PO(view_channel=False),
                                                                    g.me: me}, reason=A)
            ids = {"cat_id": cat.id}
            for col, name, overwrites, topic in spec:
                ch = g.get_channel(row[col]) if row[col] else None
                if ch:
                    await ch.edit(overwrites=overwrites, category=cat)
                else:
                    ch = await g.create_text_channel(name, category=cat, overwrites=overwrites, topic=topic, reason=A)
                ids[col] = ch.id
            hook = row["webhook"]
            chat = g.get_channel(ids["chat_id"])
            if not hook or not self.hooks.get(row["guild_id"]):
                hooks = [w for w in await chat.webhooks() if w.user and w.user.id == bot.user.id]
                wh = hooks[0] if hooks else await chat.create_webhook(name=A[:80], reason=A)
                hook = wh.url
                self.hooks[row["guild_id"]] = wh
            sets = ", ".join(f"{k}=?" for k in ids)
            bot.db.q(f"UPDATE alliance_families SET {sets}, webhook=? WHERE guild_id=?", *ids.values(), hook,
                     row["guild_id"])
        except discord.HTTPException as e:
            log.warning("Альянс: каналы на «%s»: %s", g.name, e)
            return None
        return self.fam(row["guild_id"])

    async def ensure_ally_role(self, row):
        home = self.home()
        if not home or (not self.is_ext(row) and self.bot.is_home(self.bot.get_guild(row["guild_id"]))):
            return
        r = home.get_role(row["ally_role_id"]) if row["ally_role_id"] else None
        if not r:
            try:
                r = await home.create_role(name=f"🤝 {row['family']}", color=COLOR, hoist=False, mentionable=False,
                                           reason=f"{A}: союзная семья")
            except discord.HTTPException:
                return
            self.bot.db.q("UPDATE alliance_families SET ally_role_id=? WHERE guild_id=?", r.id, row["guild_id"])

    def channel(self, row, col):
        g = self.bot.get_guild(row["guild_id"])
        return g.get_channel(row[col]) if g and row[col] else None

    async def broadcast(self, col, embed=None, content=None, view_factory=None, ping=False, only=None):
        """Во все серверы альянса (семьи с ботом + штаб). Нет канала — один раз пересобираем каналы и пробуем снова.
        self.last_failed — [(сервер, почему)] для ответа автору."""
        sent, self.last_failed = [], []
        for row in self.nodes():
            if only and row["guild_id"] not in only:
                continue
            if self.is_ext(row):
                continue  # у семьи без бота своих каналов нет — она всё видит в штабе
            ch = self.channel(row, col)
            g = self.bot.get_guild(row["guild_id"])
            if not ch and g:
                try:  # канал удалили/пересоздали руками — восстанавливаем
                    fixed = await (self.build_hub(g) if row["status"] == "hub" else self.ensure_channels(row))
                    ch = self.channel(fixed, col) if fixed else None
                except Exception:
                    log.exception("Альянс: восстановление каналов на «%s»", g.name)
            if not ch:
                why = "бот не видит этот сервер" if not g else "нет канала и не вышло его создать (права бота?)"
                self.last_failed.append((row["family"], why))
                log.error("Альянс: рассылка %s — «%s»: %s", col, row["family"], why)
                continue
            kw = {"embed": embed, "allowed_mentions": discord.AllowedMentions(everyone=ping)}
            text = ("@here " if ping else "") + (content or "")
            if text:
                kw["content"] = text
            if view_factory:
                kw["view"] = view_factory()
            try:
                m = await ch.send(**kw)
                sent.append((ch.id, m.id))
            except discord.HTTPException as e:
                self.last_failed.append((row["family"], f"Discord не дал написать в #{ch.name}: {str(e)[:80]}"))
                log.error("Альянс: рассылка в «%s» #%s: %s", row["family"], ch.name, e)
        return sent

    def embed(self, title, text, color=COLOR):
        e = discord.Embed(title=title, description=text, color=color, timestamp=now())
        e.set_footer(text=A)
        return e

    async def record(self, text):
        self.bot.db.q("INSERT INTO alliance_log(text, created_at) VALUES(?,?)", text, now_iso())
        await self.broadcast("news_id", embed=self.embed("📜 Летопись альянса", text))

    # ================= общий чат =================
    async def webhook(self, row):
        wh = self.hooks.get(row["guild_id"])
        if wh:
            return wh
        if not row["webhook"]:
            return None
        wh = discord.Webhook.from_url(row["webhook"], client=self.bot)
        self.hooks[row["guild_id"]] = wh
        return wh

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        row = self.fam(message.guild.id)
        if not row or row["status"] not in ("active", "hub") or message.channel.id != row["chat_id"]:
            return
        label = self.family_label(message.author)
        name = f"{message.author.display_name[:48]} · {label}"[:80]
        text = self.relay_text(message)
        copies = []
        files_src = [a for a in message.attachments if a.size <= 8 * 1024 * 1024][:4]
        if not text.strip() and not files_src:
            return
        for other in self.nodes():
            if other["guild_id"] == message.guild.id:
                continue
            wh = await self.webhook(other)
            if not wh:
                continue
            files = []
            for a in files_src:
                try:
                    files.append(await a.to_file())
                except discord.HTTPException:
                    pass
            try:
                m = await wh.send(content=text[:2000] or None, username=name,
                                  avatar_url=message.author.display_avatar.url, files=files or discord.utils.MISSING,
                                  allowed_mentions=discord.AllowedMentions.none(), wait=True)
                copies.append((other["guild_id"], m.id))
            except discord.NotFound:
                self.hooks.pop(other["guild_id"], None)
                self.bot.db.q("UPDATE alliance_families SET webhook=NULL WHERE guild_id=?", other["guild_id"])
                if other["status"] == "hub":
                    await self.build_hub(self.bot.get_guild(other["guild_id"]))
                else:
                    await self.ensure_channels(other)
            except discord.HTTPException as e:
                log.warning("Альянс-чат → %s: %s", other["family"], e)

        if copies:
            self.relay[message.id] = copies
            while len(self.relay) > 1000:
                self.relay.pop(next(iter(self.relay)))

    @staticmethod
    def relay_text(message):
        text = message.content or ""
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message):
            who = ref.author.display_name.split(" · ")[0]
            text = f"> **{who}:** {(ref.content or '…')[:120]}\n{text}"
        return text

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        """Автор поправил сообщение в чате альянса — правим копии у остальных семей."""
        copies = self.relay.get(after.id)
        if not copies or before.content == after.content:
            return
        text = self.relay_text(after)[:2000]
        for gid, mid in copies:
            row = self.fam(gid)
            wh = await self.webhook(row) if row else None
            if wh:
                try:
                    await wh.edit_message(mid, content=text or "…", allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        copies = self.relay.pop(payload.message_id, None)
        for gid, mid in copies or []:
            row = self.fam(gid)
            wh = await self.webhook(row) if row else None
            if wh:
                try:
                    await wh.delete_message(mid)
                except discord.HTTPException:
                    pass

    # ================= союзники у нас + ЧС при входе =================
    async def sync_member(self, member):
        """Выдаёт/снимает у нас роль «🤝 <Семья>» по роли семьи на её сервере."""
        home = self.home()
        if not home or member.guild.id != home.id or member.bot:
            return
        for row in self.active():
            if not row["ally_role_id"]:
                continue
            role = home.get_role(row["ally_role_id"])
            if not role:
                continue
            if self.is_ext(row):
                is_member = self.ext_member(row, member.id)
            else:
                g = self.bot.get_guild(row["guild_id"])
                if not g or self.bot.is_home(g):
                    continue
                fm = g.get_member(member.id)
                if self.bot.is_family(g):  # семья с нашим ботом — по её рангам
                    is_member = bool(fm and self.bot.in_family(fm))
                else:
                    is_member = bool(fm and row["family_role_id"] and any(r.id == row["family_role_id"] for r in fm.roles))
            try:
                if is_member and role not in member.roles:
                    await member.add_roles(role, reason=f"{A}: состоит в семье {row['family']}")
                elif not is_member and role in member.roles:
                    await member.remove_roles(role, reason=f"{A}: больше не в семье {row['family']}")
            except discord.HTTPException:
                pass

    async def check_bl(self, member):
        hit = self.bot.alliance_bl_find(discord_id=member.id)
        row = self.fam(member.guild.id)
        if not hit or not row or row["status"] not in ("active", "hub"):
            return
        ch = self.channel(row, "council_id")
        if ch:
            try:
                await ch.send(embed=self.embed("⛔ Зашёл человек из ЧС альянса",
                                               f"{member.mention} (`{member.id}`) — запись №{hit['id']}\n"
                                               f"**Причина:** {hit['reason']}", 0xE53935))
            except discord.HTTPException:
                pass

    async def resync(self, uid):
        """Человек вступил/вышел/сменил роли где-то в альянсе — обновляем его роли у нас и в штабе."""
        home, hub = self.home(), self.hub()
        hm = home.get_member(uid) if home else None
        if hm:
            await self.sync_member(hm)
        sm = hub.get_member(uid) if hub else None
        if sm:
            await self.sync_hub_member(sm)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not self.bot.is_family(member.guild):
            await self.check_bl(member)  # на серверах семей ЧС альянса проверяет модуль ЧС семьи
        await self.resync(member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self.resync(member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if self.is_hub(after.guild):
            if before.nick != after.nick:
                await self.sync_hub_member(after)  # сменил ник в штабе — вернём ник с сервера семьи
            return
        if before.roles == after.roles and before.display_name == after.display_name:
            return
        await self.resync(after.id)

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before.display_name != after.display_name:
            await self.resync(after.id)

    @tasks.loop(minutes=30)
    @guarded
    async def sync_loop(self):
        home = self.home()
        if not home or not self.exists():
            return
        for m in home.members:
            await self.sync_member(m)
        hub = self.hub()
        for m in hub.members if hub else []:
            await self.sync_hub_member(m)

    @sync_loop.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    # ================= команды на серверах союзников =================
    async def sync_foreign(self, guild):
        """На сервере без семьи регистрируем только /альянс и /семья (подключить семью)."""
        if self.bot.is_family(guild):
            return
        cmds = [c for c in (self.bot.tree.get_command(n) for n in ("альянс", "семья")) if c]
        if not cmds:
            return
        self.bot.tree.clear_commands(guild=guild)
        for cmd in cmds:
            self.bot.tree.add_command(cmd, guild=guild)
        try:
            await self.bot.tree.sync(guild=guild)
        except discord.HTTPException as e:
            log.warning("Альянс: команды на «%s»: %s", guild.name, e)

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            if not self.bot.is_family(g):
                await self.sync_foreign(g)
        for row in self.active():
            if row["webhook"]:
                await self.webhook(row)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.bot.is_family(guild):
            return
        await self.sync_foreign(guild)
        # приветствие (с вариантами «своя семья» / «союзник по альянсу» / «штаб») шлёт ког Families — одно на всех

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        """Бота убрали с сервера семьи — семья больше не участвует, иначе её «голос» блокировал бы решения совета."""
        row = self.fam(guild.id)
        if not row:
            return
        self.hooks.pop(guild.id, None)
        if row["status"] == "active":
            await self.leave(row, "покинула альянс (бот удалён с её сервера)")
            for v in self.bot.db.all("SELECT id FROM alliance_votes WHERE status='open'"):
                if not await self.maybe_close(v["id"]):
                    await self.refresh(v["id"])
        elif row["status"] == "pending":
            self.bot.db.q("UPDATE alliance_families SET status='rejected' WHERE guild_id=?", guild.id)
        elif row["status"] == "hub":
            self.bot.db.q("DELETE FROM alliance_families WHERE guild_id=?", guild.id)
            self.bot.db.set("hub:invite", "")

    # ================= голосования =================
    async def start_vote(self, user, guild, kind, title, text, target, hours, fallback=None):
        """Голосование совета → во все каналы «совет-альянса». Никуда не ушло — в fallback (канал команды)
        и ошибка разработчику. self.last_sent — куда разослали (для ответа автору)."""
        hours = max(1, min(int(hours or 24), 168))
        ends = now() + dt.timedelta(hours=hours)
        cur = self.bot.db.q("INSERT INTO alliance_votes(kind, title, text, target, created_by, created_gid, created_at,"
                            " ends_at) VALUES(?,?,?,?,?,?,?,?)", kind, title[:200], (text or "")[:3800], target,
                            user.id, guild.id if guild else None, now_iso(), ends.isoformat())
        vid = cur.lastrowid
        e = self.vote_embed(vid)
        sent = await self.broadcast("council_id", embed=e, view_factory=lambda: self.vote_view(vid))
        self.last_fallback = not sent
        if not sent:
            nodes = [(r["family"], r["council_id"], bool(self.bot.get_guild(r["guild_id"]))) for r in self.nodes()]
            log.error("Альянс: голосование №%s не разослано ни в один канал совета (семья, council_id, сервер виден): %s",
                      vid, nodes)
            if fallback is not None:
                try:
                    m = await fallback.send(embed=e, view=self.vote_view(vid))
                    sent = [(fallback.id, m.id)]
                except discord.HTTPException:
                    pass
        self.last_sent = sent
        self.bot.db.q("UPDATE alliance_votes SET messages=? WHERE id=?", json.dumps(sent), vid)
        await self.maybe_close(vid)
        return vid

    def where_sent(self):
        """Куда ушло последнее голосование — строкой для ответа."""
        if getattr(self, "last_fallback", False):
            return ("⚠️ Каналы «совет-альянса» не найдены ни на одном сервере — голосование выложено прямо здесь, "
                    "разработчику ушёл отчёт. Голосовать можно и через `/альянс голосования`.")
        names = []
        for cid, _ in getattr(self, "last_sent", []):
            ch = self.bot.get_channel(cid)
            if ch:
                names.append(f"{ch.mention} ({ch.guild.name})")
        out = ("✅ Разослано: " + ", ".join(names)) if names else \
            "⚠️ Не нашёл ни одного канала совета альянса — голосование не разослано. Голосуйте через `/альянс голосования`."
        failed = getattr(self, "last_failed", [])
        if failed:
            out += "\n⚠️ Не дошло: " + "; ".join(f"**{f}** — {why}" for f, why in failed) + \
                   "\nГолосовать там можно через `/альянс голосования`. Разработчику ушёл отчёт."
        return out

    def vote_view(self, vid, closed=False):
        v = discord.ui.View(timeout=None)
        for c in CHOICES:
            v.add_item(VoteButton(c, vid, disabled=closed))
        return v

    def vote_embed(self, vid):
        db = self.bot.db
        v = db.one("SELECT * FROM alliance_votes WHERE id=?", vid)
        ballots = db.all("SELECT * FROM alliance_ballots WHERE vote_id=?", vid)
        t = tally(ballots)
        fams = self.active()
        voted = {b["guild_id"]: b["choice"] for b in ballots}
        lines = []
        for f in fams:
            c = voted.get(f["guild_id"])
            lines.append(f"{CHOICES[c][1] if c else '⏳'} {f['family']}")
        status = {"open": f"⏳ Идёт до {discord.utils.format_dt(parse_dt(v['ends_at']), 'f')}",
                  "passed": "✅ **ПРИНЯТО**", "rejected": "❌ **ОТКЛОНЕНО**"}.get(v["status"], v["status"])
        e = self.embed(f"🗳 Совет альянса · №{vid} · {KINDS.get(v['kind'], v['kind'])}",
                       f"**{v['title']}**\n{v['text'][:3000] if v['text'] else ''}\n\n"
                       f"✅ За: **{t['yes']}** · ❌ Против: **{t['no']}** · ➖ Воздержались: **{t['abs']}**\n"
                       f"Нужно для решения: больше половины семей ({len(fams) // 2 + 1} из {len(fams)})\n\n"
                       + "\n".join(lines) + f"\n\n{status}",
                       COLOR if v["status"] == "open" else (0x43A047 if v["status"] == "passed" else 0x757575))
        return e

    async def cast(self, inter, vid, choice):
        db = self.bot.db
        v = db.one("SELECT * FROM alliance_votes WHERE id=?", vid)
        if not v or v["status"] != "open":
            return await inter.response.send_message("Голосование уже завершено.", ephemeral=True)
        if inter.guild and self.is_hub(inter.guild):
            row = self.leader_family(inter.user.id)
            if not row:
                return await inter.response.send_message("⛔ Голос семьи отдаёт её руководство.", ephemeral=True)
        else:
            row = self.fam(inter.guild.id) if inter.guild else None
            if not row or row["status"] != "active":
                return await inter.response.send_message("Голосуют только семьи альянса.", ephemeral=True)
            if not self.is_leader(inter.user):
                return await inter.response.send_message("⛔ Голос семьи отдаёт её руководство.", ephemeral=True)
        db.q("INSERT INTO alliance_ballots(vote_id, guild_id, user_id, choice) VALUES(?,?,?,?) "
             "ON CONFLICT(vote_id, guild_id) DO UPDATE SET user_id=excluded.user_id, choice=excluded.choice",
             vid, row["guild_id"], inter.user.id, choice)
        await inter.response.send_message(f"Голос семьи {row['family']}: **{CHOICES[choice][0]}**. Пока голосование "
                                          "идёт, его можно изменить.", ephemeral=True)
        if not await self.maybe_close(vid):
            await self.refresh(vid)

    async def refresh(self, vid, closed=False):
        v = self.bot.db.one("SELECT * FROM alliance_votes WHERE id=?", vid)
        e = self.vote_embed(vid)
        for cid, mid in json.loads(v["messages"] or "[]"):
            ch = self.bot.get_channel(cid)
            if not ch:
                continue
            try:
                await ch.get_partial_message(mid).edit(embed=e, view=self.vote_view(vid, closed))
            except discord.HTTPException:
                pass

    async def maybe_close(self, vid, deadline=False):
        db = self.bot.db
        v = db.one("SELECT * FROM alliance_votes WHERE id=?", vid)
        if not v or v["status"] != "open":
            return False
        total = len(self.active())
        t = tally(db.all("SELECT choice FROM alliance_ballots WHERE vote_id=?", vid))
        need = total // 2 + 1
        left = total - sum(t.values())
        result = None
        if t["yes"] >= need:
            result = "passed"
        elif t["yes"] + left < need and not deadline:
            result = "rejected"
        elif deadline:
            result = "passed" if t["yes"] > t["no"] else "rejected"
        if not result:
            return False
        db.q("UPDATE alliance_votes SET status=? WHERE id=?", result, vid)
        await self.refresh(vid, closed=True)
        await self.apply(v, result == "passed", t)
        return True

    async def apply(self, v, passed, t):
        kind, title = v["kind"], v["title"]
        verdict = "✅ принято" if passed else "❌ отклонено"
        await self.broadcast("news_id", embed=self.embed(
            f"🏛 Решение совета №{v['id']}: {verdict}",
            f"**{KINDS.get(kind, kind)}:** {title}\nЗа {t['yes']} · Против {t['no']} · Воздержались {t['abs']}",
            0x43A047 if passed else 0x757575))
        target = self.fam(v["target"]) if v["target"] else None
        if kind == "join" and target:
            if target["status"] != "pending":
                return  # заявку отозвали или бота убрали с сервера, пока шло голосование
            if passed:
                await self.activate(target)
            else:
                self.bot.db.q("UPDATE alliance_families SET status='rejected' WHERE guild_id=?", target["guild_id"])
                g = self.bot.get_guild(target["guild_id"])
                ch = g and (g.system_channel or next((c for c in g.text_channels
                                                      if c.permissions_for(g.me).send_messages), None))
                if ch:
                    try:
                        await ch.send(f"❌ Совет альянса отклонил вступление семьи {target['family']}.")
                    except discord.HTTPException:
                        pass
            return
        if not passed:
            return
        if kind == "kick" and target:
            await self.leave(target, "исключена решением совета")
        elif kind == "charter":
            self.bot.db.set("alliance_charter", v["text"])
            await self.broadcast("news_id", embed=self.embed("📜 Новая редакция устава альянса", v["text"][:4000]))
            await self.record("📜 Совет принял новую редакцию устава.")
        else:
            await self.record(f"🏛 {KINDS.get(kind, kind)}: **{title}**")

    @tasks.loop(minutes=5)
    @guarded
    async def vote_loop(self):
        for v in self.bot.db.all("SELECT id FROM alliance_votes WHERE status='open' AND ends_at<=?", now_iso()):
            await self.maybe_close(v["id"], deadline=True)

    @vote_loop.before_loop
    async def _before_votes(self):
        await self.bot.wait_until_ready()

    # ================= вступление и выход =================
    async def activate(self, row):
        self.bot.db.q("UPDATE alliance_families SET status='active', joined_at=? WHERE guild_id=?", now_iso(),
                      row["guild_id"])
        row = self.fam(row["guild_id"])
        await self.ensure_ally_role(row)
        row = await self.ensure_channels(self.fam(row["guild_id"])) or row
        home_row = self.fam(self.bot.home_id)
        if home_row:
            await self.ensure_channels(home_row)  # дать доступ союзникам к нашей категории
        news = self.channel(row, "news_id")
        if news:
            try:
                await news.send(embed=self.embed(f"🤝 Добро пожаловать в {A}!", self.charter()))
            except discord.HTTPException:
                pass
        await self.record(f"🤝 Семья **{row['family']}** вступила в альянс.")
        home = self.home()
        if home:
            for m in home.members:
                await self.sync_member(m)
        if self.hub():
            await self.build_hub(self.hub())
        await self.refresh_boards()

    async def refresh_boards(self):
        """Состав альянса поменялся — обновить доски отношений семей (там раздел «🏰 Альянс»)."""
        diplo = self.bot.get_cog("Diplomacy")
        for g in self.bot.home_guilds if diplo else []:
            try:
                await diplo.update_board(g)
            except Exception:
                log.exception("Доска отношений «%s»", g.name)

    async def leave(self, row, why):
        self.bot.db.q("UPDATE alliance_families SET status='left' WHERE guild_id=?", row["guild_id"])
        hub = self.hub()
        for m in hub.members if hub else []:
            await self.sync_hub_member(m)
        await self.record(f"🚪 Семья **{row['family']}** {why}.")
        home = self.home()
        r = home.get_role(row["ally_role_id"]) if home and row["ally_role_id"] else None
        if r:
            for m in list(r.members):
                try:
                    await m.remove_roles(r, reason=f"{A}: семья вышла из альянса")
                except discord.HTTPException:
                    pass
        await self.refresh_boards()

    def charter(self):
        return self.bot.db.get("alliance_charter") or DEFAULT_CHARTER

    # ================= штаб альянса =================
    async def build_hub(self, g):
        """Собирает/обновляет сервер-штаб: роли семей, совет, каналы, общие голосовые. Строго по одному разу."""
        if not g:
            return None
        async with self.hub_lock:
            return await self._build_hub(g)

    async def _build_hub(self, g):
        db, PO = self.bot.db, discord.PermissionOverwrite

        def sid(k):
            v = db.get(f"hub:{k}")
            return int(v) if v else 0

        async def role(key, name, color, hoist):
            r = g.get_role(sid(key))
            if not r:
                same = [x for x in g.roles if x.name == name]
                r = max(same, key=lambda x: len(x.members)) if same else \
                    await g.create_role(name=name, color=color, hoist=hoist, mentionable=False, reason=A)
                db.set(f"hub:{key}", r.id)
            elif r.name != name:
                await r.edit(name=name)
            return r

        try:
            council = await role("council", "🏛 Совет альянса", 0xE8C872, True)
            fams = [await role(f"fam:{row['guild_id']}", f"🤝 {row['family']}", COLOR, True) for row in self.active()]
            members = fams + [council]
            me = PO(view_channel=True, send_messages=True, manage_channels=True, manage_webhooks=True,
                    embed_links=True, attach_files=True, read_message_history=True, mention_everyone=True,
                    manage_permissions=True, connect=True, speak=True, move_members=True)

            def ow(read, write=(), voice=False, public=False):
                o = {g.default_role: PO(view_channel=public, send_messages=False, connect=False), g.me: me}
                for r in read:
                    w = r in write
                    o[r] = PO(view_channel=True, read_message_history=True, send_messages=w, attach_files=w,
                              embed_links=w, add_reactions=True, connect=voice or None, speak=(voice and w) or None,
                              stream=(voice and w) or None, use_voice_activation=voice or None)
                return o

            layout = [
                ("cat_info", "📢 АЛЬЯНС", [
                    ("welcome", "text", "👋・добро-пожаловать", ow([], public=True)),
                    ("news", "text", "📢・новости", ow(members)),
                    ("charter", "text", "📜・устав", ow(members))]),
                ("cat_talk", "💬 ОБЩЕНИЕ", [
                    ("chat", "text", "💬・общий-чат", ow(members, members)),
                    ("v_main", "voice", "🎙 Общий сбор", ow(members, members, voice=True)),
                    ("v_1", "voice", "🔊 Свободный 1", ow(members, members, voice=True)),
                    ("v_2", "voice", "🔊 Свободный 2", ow(members, members, voice=True))]),
                ("cat_council", "🏛 СОВЕТ АЛЬЯНСА", [
                    ("council", "text", "🏛・совет-альянса", ow([council], [council])),
                    ("bl", "text", "⛔・чс-альянса", ow([council])),
                    ("v_council", "voice", "🎙 Совет альянса", ow([council], [council], voice=True))]),
            ]
            ids = {}
            for ckey, cname, chans in layout:
                cat = g.get_channel(sid(ckey))
                if not cat:
                    cat = await g.create_category(cname, reason=A)
                    db.set(f"hub:{ckey}", cat.id)
                for key, kind, name, overwrites in chans:
                    ch = g.get_channel(sid(key))
                    if ch:
                        await ch.edit(overwrites=overwrites, category=cat)
                    elif kind == "text":
                        ch = await g.create_text_channel(name, category=cat, overwrites=overwrites, reason=A)
                    else:
                        ch = await g.create_voice_channel(name, category=cat, overwrites=overwrites, reason=A)
                    db.set(f"hub:{key}", ch.id)
                    ids[key] = ch
            hooks = [w for w in await ids["chat"].webhooks() if w.user and w.user.id == self.bot.user.id]
            wh = hooks[0] if hooks else await ids["chat"].create_webhook(name=A[:80], reason=A)
            self.hooks[g.id] = wh
            db.q("INSERT OR REPLACE INTO alliance_families(guild_id, family, status, lead_role_id, cat_id, chat_id, "
                 "news_id, council_id, bl_id, webhook, joined_at) VALUES(?, 'Штаб альянса', 'hub', ?,?,?,?,?,?,?,?)",
                 g.id, council.id, sid("cat_info"), ids["chat"].id, ids["news"].id, ids["council"].id, ids["bl"].id,
                 wh.url, now_iso())
            # приветствие и устав — одно сообщение, обновляется
            await self.pinned(g, "welcome", self.embed(
                f"👋 Штаб «{A}»",
                "Здесь собираются все семьи альянса: общий чат, общие голосовые, совет.\n\n"
                "**Доступ выдаётся автоматически:** бот видит, в какой семье альянса ты состоишь, и даёт роль "
                "этой семьи. Руководство семей получает роль «🏛 Совет альянса».\n"
                "Семьи без нашего бота: роль выдаёт их руководство — `/альянс участники добавить`.\n\n"
                "Если роли нет — ты не состоишь ни в одной семье альянса. Сначала вступи в семью.\n\n"
                "Семьи: " + (", ".join(r["family"] for r in self.active()) or "—")))
            await self.pinned(g, "charter", self.embed(f"📜 Устав «{A}»", self.charter()[:4000]))
            if not db.get("hub:invite"):
                inv = await ids["welcome"].create_invite(max_age=0, max_uses=0, reason=A)
                db.set("hub:invite", inv.url)
        except discord.HTTPException as e:
            log.warning("Штаб альянса: %s", e)
            return None
        for m in g.members:
            await self.sync_hub_member(m)
        return self.hub_row()

    async def pinned(self, g, key, embed):
        ch = g.get_channel(int(self.bot.db.get(f"hub:{key}") or 0))
        if not ch:
            return
        mid = self.bot.db.get(f"hub:{key}_msg")
        if mid:
            try:
                return await ch.get_partial_message(int(mid)).edit(embed=embed)
            except discord.HTTPException:
                pass
        m = await ch.send(embed=embed)
        self.bot.db.set(f"hub:{key}_msg", m.id)

    async def sync_hub_member(self, member):
        hub = self.hub()
        if not hub or member.guild.id != hub.id or member.bot:
            return
        db = self.bot.db
        mine = {f["guild_id"] for f in self.member_families(member.id)}
        lead = self.leader_family(member.id)
        want, drop = [], []
        for row in self.bot.db.all("SELECT * FROM alliance_families WHERE status IN ('active','left','rejected')"):
            r = hub.get_role(int(db.get(f"hub:fam:{row['guild_id']}") or 0))
            if r:
                (want if row["guild_id"] in mine and row["status"] == "active" else drop).append(r)
        council = hub.get_role(int(db.get("hub:council") or 0))
        if council:
            (want if lead else drop).append(council)
        add = [r for r in want if r not in member.roles]
        rem = [r for r in drop if r in member.roles]
        try:
            if add:
                await member.add_roles(*add, reason=f"{A}: состав семей")
            if rem:
                await member.remove_roles(*rem, reason=f"{A}: состав семей")
        except discord.HTTPException:
            pass
        # ник в штабе = ник на сервере своей семьи
        fams = self.member_families(member.id)
        if fams and member.id != hub.owner_id:
            fg = self.bot.get_guild(fams[0]["guild_id"])
            src = fg.get_member(member.id) if fg else None
            name = src.display_name[:32] if src else None
            if name and member.display_name != name:
                try:
                    await member.edit(nick=name, reason=f"{A}: ник как на сервере семьи {fams[0]['family']}")
                except discord.HTTPException:
                    pass

    @group.command(name="штаб", description="Сделать этот сервер штабом альянса (или обновить штаб)")
    async def hub_cmd(self, inter: discord.Interaction):
        g = inter.guild
        if self.bot.is_home(g) or (self.fam(g.id) and self.fam(g.id)["status"] in ("active", "pending")):
            return await inter.response.send_message("Штаб — это отдельный сервер, не сервер семьи. Создай новый "
                                                     "Discord, добавь туда бота через `/альянс пригласить` и запусти "
                                                     "команду там.", ephemeral=True)
        if not self.exists():
            return await inter.response.send_message("Альянс ещё не создан.", ephemeral=True)
        if not self.leader_family(inter.user.id):
            return await inter.response.send_message("⛔ Только руководство семей альянса.", ephemeral=True)
        cur = self.hub_row()
        if cur and cur["guild_id"] != g.id and self.bot.get_guild(cur["guild_id"]):
            return await inter.response.send_message("У альянса уже есть штаб на другом сервере.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        first = not cur
        row = await self.build_hub(g)
        if not row:
            return await inter.followup.send("❌ Не получилось собрать штаб — проверь права бота (нужен администратор "
                                             "или управление ролями и каналами).", ephemeral=True)
        inv = self.bot.db.get("hub:invite")
        if first:
            await self.broadcast("news_id", embed=self.embed("🏛 У альянса появился штаб",
                                                             f"Общий сервер всех семей альянса: {inv}\n"
                                                             "Заходи — роль твоей семьи выдастся автоматически."))
            await self.record(f"🏛 Открыт штаб альянса: {inv}")
        await inter.followup.send(f"✅ Штаб готов. Приглашение для всех семей: {inv}", ephemeral=True)

    # ================= команды =================
    async def need_leader(self, inter):
        if self.is_hub(inter.guild):
            row = self.leader_family(inter.user.id)
            if not row:
                await inter.response.send_message("⛔ Только для руководства семей альянса.", ephemeral=True)
            return row
        row = self.fam(inter.guild.id)
        if not row or row["status"] != "active":
            await inter.response.send_message("Ваша семья не состоит в альянсе.", ephemeral=True)
            return None
        if not self.is_leader(inter.user):
            await inter.response.send_message("⛔ Только для руководства семьи.", ephemeral=True)
            return None
        return row

    @group.command(name="создать", description="Основать альянс (один раз, на сервере семьи-основателя)")
    async def create(self, inter: discord.Interaction):
        if not self.bot.is_home(inter.guild) or not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только руководство семьи-основателя.", ephemeral=True)
        if self.exists():
            return await inter.response.send_message("Альянс уже создан. `/альянс состав` — кто в нём.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        self.bot.db.q("INSERT OR REPLACE INTO alliance_families(guild_id, family, status, joined_at, added_by) "
                      "VALUES(?,?, 'active', ?, ?)", inter.guild.id, self.bot.family(inter.guild)["name"], now_iso(),
                      inter.user.id)
        row = await self.ensure_channels(self.fam(inter.guild.id))
        if not row:
            return await inter.followup.send("❌ Не получилось создать каналы — проверь права бота.", ephemeral=True)
        news = self.channel(row, "news_id")
        if news:
            await news.send(embed=self.embed(f"🤝 {A}", self.charter()))
        await self.record(f"🏛 Основан **{A}**. Семья-основатель — {self.bot.family(inter.guild)['name']}.")
        await inter.followup.send(f"✅ {A} создан: категория с каналами готова. Дальше — `/альянс пригласить`.",
                                  ephemeral=True)

    @group.command(name="пригласить", description="Ссылка, чтобы добавить бота альянса на сервер другой семьи")
    async def invite(self, inter: discord.Interaction):
        if not await self.need_leader(inter):
            return
        url = discord.utils.oauth_url(self.bot.application_id, permissions=BOT_PERMS,
                                      scopes=("bot", "applications.commands"))
        await inter.response.send_message(embed=self.embed(
            "✉️ Как подключить семью к альянсу",
            f"1. Администратор сервера семьи открывает ссылку и добавляет бота:\n{url}\n"
            "2. На своём сервере пишет `/альянс подключить`: название семьи, роль членов семьи, роль руководства.\n"
            "3. Совет альянса голосует — после решения у них появятся каналы альянса.\n\n"
            "Бот на их сервере работает **только** для альянса: видит лишь свою категорию и роль семьи."),
            ephemeral=True)

    @group.command(name="подключить", description="Подать заявку вашей семьи на вступление в альянс")
    @app_commands.rename(family="семья", fam_role="роль_семьи", lead_role="роль_руководства")
    @app_commands.describe(family="Название семьи", fam_role="Роль, которая есть у всех членов семьи",
                           lead_role="Роль руководства (Глава, замы) — они голосуют в совете")
    async def connect(self, inter: discord.Interaction, family: str, fam_role: discord.Role,
                      lead_role: discord.Role):
        g = inter.guild
        if self.bot.is_home(g):
            return await inter.response.send_message("Это сервер семьи-основателя — он уже в альянсе.", ephemeral=True)
        if inter.user.id != g.owner_id and not inter.user.guild_permissions.administrator:
            return await inter.response.send_message("⛔ Подключает только администратор сервера.", ephemeral=True)
        if not self.exists():
            return await inter.response.send_message("Альянс ещё не создан.", ephemeral=True)
        row = self.fam(g.id)
        if row and row["status"] == "hub":
            return await inter.response.send_message("Это штаб альянса, а не сервер семьи.", ephemeral=True)
        if row and row["status"] == "active":
            return await inter.response.send_message("Ваша семья уже в альянсе.", ephemeral=True)
        if row and row["status"] == "pending":
            return await inter.response.send_message("Заявка уже на голосовании в совете.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        family = family.strip()[:40]
        if row:  # заявка после выхода/отказа: каналы и роль союзника уже есть — не создаём дубли
            self.bot.db.q("UPDATE alliance_families SET family=?, status='pending', family_role_id=?, lead_role_id=?, "
                          "added_by=? WHERE guild_id=?", family, fam_role.id, lead_role.id, inter.user.id, g.id)
        else:
            self.bot.db.q("INSERT INTO alliance_families(guild_id, family, status, family_role_id, lead_role_id,"
                          " added_by) VALUES(?,?, 'pending', ?, ?, ?)", g.id, family, fam_role.id, lead_role.id,
                          inter.user.id)
        n = len([m for m in fam_role.members if not m.bot])
        vid = await self.start_vote(inter.user, g, "join", f"Семья {family}",
                                    f"Сервер: {g.name}\nЧленов семьи: {n}\nЗаявку подал: {inter.user} ", g.id, 48,
                                    fallback=inter.channel)
        await inter.followup.send(f"📨 Заявка семьи **{family}** отправлена. Совет альянса голосует (№{vid}, до 48 ч).\n"
                                  f"{self.where_sent()}", ephemeral=True)

    @group.command(name="добавить-семью", description="Принять в альянс семью БЕЗ нашего бота (она будет в штабе)")
    @app_commands.rename(family="семья", leader="глава", deputy="заместитель", now_="сразу")
    @app_commands.describe(family="Название семьи", leader="Глава семьи — должен быть в штабе альянса",
                           deputy="Заместитель (необязательно) — тоже голосует в совете",
                           now_="Принять сразу, без голосования (только Глава/зам семьи-основателя)")
    async def add_ext(self, inter: discord.Interaction, family: str, leader: discord.Member,
                      deputy: discord.Member = None, now_: bool = False):
        if not await self.need_leader(inter):
            return
        hub = self.hub()
        if not hub:
            return await inter.response.send_message(
                "Семьи без бота живут в штабе альянса, а штаба ещё нет. Создай отдельный сервер, добавь туда бота "
                "(`/альянс пригласить`) и запусти там `/альянс штаб`.", ephemeral=True)
        ids = [m.id for m in (leader, deputy) if m and not m.bot]
        if not ids:
            return await inter.response.send_message("Глава должен быть человеком, а не ботом.", ephemeral=True)
        missing = [m for m in (leader, deputy) if m and not hub.get_member(m.id)]
        if missing:
            return await inter.response.send_message(
                f"❌ {', '.join(m.display_name for m in missing)} ещё не в штабе — пусть зайдёт: "
                f"{self.bot.db.get('hub:invite') or '/альянс состав'}", ephemeral=True)
        family = " ".join(family.split())[:40]
        db = self.bot.db
        if db.one("SELECT 1 FROM alliance_families WHERE lower(family)=lower(?) AND status IN ('active','pending')",
                  family):
            return await inter.response.send_message("Семья с таким названием уже в альянсе или на голосовании.",
                                                     ephemeral=True)
        for uid in ids:
            other = self.leader_family(uid)
            if other:
                return await inter.response.send_message(f"<@{uid}> уже в руководстве семьи {other['family']}.",
                                                         ephemeral=True)
        if now_ and not self.is_founder(inter.user):
            return await inter.response.send_message("⛔ Принять без голосования может только Глава или заместитель "
                                                     "семьи-основателя альянса.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        gid = self.new_ext_id()
        db.q("INSERT INTO alliance_families(guild_id, family, status, external, leaders, added_by) "
             "VALUES(?,?, 'pending', 1, ?, ?)", gid, family, json.dumps(ids), inter.user.id)
        if now_:
            await self.force_accept(self.fam(gid), inter.user)
            return await inter.followup.send(f"✅ Семья **{family}** принята в альянс без голосования. Её руководство "
                                             "уже может выдавать роль семьи в штабе: `/альянс участники добавить`.",
                                             ephemeral=True)
        vid = await self.start_vote(inter.user, inter.guild, "join", f"Семья {family} (без бота, в штабе)",
                                    "Руководство: " + ", ".join(f"<@{u}>" for u in ids) + f"\nПредложил: {inter.user}\n"
                                    "Своего сервера с ботом у семьи нет — участвует через штаб альянса.", gid, 48,
                                    fallback=inter.channel)
        await inter.followup.send(f"📨 Семья **{family}** вынесена на голосование совета (№{vid}, до 48 ч). После "
                                  "принятия её руководство получит роль совета в штабе и сможет выдавать роль семьи "
                                  f"своим: `/альянс участники добавить`.\n{self.where_sent()}", ephemeral=True)

    async def ext_gate(self, inter):
        """Руководство семьи без бота, в штабе. Возвращает её строку или None (ответ уже отправлен)."""
        if not self.is_hub(inter.guild):
            await inter.response.send_message("Это делается в штабе альянса.", ephemeral=True)
            return None
        row = self.leader_family(inter.user.id)
        if not row or not self.is_ext(row):
            await inter.response.send_message("⛔ Только руководство семьи, которая в альянсе без своего бота. "
                                              "Семьи с ботом получают роли автоматически.", ephemeral=True)
            return None
        return row

    @members_grp.command(name="добавить", description="Дать человеку роль вашей семьи в штабе")
    @app_commands.rename(member="кого")
    async def ext_add(self, inter: discord.Interaction, member: discord.Member):
        row = await self.ext_gate(inter)
        if not row:
            return
        if member.bot:
            return await inter.response.send_message("Ботов в семью не берём.", ephemeral=True)
        if self.bot.alliance_bl_find(discord_id=member.id):
            return await inter.response.send_message("⛔ Этот человек в ЧС альянса.", ephemeral=True)
        others = [f["family"] for f in self.member_families(member.id) if f["guild_id"] != row["guild_id"]]
        if others:
            return await inter.response.send_message(f"Он уже в семье {', '.join(others)}.", ephemeral=True)
        r = self.hub_fam_role(row)
        if not r:
            return await inter.response.send_message("Роль семьи в штабе не найдена — руководство альянса: "
                                                     "`/альянс штаб` (обновить штаб).", ephemeral=True)
        try:
            await member.add_roles(r, reason=f"{A}: принят в семью {row['family']} ({inter.user})")
        except discord.HTTPException:
            return await inter.response.send_message("❌ Не хватает прав выдать роль.", ephemeral=True)
        await inter.response.send_message(f"✅ {member.mention} — в семье **{row['family']}**.", ephemeral=True)
        await self.record(f"👤 {member.display_name} принят в семью **{row['family']}** (штаб).")
        await self.resync(member.id)

    @members_grp.command(name="убрать", description="Снять с человека роль вашей семьи в штабе")
    @app_commands.rename(member="кого")
    async def ext_remove(self, inter: discord.Interaction, member: discord.Member):
        row = await self.ext_gate(inter)
        if not row:
            return
        if member.id in self.ext_leaders(row):
            return await inter.response.send_message("Это руководство семьи — сначала `/альянс участники руководство`.",
                                                     ephemeral=True)
        r = self.hub_fam_role(row)
        if r and r in member.roles:
            try:
                await member.remove_roles(r, reason=f"{A}: вышел из семьи {row['family']} ({inter.user})")
            except discord.HTTPException:
                return await inter.response.send_message("❌ Не хватает прав снять роль.", ephemeral=True)
        await inter.response.send_message(f"✅ {member.mention} больше не в семье **{row['family']}**.", ephemeral=True)
        await self.resync(member.id)

    @members_grp.command(name="руководство", description="Добавить или убрать члена руководства вашей семьи (голос в совете)")
    @app_commands.rename(member="кого", action="действие")
    @app_commands.choices(action=[app_commands.Choice(name="добавить в руководство", value="add"),
                                  app_commands.Choice(name="убрать из руководства", value="del")])
    async def ext_lead(self, inter: discord.Interaction, member: discord.Member, action: app_commands.Choice[str]):
        row = await self.ext_gate(inter)
        if not row:
            return
        leaders = self.ext_leaders(row)
        if action.value == "add":
            if member.bot:
                return await inter.response.send_message("Ботов не берём.", ephemeral=True)
            other = self.leader_family(member.id)
            if other and other["guild_id"] != row["guild_id"]:
                return await inter.response.send_message(f"Он уже в руководстве семьи {other['family']}.",
                                                         ephemeral=True)
            leaders.add(member.id)
        else:
            if member.id not in leaders:
                return await inter.response.send_message("Его нет в руководстве.", ephemeral=True)
            if len(leaders) == 1:
                return await inter.response.send_message("Нельзя убрать последнего — сначала добавь другого.",
                                                         ephemeral=True)
            leaders.discard(member.id)
        self.bot.db.q("UPDATE alliance_families SET leaders=? WHERE guild_id=?", json.dumps(sorted(leaders)),
                      row["guild_id"])
        await inter.response.send_message(f"✅ Руководство семьи **{row['family']}**: "
                                          + ", ".join(f"<@{u}>" for u in sorted(leaders)), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())
        await self.resync(member.id)

    async def pending_auto(self, inter: discord.Interaction, current: str):
        rows = self.bot.db.all("SELECT guild_id, family, external FROM alliance_families WHERE status='pending'")
        return [app_commands.Choice(name=(r["family"] + (" (без бота)" if r["external"] else ""))[:100],
                                    value=str(r["guild_id"]))
                for r in rows if (current or "").lower() in r["family"].lower()][:25]

    @group.command(name="принять-сразу", description="Принять семью из заявки без голосования (Глава/зам основателя)")
    @app_commands.rename(family="семья")
    @app_commands.autocomplete(family=pending_auto)
    async def force_cmd(self, inter: discord.Interaction, family: str):
        if not self.is_founder(inter.user):
            return await inter.response.send_message("⛔ Только Глава или заместитель семьи-основателя альянса.",
                                                     ephemeral=True)
        row = self.fam(int(family)) if family.lstrip("-").isdigit() else self.bot.db.one(
            "SELECT * FROM alliance_families WHERE status='pending' AND lower(family)=lower(?)", family.strip())
        if not row or row["status"] != "pending":
            return await inter.response.send_message("Нет такой заявки. Сначала: `/альянс подключить` (семья с "
                                                     "ботом) или `/альянс добавить-семью` (без бота).", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        await self.force_accept(row, inter.user)
        await inter.followup.send(f"✅ Семья **{row['family']}** принята в альянс без голосования.", ephemeral=True)

    @group.command(name="голосования", description="Открытые голосования совета — с кнопками, голосовать отсюда")
    async def votes_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT id FROM alliance_votes WHERE status='open' ORDER BY id")
        if not rows:
            return await inter.response.send_message("🗳 Открытых голосований нет.", ephemeral=True)
        await inter.response.send_message(embed=self.vote_embed(rows[0]["id"]), view=self.vote_view(rows[0]["id"]),
                                          ephemeral=True)
        for r in rows[1:5]:
            await inter.followup.send(embed=self.vote_embed(r["id"]), view=self.vote_view(r["id"]), ephemeral=True)

    @group.command(name="состав", description="Семьи альянса")
    async def roster(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM alliance_families WHERE status IN ('active','pending') "
                               "ORDER BY status, joined_at")
        if not rows:
            return await inter.response.send_message("Альянс ещё не создан.", ephemeral=True)
        total, lines = 0, []
        for r in rows:
            n = self.members_count(r)
            if r["status"] == "active":
                total += n
                since = f" · с {parse_dt(r['joined_at']).strftime('%d.%m.%Y')}" if r["joined_at"] else ""
                lines.append(f"🤝 **{r['family']}** — {n} чел.{since}" + (" · 🏛 только в штабе" if self.is_ext(r) else ""))
            else:
                lines.append(f"⏳ {r['family']} — заявка на голосовании")
        await inter.response.send_message(embed=self.embed(
            f"🤝 {A}", "\n".join(lines) + f"\n\n**Всего в альянсе:** {total} чел. · решения — большинством семей"
            + (f"\n**Штаб:** {self.bot.db.get('hub:invite')}" if self.hub_row() else "")),
            ephemeral=True)

    @group.command(name="голосование", description="Вынести вопрос на совет альянса")
    @app_commands.rename(kind="вопрос", text="суть", family="семья", hours="часов")
    @app_commands.describe(kind="Тип решения", text="Коротко: что решаем (для устава откроется окно с текстом)",
                           family="Для «исключить семью»: название семьи", hours="Сколько часов голосовать (1–168)")
    @app_commands.choices(kind=[app_commands.Choice(name=v, value=k) for k, v in KINDS.items() if k != "join"])
    async def vote_cmd(self, inter: discord.Interaction, kind: app_commands.Choice[str], text: str = "",
                       family: str = None, hours: int = 24):
        if not await self.need_leader(inter):
            return
        k = kind.value
        if k == "charter":
            return await inter.response.send_modal(CharterModal(hours, self.charter()))
        target = None
        if k == "kick":
            row = self.bot.db.one("SELECT * FROM alliance_families WHERE status='active' AND lower(family)=lower(?)",
                                  (family or "").strip())
            if not row:
                return await inter.response.send_message("Укажи семью альянса в поле «семья».", ephemeral=True)
            if not self.is_ext(row) and self.bot.is_home(self.bot.get_guild(row["guild_id"])):
                return await inter.response.send_message("Семью-основателя исключить нельзя.", ephemeral=True)
            target, text = row["guild_id"], text or f"Исключить семью {row['family']}"
        if not text.strip():
            return await inter.response.send_message("Опиши суть решения в поле «суть».", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        title = text.strip().split("\n")[0][:120]
        vid = await self.start_vote(inter.user, inter.guild, k, title, text, target, hours, fallback=inter.channel)
        await inter.followup.send(f"🗳 Голосование №{vid} запущено.\n{self.where_sent()}", ephemeral=True)

    @group.command(name="тревога", description="Позвать на помощь все семьи альянса")
    @app_commands.rename(text="что_случилось")
    async def alarm(self, inter: discord.Interaction, text: str):
        row = await self.need_leader(inter)
        if not row:
            return
        last = self.alarm_at.get(row["guild_id"], 0)
        if now().timestamp() - last < ALARM_COOLDOWN:
            return await inter.response.send_message("⏳ Тревогу альянса можно поднимать раз в 10 минут.",
                                                     ephemeral=True)
        self.alarm_at[row["guild_id"]] = now().timestamp()
        await inter.response.defer(ephemeral=True)
        await self.broadcast("news_id", embed=self.embed(f"🚨 ТРЕВОГА АЛЬЯНСА — семья {row['family']}",
                                                         f"{text[:1500]}\n\nПодняли: {inter.user.mention}",
                                                         0xE53935), ping=True)
        music = self.bot.get_cog("Music")
        home = self.home()
        if music and home:
            self.bot.spawn(music.alarm(home, f"Тревога альянса! Семья {row['family']} просит помощи. {text[:200]}"))
        await self.record(f"🚨 Тревога от семьи **{row['family']}**: {text[:300]}")
        await inter.followup.send("🚨 Тревога разослана всем семьям альянса.", ephemeral=True)

    @group.command(name="устав", description="Устав альянса")
    async def charter_cmd(self, inter: discord.Interaction):
        await inter.response.send_message(embed=self.embed(f"📜 {A}", self.charter()[:4000]), ephemeral=True)

    @group.command(name="летопись", description="Последние события альянса")
    async def chronicle_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM alliance_log ORDER BY id DESC LIMIT 15")
        text = "\n".join(f"`{parse_dt(r['created_at']).strftime('%d.%m.%y')}` {r['text']}" for r in rows)
        await inter.response.send_message(embed=self.embed("📜 Летопись альянса", text or "Пока пусто."),
                                          ephemeral=True)

    @group.command(name="выйти", description="Вывести свою семью из альянса")
    async def leave_cmd(self, inter: discord.Interaction):
        row = await self.need_leader(inter)
        if not row:
            return
        if self.bot.is_home(inter.guild):
            return await inter.response.send_message("Семья-основатель не может выйти — альянс работает на её боте.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await self.leave(row, "вышла из альянса")
        await inter.followup.send("Семья вышла из альянса. Каналы альянса можно удалить вручную.", ephemeral=True)

    # ---------- общий ЧС ----------
    @blg.command(name="добавить", description="Внести в общий чёрный список альянса")
    @app_commands.rename(nick="ник", static="static_id", discord_id="discord_id", reason="причина")
    @app_commands.describe(nick="Игровой ник", static="Static ID", discord_id="Discord ID (цифры)",
                           reason="За что")
    async def bl_add(self, inter: discord.Interaction, reason: str, nick: str = None, static: str = None,
                     discord_id: str = None):
        row = await self.need_leader(inter)
        if not row:
            return
        did = int(discord_id) if discord_id and discord_id.strip().isdigit() else None
        if not (nick or static or did):
            return await inter.response.send_message("Нужен хотя бы ник, Static ID или Discord ID.", ephemeral=True)
        cur = self.bot.db.q("INSERT INTO alliance_bl(discord_id, static_id, nick, reason, family_gid, added_by, "
                            "created_at) VALUES(?,?,?,?,?,?,?)", did, static, nick, reason[:500], row["guild_id"],
                            inter.user.id, now_iso())
        who = " · ".join(x for x in (nick and f"**{nick}**", static and f"Static `{static}`",
                                      did and f"<@{did}> (`{did}`)") if x)
        await inter.response.send_message(f"⛔ Запись А-{cur.lastrowid} добавлена в ЧС альянса.", ephemeral=True)
        await self.broadcast("bl_id", embed=self.embed(f"⛔ ЧС альянса · А-{cur.lastrowid}",
                                                       f"{who}\n**Причина:** {reason}\n**Внесла семья:** {row['family']}",
                                                       0xE53935))

    @blg.command(name="убрать", description="Убрать запись из ЧС альянса (может семья, которая внесла)")
    @app_commands.rename(num="номер")
    async def bl_remove(self, inter: discord.Interaction, num: str):
        row = await self.need_leader(inter)
        if not row:
            return
        rid = int("".join(ch for ch in num if ch.isdigit()) or 0)
        r = self.bot.db.one("SELECT * FROM alliance_bl WHERE id=? AND active=1", rid)
        if not r:
            return await inter.response.send_message("Нет такой записи.", ephemeral=True)
        hub = self.hub_row()
        if r["family_gid"] not in (row["guild_id"], hub["guild_id"] if hub else 0):  # записи из штаба старых версий
            return await inter.response.send_message("Убрать запись может только семья, которая её внесла, "
                                                     "или совет — через голосование.", ephemeral=True)
        self.bot.db.q("UPDATE alliance_bl SET active=0 WHERE id=?", rid)
        await inter.response.send_message(f"✅ Запись А-{rid} убрана.", ephemeral=True)
        await self.broadcast("bl_id", embed=self.embed(f"✅ ЧС альянса · А-{rid} снята",
                                                       f"Сняла семья {row['family']}", 0x43A047))

    @blg.command(name="проверить", description="Проверить человека по общему ЧС альянса")
    @app_commands.rename(query="кого")
    @app_commands.describe(query="Ник, Static ID или Discord ID")
    async def bl_check(self, inter: discord.Interaction, query: str):
        if not self.fam(inter.guild.id) or not self.is_leader(inter.user) and not self.bot.has(inter.user, config.HR):
            return await inter.response.send_message("⛔ Только для руководства и отдела кадров.", ephemeral=True)
        q = query.strip()
        hit = self.bot.alliance_bl_find(discord_id=int(q) if q.isdigit() and len(q) > 15 else None,
                                        static=q, nick=q)
        await inter.response.send_message(
            f"⛔ В ЧС альянса: запись {hit['id']} — {hit['reason']}" if hit else "✅ В ЧС альянса не найден.",
            ephemeral=True)

    @blg.command(name="список", description="Весь общий чёрный список альянса")
    async def bl_list(self, inter: discord.Interaction):
        if not self.fam(inter.guild.id) or not self.is_leader(inter.user):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        rows = self.bot.db.all("SELECT b.*, f.family FROM alliance_bl b LEFT JOIN alliance_families f "
                               "ON f.guild_id=b.family_gid WHERE b.active=1 ORDER BY b.id DESC LIMIT 30")
        lines = [f"`А-{r['id']}` {r['nick'] or r['static_id'] or r['discord_id']} — {r['reason'][:60]} "
                 f"({r['family']})" for r in rows]
        await inter.response.send_message(embed=self.embed("⛔ ЧС альянса", "\n".join(lines) or "Пусто."),
                                          ephemeral=True)


async def setup(bot):
    bot.add_dynamic_items(VoteButton)
    await bot.add_cog(Alliance(bot))
