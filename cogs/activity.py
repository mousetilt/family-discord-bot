# -*- coding: utf-8 -*-
"""Активность: время в войсах, сообщения, карточка участника (/профиль), топ, неактив, еженедельный отчёт."""
import datetime as dt
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import fmt_hours, guarded, now, parse_dt, staff_only

log = logging.getLogger("family-bot.activity")


def plain(s):
    return re.sub(r"[^\w\s\-.,()№/]", "", s or "").strip()


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()

    # ---------- учёт ----------
    def counted(self, guild, channel):
        afk = self.bot.ch(guild, "afk")
        return channel is not None and (not afk or channel.id != afk.id)

    def flush(self, member):
        start = self.bot.voice_sessions.pop((member.guild.id, member.id), None)
        if start:
            sec = int((now() - start).total_seconds())
            if sec > 0:
                self.bot.db.touch(member.guild, member.id, voice=sec)

    def flush_all(self, guild):
        for gid, uid in list(self.bot.voice_sessions):
            m = guild.get_member(uid) if gid == guild.id else None  # только сессии ЭТОЙ семьи
            if m:
                self.flush(m)
                if m.voice and self.counted(guild, m.voice.channel):
                    self.bot.voice_sessions[(gid, uid)] = now()

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.home_guilds:
            for vc in g.voice_channels:
                if self.counted(g, vc):
                    for m in vc.members:
                        if not m.bot:
                            self.bot.voice_sessions.setdefault((g.id, m.id), now())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        self.flush(member)
        if self.counted(member.guild, after.channel):
            self.bot.voice_sessions[(member.guild.id, member.id)] = now()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        self.bot.db.touch(message.guild, message.author.id, msgs=1)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        self.flush(member)

    # ---------- профиль ----------
    def profile_embed(self, member):
        """Карточка участника: ранг, в семье с, наставник, войс, сообщения, выговоры, сборы, эфиры, буст.
        Её же показывает «👤 Карточка» в меню по правому клику (там руководство видит ещё и досье)."""
        bot, db, g = self.bot, self.bot.db, member.guild
        self.flush(member)
        if member.voice and self.counted(g, member.voice.channel):
            bot.voice_sessions[(g.id, member.id)] = now()
        a = db.one("SELECT * FROM activity WHERE guild_id=? AND user_id=?", g.id, member.id)
        w = db.one("SELECT COUNT(*) n FROM warns WHERE guild_id=? AND user_id=? AND active=1", g.id, member.id)["n"]
        app = db.one("SELECT * FROM applications WHERE guild_id=? AND user_id=? AND status='accepted' ORDER BY id DESC",
                     g.id, member.id)
        ms = db.one("SELECT mentor_id FROM mentorships WHERE guild_id=? AND newbie_id=? ORDER BY id DESC", g.id, member.id)
        mentor = g.get_member(ms["mentor_id"]) if ms and ms["mentor_id"] else None
        joined = parse_dt(app["decided_at"] or app["created_at"]) if app else member.joined_at
        e = bot.embed(f"🪪 {member.display_name}", None, config.COLOR, g)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Ранг", value=bot.rank_name(member) or "Гость", inline=True)
        e.add_field(name="Ник · Static", value=f"{app['nick'] or '—'} · `{app['static_id'] or '—'}`" if app else "—",
                    inline=True)
        e.add_field(name="В семье с", value=discord.utils.format_dt(joined, "D") if joined else "—", inline=True)
        e.add_field(name="🎙 В войсе", value=(f"{fmt_hours(a['voice_seconds'])} · за неделю {fmt_hours(a['week_voice'])}"
                                             if a else "0 мин"), inline=True)
        e.add_field(name="💬 Сообщений", value=(f"{a['messages']} · за неделю {a['week_messages']}" if a else "0"),
                    inline=True)
        e.add_field(name="⚖️ Выговоры", value=f"{w}/{bot.num(g, 'warn_limit')}", inline=True)
        extra = []
        if mentor:
            extra.append(f"🎓 Наставник: {mentor.mention}")
        gat = bot.get_cog("Gatherings")
        came, total = gat.attendance(member) if gat else (0, 0)
        if total:
            extra.append(f"📣 Сборы: пришёл на {came} из {total}")
        streams = db.one("SELECT COUNT(*) n FROM streams WHERE guild_id=? AND user_id=?", g.id, member.id)["n"]
        if streams:
            extra.append(f"📺 Эфиров: {streams}")
        if getattr(member, "premium_since", None):
            extra.append(f"💎 Бустер семьи с {member.premium_since.strftime('%d.%m.%Y')}")
        if extra:
            e.description = "\n".join(extra)
        return e

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="профиль", description="Карточка участника: ранг, активность, выговоры, сборы")
    @app_commands.rename(member="участник")
    async def profile(self, inter: discord.Interaction, member: discord.Member = None):
        member = member or inter.user
        await inter.response.send_message(embed=self.profile_embed(member), ephemeral=member.id != inter.user.id)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="топ", description="Самые активные участники")
    @app_commands.rename(period="период")
    @app_commands.choices(period=[app_commands.Choice(name="За неделю", value="week"),
                                  app_commands.Choice(name="За всё время", value="all")])
    async def top(self, inter: discord.Interaction, period: app_commands.Choice[str] = None):
        week = not period or period.value == "week"
        self.flush_all(inter.guild)
        vcol, mcol = ("week_voice", "week_messages") if week else ("voice_seconds", "messages")
        rows = self.bot.db.all(f"SELECT user_id, {vcol} v, {mcol} m FROM activity WHERE guild_id=? "
                               f"ORDER BY ({vcol}/60 + {mcol}*2) DESC LIMIT 15", inter.guild.id)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — 🎙 {fmt_hours(r['v'])} · 💬 {r['m']}"
                 for i, r in enumerate(rows)]
        await inter.response.send_message(embed=self.bot.embed(
            f"🏆 Топ активности — {'неделя' if week else 'всё время'}", "\n".join(lines) or "Пока пусто", config.GOLD,
            inter.guild))

    @app_commands.default_permissions(move_members=True)
    @app_commands.command(name="неактив", description="Кто из семьи давно не появлялся")
    @app_commands.rename(days="дней")
    @staff_only(config.HR)
    async def inactive(self, inter: discord.Interaction, days: app_commands.Range[int, 1, 90] = 5):
        bot = self.bot
        border = now() - dt.timedelta(days=days)
        vac = bot.role(inter.guild, "vacation")
        out = []
        for m in bot.family_members(inter.guild):
            if (vac and vac in m.roles) or m.voice:
                continue
            a = bot.db.one("SELECT last_seen FROM activity WHERE guild_id=? AND user_id=?", inter.guild.id, m.id)
            last = parse_dt(a["last_seen"]) if a and a["last_seen"] else None
            if not last or last < border:
                out.append((last, m))
        out.sort(key=lambda x: x[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        text = "\n".join(f"{m.mention} — {discord.utils.format_dt(l, 'R') if l else 'никогда'}" for l, m in out)
        await inter.response.send_message(embed=bot.embed(f"💤 Неактивны больше {days} дн. ({len(out)})",
                                                          text[:4000] or "Все молодцы 💪", config.COLOR, inter.guild),
                                          ephemeral=True)

    # ---------- отчёт ----------
    async def build_report(self, guild):
        bot, db = self.bot, self.bot.db
        self.flush_all(guild)
        rows = []
        for m in bot.family_members(guild):
            a = db.one("SELECT * FROM activity WHERE guild_id=? AND user_id=?", guild.id, m.id)
            rows.append({"name": m.display_name, "voice_h": round((a["week_voice"] if a else 0) / 3600, 1),
                         "msgs": a["week_messages"] if a else 0,
                         "warns": db.one("SELECT COUNT(*) n FROM warns WHERE guild_id=? AND user_id=? AND active=1",
                                         guild.id, m.id)["n"]})
        week = (now() - dt.timedelta(days=7)).isoformat()
        apps = {r["status"]: r["n"] for r in
                db.all("SELECT status, COUNT(*) n FROM applications WHERE guild_id=? AND created_at>? GROUP BY status",
                       guild.id, week)}
        rows.sort(key=lambda r: r["voice_h"] * 60 + r["msgs"] * 2, reverse=True)
        ai = bot.ai_for(guild)
        if ai.on:
            system = bot.brand(guild, f"Ты аналитик семьи «{config.FAMILY}». По данным за неделю напиши отчёт для руководства по-русски: "
                      "1) общая картина, 2) топ-3 активных (кого поощрить), 3) кто проседает, 4) набор новых, "
                      "5) 2-3 конкретных совета. Коротко, с эмодзи, до 1500 символов.")
            try:
                return await ai.ask(system, json.dumps({"members": len(rows), "activity": rows[:60],
                                                            "applications": apps}, ensure_ascii=False), max_tokens=900)
            except Exception as e:
                log.warning("ИИ-отчёт: %s", e)
        top = "\n".join(f"{i + 1}. {r['name']} — {r['voice_h']} ч, {r['msgs']} сообщ." for i, r in enumerate(rows[:5]))
        zero = ", ".join(r["name"] for r in rows if r["voice_h"] == 0 and r["msgs"] == 0) or "нет"
        acc = apps.get("accepted", 0)
        new = sum(apps.values())
        return (f"👥 В семье: **{len(rows)}**\n\n🏆 **Топ недели:**\n{top or '—'}\n\n💤 **Ноль активности:** {zero}\n\n"
                f"📥 **Заявок за неделю:** {new} (принято {acc})")

    @tasks.loop(hours=1)
    @guarded
    async def weekly(self):
        t = now()
        if t.weekday() != 6 or t.hour < 18:  # воскресенье вечером (UTC)
            return
        key = f"report:{t.date()}"
        for g in self.bot.home_guilds:
            if self.bot.db.gget(g, key):
                continue
            c = self.bot.ch(g, "reports")
            if c and self.bot.feature(g, "reports:weekly"):  # отчёт можно выключить, а неделю обнулять надо всегда
                text = await self.build_report(g)
                await c.send(embed=self.bot.embed("📊 Отчёт за неделю", text[:4000], config.GOLD, g))
            self.bot.db.gset(g, key, 1)
            self.bot.db.q("UPDATE activity SET week_voice=0, week_messages=0 WHERE guild_id=?", g.id)

    @weekly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="отчёт", description="Отчёт об активности за текущую неделю")
    @staff_only(config.LEAD)
    async def report(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        text = await self.build_report(inter.guild)
        await inter.followup.send(embed=self.bot.embed("📊 Отчёт об активности", text[:4000], config.GOLD, inter.guild),
                                  ephemeral=True)


async def setup(bot):
    await bot.add_cog(Activity(bot))
