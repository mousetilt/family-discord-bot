# -*- coding: utf-8 -*-
"""Защита сервера: анти-спам, анти-рейд, анти-нюк (массовое удаление/баны), чужие боты."""
import asyncio
import datetime as dt
import logging
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import now, safe_dm

log = logging.getLogger("family-bot.security")

SPAM_MSGS, SPAM_WINDOW = 6, 5          # 6 сообщений за 5 секунд
DUP_COUNT = 4                          # 4 одинаковых сообщения подряд
MASS_MENTIONS = 6                      # упоминаний в одном сообщении
RAID_JOINS, RAID_WINDOW = 8, 30        # 8 входов за 30 секунд → режим рейда
RAID_MIN_AGE_DAYS = 7                  # в рейд выкидываем аккаунты младше 7 дней
NUKE_LIMIT, NUKE_WINDOW = 4, 60        # 4 удаления каналов/ролей или бана за минуту


class Security(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # всё — по серверу: бот обслуживает несколько семей, рейд/флуд на одном сервере не касается других
        self.msgs = defaultdict(deque)       # (guild_id, user_id) -> времена сообщений
        self.last_text = {}                  # (guild_id, user_id) -> (текст, сколько подряд)
        self.joins = defaultdict(deque)      # guild_id -> времена входов
        self.raid_until = {}                 # guild_id -> monotonic, до какого момента режим рейда
        self.actions = defaultdict(deque)    # (guild_id, user_id) -> времена «разрушительных» действий
        self._raid_tasks = {}                # guild_id -> задача «выключить рейд по таймеру»

    # ---------- режим рейда ----------
    def raid_active(self, guild):
        return time.monotonic() < self.raid_until.get(guild.id, 0)

    async def set_raid(self, guild, on, seconds=3600, reason="Режим рейда"):
        """Включить (на seconds) или выключить режим рейда на ЭТОМ сервере. Проверка входа поднимается до высокой,
        а по окончании сама возвращается к прежней (раньше оставалась высокой навсегда)."""
        old = self._raid_tasks.pop(guild.id, None)
        if old and old is not asyncio.current_task():
            old.cancel()
        if on:
            self.raid_until[guild.id] = time.monotonic() + seconds
            if guild.verification_level < discord.VerificationLevel.high:
                self.bot.db.gset(guild, "raid:prev_level", guild.verification_level.value)
            level = discord.VerificationLevel.high
            self.bot.db.gset(guild, "raid:until", int(time.time() + seconds))  # переживёт перезапуск бота
            self._raid_tasks[guild.id] = asyncio.create_task(self._raid_timer(guild, seconds))
        else:
            self.raid_until.pop(guild.id, None)
            self.bot.db.gdel(guild, "raid:until")
            try:
                level = discord.VerificationLevel(int(self.bot.db.gget(guild, "raid:prev_level") or 2))
            except ValueError:
                level = discord.VerificationLevel.medium
        try:
            if guild.verification_level != level:
                await guild.edit(verification_level=level, reason=reason)
        except discord.HTTPException:
            pass

    async def _raid_timer(self, guild, seconds):
        await asyncio.sleep(seconds)
        while (left := self.raid_until.get(guild.id, 0) - time.monotonic()) > 0:
            await asyncio.sleep(left + 0.05)  # таймер может проснуться чуть раньше срока — доспим остаток
        if not self.raid_active(guild):
            await self.set_raid(guild, False, reason="Режим рейда закончился")
            await self.bot.log(guild, "🟢 Режим рейда закончился — проверка входа возвращена к прежней.",
                               color=config.GREEN)

    @commands.Cog.listener()
    async def on_ready(self):
        """Бот перезапустился посреди рейда — довести таймер до конца (или сразу вернуть проверку входа)."""
        for g in self.bot.home_guilds:
            try:
                until = int(self.bot.db.gget(g, "raid:until") or 0)
            except ValueError:
                until = 0
            if not until or g.id in self._raid_tasks:
                continue
            left = until - time.time()
            if left > 0:
                await self.set_raid(g, True, left, reason="Режим рейда (после перезапуска)")
            else:
                await self.set_raid(g, False, reason="Режим рейда закончился")

    # ---------- общее ----------
    def trusted(self, member):
        if not isinstance(member, discord.Member):
            return False
        return member.id == member.guild.owner_id or member.bot and member.id == self.bot.user.id or \
            self.bot.has(member, config.MOD_EXEMPT)

    async def alarm(self, guild, title, text):
        council = self.bot.ch(guild, "council_chat") or self.bot.ch(guild, "logs")
        lead = self.bot.role(guild, "leader")
        dep = self.bot.role(guild, "deputy")
        e = self.bot.embed(f"🚨 {title}", text, 0xE53935, guild)
        e.timestamp = now()
        if council:
            try:
                await council.send(content=" ".join(r.mention for r in (lead, dep) if r), embed=e,
                                   allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException:
                pass
        await self.bot.log(guild, f"🚨 **{title}** — {text}", color=0xE53935)

    async def punish(self, member, minutes, reason):
        try:
            await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
        except discord.HTTPException:
            pass

    # ---------- анти-спам ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        m = message.author
        if message.guild is None or m.bot or not isinstance(m, discord.Member) or self.trusted(m) \
                or not self.bot.feature(message.guild, "moderation:antispam"):
            return
        t = time.monotonic()
        key = (message.guild.id, m.id)
        q = self.msgs[key]
        q.append(t)
        while q and t - q[0] > SPAM_WINDOW:
            q.popleft()
        text = (message.content or "").strip().lower()
        prev, count = self.last_text.get(key, ("", 0))
        count = count + 1 if text and text == prev else 1
        self.last_text[key] = (text, count)
        mentions = len(message.mentions) + len(message.role_mentions)
        reason = None
        if mentions >= MASS_MENTIONS:
            reason = f"массовые упоминания ({mentions})"
        elif len(q) >= SPAM_MSGS:
            reason = f"флуд ({len(q)} сообщений за {SPAM_WINDOW} сек)"
        elif count >= DUP_COUNT:
            reason = "одинаковые сообщения подряд"
        if not reason:
            return
        q.clear()
        self.last_text[key] = ("", 0)
        try:
            await message.channel.purge(limit=15, check=lambda x: x.author.id == m.id,
                                        after=discord.utils.utcnow() - dt.timedelta(seconds=30))
        except discord.HTTPException:
            pass
        await self.punish(m, 10, f"Анти-спам: {reason}")
        await self.bot.log(m.guild, f"🛡 {m.mention} — мут 10 мин: {reason}", color=0xFB8C00)
        await safe_dm(m, f"🔇 Ты получил мут на 10 минут на сервере семьи {config.FAMILY}: {reason}.")

    # ---------- анти-рейд и чужие боты ----------
    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        if member.bot:
            adder = None
            try:
                async for entry in g.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                    if entry.target and entry.target.id == member.id:
                        adder = entry.user
                        break
            except discord.HTTPException:
                pass
            if adder is None or adder.id != g.owner_id:
                try:
                    await member.kick(reason="Ботов может добавлять только владелец сервера")
                except discord.HTTPException:
                    pass
                await self.alarm(g, "Попытка добавить бота",
                                 f"{member.mention} добавил {adder.mention if adder else 'неизвестный'} — бот выгнан. "
                                 "Ботов может добавлять только владелец сервера.")
            return
        t = time.monotonic()
        joins = self.joins[g.id]
        joins.append(t)
        while joins and t - joins[0] > RAID_WINDOW:
            joins.popleft()
        if len(joins) >= RAID_JOINS and not self.raid_active(g):
            await self.set_raid(g, True, 600, reason="Анти-рейд")
            await self.alarm(g, "РЕЙД!", f"За {RAID_WINDOW} сек зашло {len(joins)} человек. Режим рейда на 10 минут: "
                                         f"новичков мутим, аккаунты младше {RAID_MIN_AGE_DAYS} дн. выкидываем, проверка "
                                         "входа — высокая. Выключить: `/защита рейд выключить`.")
            music = self.bot.get_cog("Music")
            if music:
                asyncio.create_task(music.alarm(g, "Внимание! Тревога! На сервер семьи идёт рейд. "
                                                   "Руководству срочно проверить Discord."))
        if self.raid_active(g):
            age = (now() - member.created_at).days
            if age < RAID_MIN_AGE_DAYS:
                try:
                    await member.kick(reason="Анти-рейд: свежий аккаунт")
                except discord.HTTPException:
                    pass
            else:
                await self.punish(member, 30, "Анти-рейд: режим рейда")

    # ---------- анти-нюк ----------
    async def track(self, guild, action, kind):
        """kind — что сделали (для сообщения). Находим виновника через журнал аудита."""
        try:
            entry = None
            async for e in guild.audit_logs(limit=1, action=action):
                entry = e
            if not entry or (now() - entry.created_at).total_seconds() > 15:
                return
            user = entry.user
        except discord.HTTPException:
            return
        member = guild.get_member(user.id) if user else None
        if not member or member.id in (guild.owner_id, self.bot.user.id):
            return
        t = time.monotonic()
        q = self.actions[(guild.id, member.id)]
        q.append(t)
        while q and t - q[0] > NUKE_WINDOW:
            q.popleft()
        if len(q) < NUKE_LIMIT:
            return
        q.clear()
        removable = [r for r in member.roles if not r.is_default() and r < guild.me.top_role and not r.managed]
        try:
            if removable:
                await member.remove_roles(*removable, reason="Анти-нюк")
            await self.punish(member, 60 * 24, "Анти-нюк")
        except discord.HTTPException:
            pass
        await self.alarm(guild, "Попытка снести сервер",
                         f"{member.mention} за минуту: {kind} ×{NUKE_LIMIT}. С него сняты все роли и выдан мут на сутки. "
                         "Проверьте журнал аудита и верните роли, если это ошибка.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self.track(channel.guild, discord.AuditLogAction.channel_delete, "удаление каналов")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self.track(role.guild, discord.AuditLogAction.role_delete, "удаление ролей")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self.track(guild, discord.AuditLogAction.ban, "баны")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        await self.track(channel.guild, discord.AuditLogAction.webhook_create, "создание вебхуков")

    # ---------- команды ----------
    group = app_commands.Group(name="защита", description="Защита сервера от рейдов и спама",
                               default_permissions=discord.Permissions(administrator=True))

    @group.command(name="статус", description="Состояние защиты")
    async def status(self, inter: discord.Interaction):
        raid = self.raid_active(inter.guild)
        e = self.bot.embed("🛡 Защита сервера",
                           f"**Режим рейда:** {'🔴 ВКЛЮЧЁН' if raid else '🟢 выключен'}\n"
                           f"**Проверка входа:** {inter.guild.verification_level.name}\n\n"
                           f"• Анти-спам: {SPAM_MSGS} сообщений за {SPAM_WINDOW} сек, {DUP_COUNT} одинаковых подряд, "
                           f"{MASS_MENTIONS}+ упоминаний → мут 10 мин\n"
                           f"• Анти-рейд: {RAID_JOINS} входов за {RAID_WINDOW} сек → режим рейда\n"
                           f"• Анти-нюк: {NUKE_LIMIT} удаления каналов/ролей или бана за минуту → снятие ролей\n"
                           "• Ботов добавляет только владелец сервера\n"
                           "• Руководство, Нач. отдела кадров и Старший состав под анти-спам не попадают",
                           config.COLOR, inter.guild)
        await inter.response.send_message(embed=e, ephemeral=True)

    @group.command(name="рейд", description="Вручную включить или выключить режим рейда")
    @app_commands.rename(on="включить")
    async def raid(self, inter: discord.Interaction, on: bool):
        await inter.response.defer(ephemeral=True)
        await self.set_raid(inter.guild, on, 3600, reason=f"Режим рейда: {inter.user}")
        await inter.followup.send("🔴 Режим рейда включён на час." if on else "🟢 Режим рейда выключен.",
                                          ephemeral=True)


async def setup(bot):
    await bot.add_cog(Security(bot))
