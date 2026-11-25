# -*- coding: utf-8 -*-
"""Личные голосовые комнаты: зашёл в «➕ Создать комнату» → своя комната; опустела → удалилась."""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

bot_log = logging.getLogger("bot.voice_rooms")

OWNER_PERMS = dict(view_channel=True, connect=True, speak=True, manage_channels=True, move_members=True,
                   mute_members=True)
# Перенесли в войс, куда человеку нельзя (друга семьи — в семейный): даём говорить, пока он там
GUEST_PERMS = dict(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True)
GUEST_MIN = dict(view_channel=True, connect=True, speak=True)  # если у бота самого нет права на стрим

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_grants(
    channel_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY(channel_id, user_id)
);
"""


def needs_grant(member, channel, afk_id=None):
    """Человека перенесли туда, куда сам он зайти не мог, и говорить он там не может → выдать доступ.
    Сам зашёл (есть право входа), а говорить нельзя — значит, канал так задуман (AFK, «Мероприятие»): не трогаем.
    Тайм-аут (мут) и личный запрет на этот канал — наказание, его не обходим."""
    if member.bot or not isinstance(channel, discord.VoiceChannel):
        return False
    if afk_id and channel.id == afk_id or member.guild.afk_channel and channel.id == member.guild.afk_channel.id:
        return False
    if member.is_timed_out():
        return False
    own = channel.overwrites.get(member)
    if own is not None and (own.speak is False or own.connect is False):
        return False
    p = channel.permissions_for(member)
    return not p.speak and not (p.view_channel and p.connect)


class VoiceRooms(commands.Cog):
    room = app_commands.Group(name="комната", description="Управление личной голосовой комнатой",
                              default_permissions=discord.Permissions(manage_messages=True))  # участникам — панель

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)

    async def grant(self, member, channel):
        """Перенесли в «чужой» войс → может говорить. Отметка в базе — чтобы снять доступ на выходе (и после
        перезапуска бота)."""
        bot = self.bot
        if not bot.is_family(member.guild) or not bot.feature(member.guild, "voice:moved"):
            return False
        afk = bot.ch(member.guild, "afk")
        if not needs_grant(member, channel, afk.id if afk else None):
            return False
        why = "Перенесли в войс — может говорить, пока он здесь"
        try:
            try:
                await channel.set_permissions(member, overwrite=discord.PermissionOverwrite(**GUEST_PERMS), reason=why)
            except discord.Forbidden:  # Discord не даёт выдать право, которого нет у самого бота
                await channel.set_permissions(member, overwrite=discord.PermissionOverwrite(**GUEST_MIN), reason=why)
        except discord.HTTPException as e:
            bot_log.warning("Доступ в войс %s для %s: %s", channel.id, member.id, e)
            return False
        bot.db.q("INSERT OR REPLACE INTO voice_grants(channel_id, guild_id, user_id, ts) VALUES(?,?,?,?)",
                 channel.id, member.guild.id, member.id, int(time.time()))
        return True

    async def revoke(self, guild, channel_id, user_id):
        """Снять выданный доступ (вышел из войса). Личные права, которые поставил не бот, не трогаем."""
        self.bot.db.q("DELETE FROM voice_grants WHERE channel_id=? AND user_id=?", channel_id, user_id)
        ch = guild.get_channel(channel_id) if guild else None
        member = guild.get_member(user_id) if guild else None
        if not ch or not member:
            return
        own = ch.overwrites.get(member)
        if own is None or own.pair() not in (discord.PermissionOverwrite(**GUEST_PERMS).pair(),
                                             discord.PermissionOverwrite(**GUEST_MIN).pair()):
            return  # права уже поменяли руками — оставляем как есть
        try:
            await ch.set_permissions(member, overwrite=None, reason="Вышел из войса — временный доступ снят")
        except discord.HTTPException as e:
            bot_log.warning("Снять доступ в войс %s для %s: %s", channel_id, user_id, e)

    async def create_room(self, member):
        bot, guild = self.bot, member.guild
        category = bot.ch(guild, "temp_cat")
        if not category:
            return
        ow = dict(category.overwrites)
        ow[member] = discord.PermissionOverwrite(**OWNER_PERMS)
        ch = await guild.create_voice_channel(f"🔊 {member.display_name}"[:100], category=category, overwrites=ow,
                                                bitrate=int(guild.bitrate_limit))
        bot.db.q("INSERT OR REPLACE INTO temp_voice(channel_id, guild_id, owner_id) VALUES(?,?,?)", ch.id, guild.id,
                 member.id)
        try:
            await member.move_to(ch)
        except discord.HTTPException:
            await ch.delete()
            bot.db.q("DELETE FROM temp_voice WHERE channel_id=?", ch.id)
            return
        try:
            await ch.send(f"{member.mention}, это твоя комната. Управление: `/комната имя` · `/комната лимит` · "
                          "`/комната закрыть` · `/комната открыть` · `/комната выгнать` · `/комната передать`\n"
                          "Когда все выйдут — комната удалится сама.")
        except discord.HTTPException:
            pass

    async def maybe_delete(self, channel):
        if channel and not channel.members and \
                self.bot.db.one("SELECT 1 FROM temp_voice WHERE channel_id=?", channel.id):
            try:
                await channel.delete(reason="Личная комната опустела")
            except discord.HTTPException:
                pass
            self.bot.db.q("DELETE FROM temp_voice WHERE channel_id=?", channel.id)

    @commands.Cog.listener()
    async def on_ready(self):
        # выданный перенесённым доступ: кто уже вышел, пока бот лежал, — снимаем
        for row in self.bot.db.all("SELECT channel_id, guild_id, user_id FROM voice_grants"):
            g = self.bot.get_guild(row["guild_id"])
            m = g.get_member(row["user_id"]) if g else None
            if not (m and m.voice and m.voice.channel and m.voice.channel.id == row["channel_id"]):
                await self.revoke(g, row["channel_id"], row["user_id"])
        for row in self.bot.db.all("SELECT channel_id FROM temp_voice"):
            ch = self.bot.get_channel(row["channel_id"])
            if ch is None:
                self.bot.db.q("DELETE FROM temp_voice WHERE channel_id=?", row["channel_id"])
            else:
                await self.maybe_delete(ch)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        if before.channel and self.bot.db.one("SELECT 1 FROM voice_grants WHERE channel_id=? AND user_id=?",
                                              before.channel.id, member.id):
            await self.revoke(member.guild, before.channel.id, member.id)
        if after.channel:
            await self.grant(member, after.channel)
        creator = self.bot.ch(member.guild, "creator")
        if not member.bot and after.channel and creator and after.channel.id == creator.id:
            await self.create_room(member)
        if before.channel:
            await self.maybe_delete(before.channel)  # и когда последним вышел бот (музыка) — тоже удаляем

    async def room_speech(self, member, op, value=None):
        """Речь: «закрой мою комнату», «лимит 3», «назови комнату Кухня 2». → текст ответа."""
        from core import config
        vc = member.voice.channel if member.voice else None
        row = self.bot.db.one("SELECT owner_id FROM temp_voice WHERE channel_id=?", vc.id) if vc else None
        if not row:
            return "❌ Зайди в свою личную комнату — тогда сделаю."
        if row["owner_id"] != member.id and not self.bot.has(member, config.LEAD):
            return "⛔ Это не твоя комната."
        if op == "limit":
            try:
                n = max(0, min(99, int(value or 0)))
            except (TypeError, ValueError):
                return "❌ Сколько мест? Число от 0 до 99."
            await vc.edit(user_limit=n)
            return f"✅ Лимит: {n or 'нет'}"
        if op == "name":
            name = str(value or "").strip()[:90]
            if not name:
                return "❌ Как назвать?"
            try:
                await asyncio.wait_for(vc.edit(name=f"🔊 {name}"), timeout=6)
            except asyncio.TimeoutError:
                return "⏳ Discord разрешает менять имя комнаты 2 раза за 10 минут — попробуй позже."
            return "✅ Переименовал."
        if op == "lock":
            ow = dict(vc.overwrites)
            for target, perm in ow.items():
                if isinstance(target, discord.Role):
                    perm.connect = False
            for m in vc.members:
                ow.setdefault(m, discord.PermissionOverwrite()).connect = True
            await vc.edit(overwrites=ow)
            return "🔒 Комната закрыта."
        if op == "unlock":
            ow = dict(vc.category.overwrites)
            owner = vc.guild.get_member(row["owner_id"])
            if owner:
                ow[owner] = discord.PermissionOverwrite(**OWNER_PERMS)
            await vc.edit(overwrites=ow)
            return "🔓 Комната открыта."
        return "❌ Что сделать с комнатой: закрыть, открыть, лимит или имя?"

    async def my_room(self, inter):
        vc = inter.user.voice.channel if inter.user.voice else None
        if not vc:
            await inter.response.send_message("Зайди в свою комнату.", ephemeral=True)
            return None
        row = self.bot.db.one("SELECT owner_id FROM temp_voice WHERE channel_id=?", vc.id)
        if not row:
            await inter.response.send_message("Это не личная комната.", ephemeral=True)
            return None
        from core import config
        if row["owner_id"] != inter.user.id and not self.bot.has(inter.user, config.LEAD):
            await inter.response.send_message("Это не твоя комната.", ephemeral=True)
            return None
        return vc

    @room.command(name="имя", description="Переименовать комнату")
    @app_commands.rename(name="название")
    async def r_name(self, inter: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        vc = await self.my_room(inter)
        if vc:
            await inter.response.defer(ephemeral=True)
            try:  # Discord разрешает менять имя 2 раза за 10 минут — дальше запрос «висит», не ждём его
                await asyncio.wait_for(vc.edit(name=f"🔊 {name}"), timeout=6)
            except asyncio.TimeoutError:
                return await inter.followup.send("⏳ Discord разрешает менять имя комнаты 2 раза за 10 минут — "
                                                 "попробуй позже.", ephemeral=True)
            await inter.followup.send("✅ Готово.", ephemeral=True)

    @room.command(name="лимит", description="Ограничить число мест (0 — без лимита)")
    @app_commands.rename(limit="мест")
    async def r_limit(self, inter: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        vc = await self.my_room(inter)
        if vc:
            await vc.edit(user_limit=limit)
            await inter.response.send_message(f"✅ Лимит: {limit or 'нет'}", ephemeral=True)

    @room.command(name="закрыть", description="Закрыть комнату — больше никто не зайдёт")
    async def r_lock(self, inter: discord.Interaction):
        vc = await self.my_room(inter)
        if not vc:
            return
        ow = dict(vc.overwrites)
        for target, perm in ow.items():
            if isinstance(target, discord.Role):
                perm.connect = False
        for m in vc.members:
            ow.setdefault(m, discord.PermissionOverwrite()).connect = True
        await vc.edit(overwrites=ow)
        await inter.response.send_message("🔒 Комната закрыта.", ephemeral=True)

    @room.command(name="открыть", description="Открыть комнату")
    async def r_unlock(self, inter: discord.Interaction):
        vc = await self.my_room(inter)
        if not vc:
            return
        owner = self.bot.db.one("SELECT owner_id FROM temp_voice WHERE channel_id=?", vc.id)["owner_id"]
        ow = dict(vc.category.overwrites)
        m = inter.guild.get_member(owner)
        if m:
            ow[m] = discord.PermissionOverwrite(**OWNER_PERMS)
        await vc.edit(overwrites=ow)
        await inter.response.send_message("🔓 Комната открыта.", ephemeral=True)

    @room.command(name="выгнать", description="Выгнать и запретить вход")
    @app_commands.rename(member="участник")
    async def r_kick(self, inter: discord.Interaction, member: discord.Member):
        vc = await self.my_room(inter)
        if not vc:
            return
        await vc.set_permissions(member, connect=False)
        if member in vc.members:
            await member.move_to(None)
        await inter.response.send_message(f"👢 {member.display_name} выгнан.", ephemeral=True)

    @room.command(name="передать", description="Передать комнату другому участнику")
    @app_commands.rename(member="участник")
    async def r_transfer(self, inter: discord.Interaction, member: discord.Member):
        vc = await self.my_room(inter)
        if not vc:
            return
        self.bot.db.q("UPDATE temp_voice SET owner_id=? WHERE channel_id=?", member.id, vc.id)
        await vc.set_permissions(member, **OWNER_PERMS)
        await vc.set_permissions(inter.user, overwrite=None)
        await inter.response.send_message(f"👑 Комната передана {member.mention}.")


async def setup(bot):
    await bot.add_cog(VoiceRooms(bot))
