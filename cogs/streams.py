# -*- coding: utf-8 -*-
"""Стримы семьи.
• Статус Discord «Стримит» (привязанный Twitch / YouTube) → анонс в 🔴・эфиры, роль «🔴 В эфире», пинг «🔔 Эфиры».
• «Go Live» (демонстрация экрана) в голосовом канале сервера → короткое сообщение в общий чат.
• /эфир ссылка — ручной анонс (VK Play, Kick и т.д.), /эфиры — кто сейчас в эфире и статистика.
Работает без ключей: данные даёт сам Discord (нужен Presence Intent)."""
import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import now, now_iso, parse_dt

log = logging.getLogger("family-bot.streams")

END_GRACE = 180          # сек: статус пропал — ждём, вдруг стрим перезапустился
RESUME_WINDOW = 20 * 60  # сек: новый эфир с той же ссылкой в течение 20 мин — это тот же эфир
GOLIVE_COOLDOWN = 600
GOLIVE_ANNOUNCE = False  # «включил трансляцию в войсе» — выключено, анонсируем только Twitch/YouTube/TikTok и т.д.    # сек между сообщениями о демонстрации экрана от одного человека
MANUAL_COOLDOWN = 3600   # /эфир не чаще раза в час
STREAM_COLOR = 0xFF2D55

PLATFORMS = [
    (r"twitch\.tv", "Twitch", 0x9146FF), (r"youtu\.?be", "YouTube", 0xFF0000),
    (r"(vkplay\.live|live\.vkvideo\.ru|vkvideo\.ru|vk\.com/video)", "VK Видео", 0x0077FF),
    (r"kick\.com", "Kick", 0x53FC18), (r"trovo\.live", "Trovo", 0x19D66B), (r"tiktok\.com", "TikTok", 0x010101),
]


def platform_of(url, fallback=None):
    for rx, name, color in PLATFORMS:
        if re.search(rx, url or "", re.I):
            return name, color
    return fallback or "Эфир", STREAM_COLOR


def thumbnail(url, twitch_name=None):
    if twitch_name:
        return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{twitch_name.lower()}-1280x720.jpg?t={int(time.time())}"
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", url or "")
    if m:
        return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault_live.jpg?t={int(time.time())}"
    return None


def fmt_dur(sec):
    sec = int(max(sec, 0))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} ч {m} мин" if h else f"{m} мин"


class Streams(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # всё по (guild_id, user_id): стример может быть в нескольких семьях — у каждой свой анонс
        self.live = {}        # (guild, user) -> id записи в streams
        self.end_tasks = {}   # (guild, user) -> asyncio.Task (отложенное завершение)
        self.golive = {}      # (guild, user) -> (message, monotonic)

    def cog_unload(self):
        for t in self.end_tasks.values():
            t.cancel()

    # ---------- общее ----------
    def family(self, member):
        return isinstance(member, discord.Member) and not member.bot and self.bot.has(member, config.FAMILY_PLUS)

    def is_main(self, guild):
        """Эфиры — у любой семьи (раньше только у GUILD_ID: состояние было общим на всех)."""
        return guild is not None and self.bot.is_family(guild)

    def embed(self, member, row, ended=False):
        plat, color = platform_of(row["url"], row["platform"])
        if ended:
            dur = (parse_dt(row["ended_at"]) - parse_dt(row["started_at"])).total_seconds()
            e = discord.Embed(title=f"⚫ {member.display_name} — эфир завершён",
                              description=f"**{row['title'] or 'Без названия'}**\n\n⏱ Длительность: **{fmt_dur(dur)}**",
                              color=0x424242)
        else:
            e = discord.Embed(title=f"🔴 {member.display_name} в эфире!", url=row["url"],
                              description=f"**{row['title'] or 'Без названия'}**", color=color)
            if row["game"]:
                e.add_field(name="Игра", value=row["game"])
            e.add_field(name="Платформа", value=plat)
            img = thumbnail(row["url"], row.get("_twitch") if isinstance(row, dict) else None)
            if img:
                e.set_image(url=img)
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        e.set_footer(text=f"Семья {self.bot.family(member.guild)['name']} · {'анонс вручную' if row['manual'] else plat}")
        e.timestamp = parse_dt(row["started_at"])
        return e

    def link_view(self, url):
        v = discord.ui.View(timeout=None)
        if url and re.match(r"https?://", url):
            v.add_item(discord.ui.Button(label="Смотреть", emoji="📺", url=url))
        return v

    async def set_live_role(self, member, on):
        r = self.bot.role(member.guild, "live")
        if not r:
            return
        try:
            if on and r not in member.roles:
                await member.add_roles(r, reason="Начал эфир")
            elif not on and r in member.roles:
                await member.remove_roles(r, reason="Эфир завершён")
        except discord.HTTPException:
            pass

    async def announce(self, member, url, title, game, platform, manual=False, twitch_name=None):
        bot, g = self.bot, member.guild
        ch = bot.ch(g, "streams") or bot.ch(g, "chat")
        if not ch:
            return None
        cur = bot.db.q("INSERT INTO streams(guild_id, user_id, platform, title, url, game, started_at, manual) "
                       "VALUES(?,?,?,?,?,?,?,?)", g.id, member.id, platform, (title or "")[:200], url, (game or "")[:100], now_iso(), int(manual))
        row = dict(bot.db.one("SELECT * FROM streams WHERE id=?", cur.lastrowid))
        row["_twitch"] = twitch_name
        ping = bot.role(g, "n_streams")
        try:
            msg = await ch.send(content=ping.mention if ping else None, embed=self.embed(member, row),
                                view=self.link_view(url), allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException as e:
            log.warning("Анонс эфира: %s", e)
            return cur.lastrowid
        bot.db.q("UPDATE streams SET message_id=?, channel_id=? WHERE id=?", msg.id, ch.id, cur.lastrowid)
        return cur.lastrowid

    async def finish(self, member, sid):
        bot = self.bot
        bot.db.q("UPDATE streams SET ended_at=? WHERE id=? AND ended_at IS NULL", now_iso(), sid)
        row = bot.db.one("SELECT * FROM streams WHERE id=?", sid)
        self.live.pop((member.guild.id, member.id), None)
        await self.set_live_role(member, False)
        if row and row["message_id"]:
            ch = member.guild.get_channel(row["channel_id"])
            try:
                msg = await ch.fetch_message(row["message_id"])
                await msg.edit(content=None, embed=self.embed(member, row, ended=True), view=self.link_view(row["url"]))
            except (discord.HTTPException, AttributeError):
                pass

    # ---------- статус «Стримит» ----------
    @staticmethod
    def streaming(member):
        return next((a for a in getattr(member, "activities", ()) if isinstance(a, discord.Streaming)), None)

    async def handle(self, member):
        if not self.family(member) or not self.is_main(member.guild):
            return
        st = self.streaming(member)
        uid = (member.guild.id, member.id)
        if st:
            t = self.end_tasks.pop(uid, None)
            if t:
                t.cancel()
            if uid in self.live:
                return
            last = self.bot.db.one("SELECT * FROM streams WHERE guild_id=? AND user_id=? AND manual=0 ORDER BY id DESC",
                                   member.guild.id, member.id)
            if last and last["url"] == st.url and (
                    last["ended_at"] is None or (now() - parse_dt(last["ended_at"])).total_seconds() < RESUME_WINDOW):
                # тот же эфир (перезапуск или рестарт бота) — без повторного анонса
                self.bot.db.q("UPDATE streams SET ended_at=NULL WHERE id=?", last["id"])
                self.live[uid] = last["id"]
                await self.set_live_role(member, True)
                return
            plat = st.platform or platform_of(st.url)[0]
            sid = await self.announce(member, st.url, st.name or st.details, st.game, plat,
                                      twitch_name=getattr(st, "twitch_name", None))
            if sid:
                self.live[uid] = sid
                await self.set_live_role(member, True)
                self.bot.emit(member.guild, "stream", member=member, url=st.url, title=st.name)
        elif uid in self.live and uid not in self.end_tasks:
            self.end_tasks[uid] = asyncio.create_task(self._delayed_end(member, self.live[uid]))

    async def _delayed_end(self, member, sid):
        try:
            await asyncio.sleep(END_GRACE)
            m = member.guild.get_member(member.id) or member
            if not self.streaming(m):
                await self.finish(m, sid)
        except asyncio.CancelledError:
            pass
        finally:
            self.end_tasks.pop((member.guild.id, member.id), None)

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        if self.streaming(before) is None and self.streaming(after) is None and \
                (after.guild.id, after.id) not in self.live:
            return
        await self.handle(after)

    @commands.Cog.listener()
    async def on_ready(self):
        # восстанавливаем состояние после перезапуска
        for row in self.bot.db.all("SELECT * FROM streams WHERE ended_at IS NULL AND manual=0"):
            for g in filter(None, [self.bot.get_guild(row["guild_id"])]):
                m = g.get_member(row["user_id"])
                if m:
                    if self.streaming(m):
                        self.live[(g.id, m.id)] = row["id"]
                    else:
                        await self.finish(m, row["id"])
                    break
            else:
                self.bot.db.q("UPDATE streams SET ended_at=? WHERE id=?", now_iso(), row["id"])
        for g in self.bot.home_guilds:
            if not self.is_main(g):
                continue
            r = self.bot.role(g, "live")
            for m in list(r.members) if r else []:
                if (g.id, m.id) not in self.live and not self.streaming(m):
                    await self.set_live_role(m, False)
            if self.bot.presences_on:
                for m in g.members:
                    if self.streaming(m) and (g.id, m.id) not in self.live:
                        await self.handle(m)

    # ---------- демонстрация экрана в войсе ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not GOLIVE_ANNOUNCE or not self.family(member) or not self.is_main(member.guild):
            return
        started = after.self_stream and after.channel and (not before.self_stream or before.channel != after.channel)
        stopped = before.self_stream and (not after.self_stream or after.channel != before.channel)
        gk = (member.guild.id, member.id)
        if stopped and gk in self.golive:
            msg, _ = self.golive[gk]
            if msg:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
                self.golive[gk] = (None, self.golive[gk][1])
        if not started:
            return
        vc = after.channel
        base = self.bot.role(member.guild, "member")
        if base and not vc.permissions_for(base).view_channel:
            return  # закрытые каналы (совет, «дела семьи») не светим
        last = self.golive.get(gk, (None, 0))[1]
        if time.monotonic() - last < GOLIVE_COOLDOWN:
            return
        ch = self.bot.ch(member.guild, "chat")
        if not ch:
            return
        try:
            msg = await ch.send(f"📺 **{member.display_name}** включил трансляцию в {vc.mention} — заходи смотреть!",
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return
        self.golive[gk] = (msg, time.monotonic())

    # ---------- команды ----------
    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="эфир", description="Анонсировать свой стрим (если статус «Стримит» не появился сам)")
    @app_commands.rename(url="ссылка", title="название")
    @app_commands.describe(url="Ссылка на трансляцию (Twitch, YouTube, VK Видео, Kick…)", title="О чём эфир")
    async def manual(self, inter: discord.Interaction, url: str, title: str = None):
        if not self.family(inter.user):
            return await inter.response.send_message("⛔ Анонсы эфиров — только для семьи и друзей семьи.", ephemeral=True)
        url = url.strip()
        if not re.match(r"https?://\S+\.\S+", url):
            return await inter.response.send_message("Нужна ссылка вида https://twitch.tv/ник", ephemeral=True)
        last = self.bot.db.one("SELECT started_at FROM streams WHERE guild_id=? AND user_id=? AND manual=1 "
                               "ORDER BY id DESC", inter.guild.id, inter.user.id)
        if last and (now() - parse_dt(last["started_at"])).total_seconds() < MANUAL_COOLDOWN and \
                not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⏳ Ручной анонс — не чаще раза в час.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        plat = platform_of(url)[0]
        sid = await self.announce(inter.user, url, title or "Прямой эфир", None, plat, manual=True)
        self.bot.db.q("UPDATE streams SET ended_at=started_at WHERE id=?", sid)
        ch = self.bot.ch(inter.guild, "streams")
        await inter.followup.send(f"✅ Анонс опубликован{' в ' + ch.mention if ch else ''}.", ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="эфиры", description="Кто сейчас стримит и статистика стримеров семьи")
    async def stats(self, inter: discord.Interaction):
        g, db = inter.guild, self.bot.db
        now_live = []
        for (gid, uid), sid in self.live.items():
            if gid != g.id:
                continue
            m, row = g.get_member(uid), db.one("SELECT * FROM streams WHERE id=?", sid)
            if m and row:
                dur = (now() - parse_dt(row["started_at"])).total_seconds()
                now_live.append(f"🔴 **{m.display_name}** — [{row['title'] or 'эфир'}]({row['url']}) · {fmt_dur(dur)}")
        rows = db.all("SELECT user_id, COUNT(*) n, SUM(CASE WHEN manual=0 AND ended_at IS NOT NULL THEN "
                      "(julianday(ended_at)-julianday(started_at))*86400 ELSE 0 END) s FROM streams "
                      "WHERE guild_id=? GROUP BY user_id ORDER BY n DESC LIMIT 10", g.id)
        top = []
        for i, r in enumerate(rows, 1):
            m = g.get_member(r["user_id"])
            if m:
                hours = f" · {fmt_dur(r['s'])}" if r["s"] else ""
                top.append(f"`{i:>2}.` {m.display_name} — {r['n']} эфир(ов){hours}")
        desc = ("**Сейчас в эфире**\n" + ("\n".join(now_live) or "Никого") + "\n\n**Стримеры семьи**\n" +
                ("\n".join(top) or "Пока никто не стримил"))
        if not self.bot.presences_on:
            desc += ("\n\n⚠️ Автоанонсы выключены: в Developer Portal → Bot включи **Presence Intent** "
                     "и перезапусти бота.")
        desc += ("\n\n💡 Чтобы бот сам анонсировал твои эфиры: Настройки Discord → Интеграции → привяжи Twitch "
                 "или YouTube. Пинги об эфирах — роль «🔔 Эфиры» в канале уведомлений.")
        await inter.response.send_message(embed=self.bot.embed("📺 Эфиры семьи", desc, STREAM_COLOR, g), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Streams(bot))
