# -*- coding: utf-8 -*-
"""Активность: время в войсах, сообщения, карточка участника (/профиль), топ, неактив, еженедельный отчёт."""
import asyncio
import datetime as dt
import io
import json
import logging
import random
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import fmt_hours, guarded, now, parse_dt, staff_only

log = logging.getLogger("family-bot.activity")


def plain(s):
    return re.sub(r"[^\w\s\-.,()№/]", "", s or "").strip()


YN = [app_commands.Choice(name="да", value="1"), app_commands.Choice(name="нет", value="0")]
WHO_BY = config.ORGANIZERS + config.HR + ["senior", "treasurer"]  # как справки помощника


PAPER_BY = config.LEAD + ["events"]  # публикуют газету недели


class PaperButton(discord.ui.DynamicItem[discord.ui.Button], template=r"paper:pub"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Опубликовать в объявлениях", emoji="📢",
                                           style=discord.ButtonStyle.primary, custom_id="paper:pub"))

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls()

    async def callback(self, inter: discord.Interaction):
        bot, g = inter.client, inter.guild
        if not bot.has(inter.user, PAPER_BY):
            return await inter.response.send_message("⛔ Публикуют руководство и организаторы.", ephemeral=True)
        text = bot.db.gget(g, "paper:draft")
        c = bot.ch(g, "news")
        if not text or not c:
            return await inter.response.send_message("Черновика нет или нет канала объявлений.", ephemeral=True)
        news = bot.role(g, "n_news")
        try:
            await c.send(content=news.mention if news else None,
                         embed=bot.embed("🗞 Газета недели", text[:4000], config.GOLD, g),
                         allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            return await inter.response.send_message("❌ Не смог написать в объявления.", ephemeral=True)
        bot.db.gdel(g, "paper:draft")
        await inter.response.edit_message(content=f"📢 Опубликовано ({inter.user.mention}).", view=None)


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
    def newspaper(self, guild):
        """Газета недели: только факты из базы (кто в топе, кто пришёл в семью, сборы и явка, летопись)."""
        bot, db, g = self.bot, self.bot.db, guild
        week = (now() - dt.timedelta(days=7)).isoformat()
        parts = [random.choice(["Неделя прошла — вот что было, пока вы спали.", "Свежий выпуск. Читайте, пока горячее.",
                                "Ваш швейцар всё записал. Итоги недели:"])]
        top = db.all("SELECT user_id, week_voice v, week_messages m FROM activity WHERE guild_id=? "
                     "ORDER BY (week_voice/60 + week_messages*2) DESC LIMIT 3", g.id)
        top = [r for r in top if g.get_member(r["user_id"]) and (r["v"] or r["m"])]
        if top:
            medals = ["🥇", "🥈", "🥉"]
            parts.append("**🏆 Самые живые**\n" + "\n".join(
                f"{medals[i]} <@{r['user_id']}> — 🎙 {fmt_hours(r['v'])} · 💬 {r['m']}" for i, r in enumerate(top)))
        new = db.all("SELECT user_id FROM applications WHERE guild_id=? AND status='accepted' AND decided_at>?", g.id, week)
        if new:
            parts.append("**👋 Новые в семье**\n" + ", ".join(f"<@{r['user_id']}>" for r in new[:15]))
        evs = []
        for r in db.all("SELECT data FROM events WHERE guild_id=? AND created_at>?", g.id, week):
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            if d.get("checked") is True and d.get("yes"):
                evs.append(f"• {d['what'][:60]} — пришли {len(d.get('came') or [])} из {len(d['yes'])}")
        if evs:
            parts.append("**📣 Сборы**\n" + "\n".join(evs[:8]))
        chron = db.all("SELECT text FROM chronicle WHERE guild_id=? AND created_at>? ORDER BY id DESC LIMIT 6", g.id, week)
        if chron:
            parts.append("**📖 Из летописи**\n" + "\n".join(f"• {str(r['text'])[:120]}" for r in chron))
        if len(parts) == 1:
            return None
        return bot.brand(g, "\n\n".join(parts))

    async def post_paper(self, guild):
        text = self.newspaper(guild)
        c = self.bot.ch(guild, "botcmd")
        if not text or not c:
            return
        self.bot.db.gset(guild, "paper:draft", text[:4000])
        v = discord.ui.View(timeout=None)
        v.add_item(PaperButton())
        try:
            await c.send(content="🗞 Черновик газеты недели — посмотрите и опубликуйте:",
                         embed=self.bot.embed("🗞 Газета недели", text[:4000], config.GOLD, guild), view=v,
                         allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

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
            if self.bot.feature(g, "reports:newspaper"):  # газета — до обнуления недели (там топ за неделю)
                try:
                    await self.post_paper(g)
                except Exception:
                    log.exception("Газета недели")
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
        e = self.bot.embed("📊 Отчёт об активности", text[:4000], config.GOLD, inter.guild)
        kw = {}
        png = await self.top_chart(inter.guild)
        if png:
            e.set_image(url="attachment://top.png")
            kw["file"] = discord.File(io.BytesIO(png), filename="top.png")
        await inter.followup.send(embed=e, ephemeral=True, **kw)

    async def top_chart(self, guild):
        """Столбики: часы в войсе за неделю у топ-10 (PNG) или None."""
        rows = self.bot.db.all("SELECT user_id, week_voice v FROM activity WHERE guild_id=? AND week_voice>0 "
                               "ORDER BY week_voice DESC LIMIT 10", guild.id)
        rows = [(guild.get_member(r["user_id"]), r["v"]) for r in rows]
        rows = [(m.display_name, round(v / 3600, 1)) for m, v in rows if m]
        if not rows:
            return None
        from core.charts import bars_png
        return await asyncio.to_thread(bars_png, [n for n, _ in rows], [v for _, v in rows], "Часы в войсе за неделю",
                                       " ч")

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="кто", description="Список людей по фильтрам: ранг, взнос, экзамен, войс, выговоры…")
    @app_commands.rename(rank="ранг", dues="взнос", exam="экзамен", voice="в_войсе", warns="выговоры",
                         term="срок_отбыт", risk="на_грани", idle="неактив_дней", limit="сколько")
    @app_commands.choices(rank=[app_commands.Choice(name=config.ROLE_NAMES[k], value=k) for k in config.ROSTER_ORDER][:25],
                          dues=[app_commands.Choice(name="должны", value="unpaid"),
                                app_commands.Choice(name="сдали", value="paid")],
                          exam=YN, voice=YN, warns=YN, term=YN, risk=YN)
    async def who(self, inter: discord.Interaction, rank: str = None, dues: str = None, exam: str = None,
                  voice: str = None, warns: str = None, term: str = None, risk: str = None,
                  idle: app_commands.Range[int, 1, 365] = None, limit: app_commands.Range[int, 1, 60] = None):
        from core import people
        if not self.bot.has(inter.user, WHO_BY):
            return await inter.response.send_message("⛔ Это справка для руководства и должностных.", ephemeral=True)
        yn = {"1": True, "0": False}
        a = {"rank": rank, "dues": dues, "exam": yn.get(exam), "in_voice": yn.get(voice), "warns": yn.get(warns),
             "probation_done": yn.get(term), "at_risk": yn.get(risk), "inactive_days": idle, "limit": limit}
        res = people.query(self.bot, inter.guild, a)
        v = people.view_for(self.bot, inter.user, res)
        await inter.response.send_message(people.render(res)[:1990], ephemeral=True,
                                          **({"view": v} if v is not None else {}))




async def setup(bot):
    bot.add_dynamic_items(PaperButton)
    await bot.add_cog(Activity(bot))
