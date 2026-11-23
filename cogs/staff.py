# -*- coding: utf-8 -*-
"""Кадры и дисциплина: ранги, должности, исключение, выговоры, мут, отпуск, объявления, авто-состав."""
import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import answer_after, guarded, now, now_iso, safe_dm, staff_only


LIMIT_ACTS = {"expel": ("Исключить", "🚪", discord.ButtonStyle.danger),
              "chance": ("Дать шанс", "🤝", discord.ButtonStyle.secondary),
              "unwarn": ("Снять один выговор", "⚪", discord.ButtonStyle.secondary)}


class WarnLimitButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wl:(?P<a>expel|chance|unwarn):(?P<uid>\d+)"):
    """Кнопки под сообщением совету «у X лимит выговоров» — решение в один клик (с проверкой прав и иерархии)."""
    def __init__(self, a, uid):
        label, emoji, style = LIMIT_ACTS[a]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"wl:{a}:{uid}"))
        self.a, self.uid = a, int(uid)

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], match["uid"])

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Staff").limit_decide(inter, self.a, self.uid)


class ExpelReason(discord.ui.Modal, title="Исключение из семьи"):
    reason = discord.ui.TextInput(label="Причина", default="Лимит выговоров", max_length=300)

    def __init__(self, cog, member):
        super().__init__()
        self.cog, self.member = cog, member

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer()
        await self.cog.bot.expel(self.member, inter.user, self.reason.value)
        await self.cog.limit_close(inter, f"🚪 Исключён ({inter.user.mention}): {self.reason.value}")


def limit_view(uid):
    v = discord.ui.View(timeout=None)
    for a in LIMIT_ACTS:
        v.add_item(WarnLimitButton(a, uid))
    return v


class AnnounceModal(discord.ui.Modal, title="Объявление"):
    head = discord.ui.TextInput(label="Заголовок", max_length=200)
    body = discord.ui.TextInput(label="Текст", style=discord.TextStyle.paragraph, max_length=3500)

    def __init__(self, ping_everyone, voice=False):
        super().__init__()
        self.ping_everyone = ping_everyone
        self.voice = voice

    async def on_submit(self, inter: discord.Interaction):
        bot = inter.client
        c = bot.ch(inter.guild, "news") or inter.channel
        e = bot.embed(f"📢 {self.head.value}", self.body.value, config.GOLD, inter.guild)
        e.set_author(name=inter.user.display_name, icon_url=inter.user.display_avatar.url)
        e.timestamp = now()
        news = bot.role(inter.guild, "n_news")
        content = "@everyone" if self.ping_everyone else (news.mention if news else None)
        await c.send(content=content, embed=e, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True))
        await inter.response.send_message("✅ Опубликовано." + (" Зачитываю в голосовых каналах…" if self.voice else ""),
                                          ephemeral=True)
        music = bot.get_cog("Music")
        if self.voice and music:
            await music.announce(inter.guild, [c.id for c in music.voice_targets(inter.guild)],
                                 text=f"Объявление семьи. {self.head.value}. {self.body.value[:400]}")


class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._roster_tasks = {}  # guild_id -> отложенное обновление состава
        self.vacation_loop.start()

    def cog_unload(self):
        self.vacation_loop.cancel()

    # ---------- состав ----------
    async def post_panels(self, guild, repost):
        await self.bot.panel_once(guild, "roster", "roster", repost,
                                  {"embed": self.bot.embed("👥 Состав", "Загружается...", config.COLOR, guild)}, version=2)
        await self.update_roster(guild)

    async def update_roster(self, guild):
        bot = self.bot
        c = bot.ch(guild, "roster")
        mid = bot.db.gget(guild, "panel:roster")
        if not c or not mid:
            return
        vac = bot.role(guild, "vacation")
        seen, total = set(), 0
        e = bot.embed(None, None, config.COLOR, guild)
        for k in config.ROSTER_ORDER:
            role = bot.role(guild, k)
            if not role:
                continue
            people = [m for m in role.members if m.id not in seen and not m.bot]
            if not people:
                continue
            seen.update(m.id for m in people)
            total += len(people)
            lines = [f"{m.mention}{' 🌙' if vac and vac in m.roles else ''}"
                     for m in sorted(people, key=lambda x: x.display_name.lower())]
            text = "\n".join(lines)
            if len(text) > 1000:
                text = text[:990].rsplit("\n", 1)[0] + "\n…"
            if len(e.fields) < 25:
                e.add_field(name=f"{role.name} — {len(people)}", value=text, inline=False)
        e.title = f"👥 Состав семьи {bot.family(guild)['name']} — {total} чел."
        e.description = "Обновляется автоматически · 🌙 — в отпуске"
        e.timestamp = now()
        try:
            m = await c.fetch_message(int(mid))
            await m.edit(embed=e)
        except discord.HTTPException:
            pass

    def schedule_roster(self, guild):
        t = self._roster_tasks.get(guild.id)  # у каждой семьи свой таймер — иначе состав второй не обновлялся
        if t and not t.done():
            return

        async def later():
            await asyncio.sleep(20)
            await self.update_roster(guild)
        self._roster_tasks[guild.id] = asyncio.create_task(later())

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if set(before.roles) != set(after.roles):
            self.schedule_roster(after.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if self.bot.in_family(member):
            await self.bot.log(member.guild, f"📤 {member} покинул сервер (**был в семье!**)", "hrlog", config.RED)
            self.bot.emit(member.guild, "left", member=member)
            self.schedule_roster(member.guild)
        else:
            await self.bot.log(member.guild, f"📤 {member} покинул сервер", color=config.RED)

    # ---------- команды ----------
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="объявление", description="Опубликовать объявление в #объявления")
    @app_commands.rename(everyone="пинг_всех", voice="зачитать_в_войсах")
    @staff_only(config.LEAD)
    async def announce(self, inter: discord.Interaction, everyone: bool = False, voice: bool = False):
        await inter.response.send_modal(AnnounceModal(everyone, voice))

    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.command(name="ранг", description="Установить ранг (Новичок / Член семьи / Старший состав)")
    @app_commands.rename(member="участник", rank="ранг")
    @app_commands.choices(rank=[app_commands.Choice(name=v, value=k) for k, v in config.BASE_RANKS.items()])
    @staff_only(config.LEAD + ["hr_head"])
    async def rank(self, inter: discord.Interaction, member: discord.Member, rank: app_commands.Choice[str]):
        if member.id == inter.user.id or member.bot:
            return await inter.response.send_message("⛔ Себе ранг не меняют — попроси руководство.", ephemeral=True)
        if not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя менять ранг тому, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await self.bot.set_rank(member, rank.value, inter.user)
        await inter.followup.send(f"✅ {member.mention} теперь {rank.name}", ephemeral=True)

    @app_commands.default_permissions(create_events=True)
    @app_commands.command(name="должность", description="Выдать/снять должность (рекрутер, кадры, дипломат, наставник…)")
    @app_commands.rename(member="участник", post="должность")
    @app_commands.choices(post=[app_commands.Choice(name=v, value=k) for k, v in config.STAFF_POSTS.items()])
    @staff_only(config.POST_GIVERS)
    async def post(self, inter: discord.Interaction, member: discord.Member, post: app_commands.Choice[str]):
        ok, why = self.bot.can_post(inter.user, member, post.value)
        if not ok:
            return await inter.response.send_message(f"⛔ {why}", ephemeral=True)
        role = self.bot.role(inter.guild, post.value)
        if not role:
            return await inter.response.send_message("Роль не найдена — запусти /настройка.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        if role in member.roles:
            await member.remove_roles(role)
            act, added = "снята должность", False
        else:
            await member.add_roles(role)
            act, added = "назначен на должность", True
        await self.bot.log(inter.guild, f"🎖 {member.mention} — {act} **{post.name}** ({inter.user.mention})", "hrlog",
                           0x8E24AA)
        self.bot.emit(inter.guild, "post", member=member, post=post.value, added=added, by=inter.user)
        await inter.followup.send(f"✅ {member.mention}: {act} {post.name}", ephemeral=True)

    # ---------- лимит выговоров: решение кнопками ----------
    async def limit_decide(self, inter, action, uid):
        bot, g = self.bot, inter.guild
        m = g.get_member(uid) if g else None
        if not m:
            return await self.limit_close(inter, "Человека уже нет на сервере.", defer=True)
        need = ["leader", "deputy"] if action == "expel" else config.LEAD + ["hr_head"]
        if not bot.has(inter.user, need):
            return await inter.response.send_message("⛔ " + ("Исключают Глава и заместитель." if action == "expel"
                                                             else "Решает руководство или начальник отдела кадров."),
                                                     ephemeral=True)
        if not bot.outranks(inter.user, m):
            return await inter.response.send_message("⛔ Он не ниже тебя по иерархии.", ephemeral=True)
        if action == "expel":
            return await inter.response.send_modal(ExpelReason(self, m))
        await inter.response.defer()
        if action == "unwarn":
            r = bot.db.one("SELECT id FROM warns WHERE guild_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                           g.id, m.id)
            if r:
                bot.db.q("UPDATE warns SET active=0 WHERE id=?", r["id"])
                await bot.log(g, f"⚪ Снят выговор №{r['id']} с {m.mention} ({inter.user.mention}, по лимиту)", "warns")
            return await self.limit_close(inter, f"⚪ Снят последний выговор ({inter.user.mention}).")
        await bot.log(g, f"🤝 {m.mention}: лимит выговоров — решили дать шанс ({inter.user.mention})", "hrlog", 0x26A69A)
        await safe_dm(m, f"🤝 У тебя лимит выговоров, но руководство семьи {config.FAMILY} решило дать тебе шанс. "
                         "Следующий выговор — разговор будет другой.")
        await self.limit_close(inter, f"🤝 Дали шанс ({inter.user.mention}).")

    async def limit_close(self, inter, text, defer=False):
        """Убрать кнопки и дописать решение в сообщение совету."""
        if defer and not inter.response.is_done():
            await inter.response.defer()
        try:
            await inter.message.edit(content=f"{inter.message.content}\n**Решение:** {text}", view=None)
        except (discord.HTTPException, AttributeError):
            pass

    @app_commands.default_permissions(kick_members=True)
    @app_commands.command(name="исключить", description="Исключить участника из семьи")
    @app_commands.rename(member="участник", reason="причина")
    @staff_only(["leader", "deputy"])
    async def expel(self, inter: discord.Interaction, member: discord.Member, reason: str):
        if not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя исключить того, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await self.bot.expel(member, inter.user, reason)
        await inter.followup.send(f"✅ {member.mention} исключён.", ephemeral=True)

    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="выговор", description="Выдать выговор")
    @app_commands.rename(member="участник", reason="причина", fine="штраф")
    @app_commands.describe(fine="Сразу выписать штраф, например 30к (если казна включена)")
    @staff_only(config.LEAD + ["hr_head"])
    async def warn(self, inter: discord.Interaction, member: discord.Member, reason: str, fine: str = None):
        if member.bot:
            return await inter.response.send_message("Боту выговор не выдать 🙂", ephemeral=True)
        if not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя выдать выговор тому, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        n = await self.bot.give_warn(inter.guild, member, inter.user, reason)
        extra = ""
        tr = self.bot.get_cog("Treasury")
        if fine and tr:  # выговор + штраф
            extra = "\n" + await tr.speech(inter.guild, inter.user, {"type": "fine_add", "amount": fine,
                                                                      "reason": reason}, member)
        await inter.followup.send(f"✅ Выговор выдан ({n}/{self.bot.num(inter.guild, 'warn_limit')}).{extra}", ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="выговоры", description="Посмотреть выговоры")
    @app_commands.rename(member="участник")
    async def warns(self, inter: discord.Interaction, member: discord.Member = None):
        member = member or inter.user
        if member != inter.user and not self.bot.has(inter.user, config.LEAD + ["hr_head"]):
            return await inter.response.send_message("⛔ Чужие выговоры видит только руководство.", ephemeral=True)
        rows = self.bot.db.all("SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
                               inter.guild.id, member.id)
        text = "\n".join(f"{'🔴' if r['active'] else '⚪'} №{r['id']} · {r['created_at'][:10]} · {r['reason']}" for r in rows)
        await inter.response.send_message(embed=self.bot.embed(f"Выговоры — {member.display_name}", text or "Чисто ✨",
                                                               config.COLOR, inter.guild), ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="снять-выговор", description="Снять выговор по номеру")
    @app_commands.rename(warn_id="номер")
    @staff_only(config.LEAD)
    async def unwarn(self, inter: discord.Interaction, warn_id: int):
        r = self.bot.db.one("SELECT * FROM warns WHERE guild_id=? AND id=?", inter.guild.id, warn_id)
        if not r:
            return await inter.response.send_message("Не найден.", ephemeral=True)
        self.bot.db.q("UPDATE warns SET active=0 WHERE id=?", warn_id)
        await self.bot.log(inter.guild, f"⚪ Снят выговор №{warn_id} с <@{r['user_id']}> ({inter.user.mention})", "warns")
        await inter.response.send_message("✅ Снят.", ephemeral=True)

    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="мут", description="Временно запретить писать и говорить")
    @app_commands.rename(member="участник", minutes="минут", reason="причина")
    @staff_only(config.LEAD + ["hr_head"])
    async def mute(self, inter: discord.Interaction, member: discord.Member,
                   minutes: app_commands.Range[int, 1, 10080], reason: str):
        if not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя замутить того, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        try:
            await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            return await inter.response.send_message("❌ Discord не даёт мутить администраторов и тех, чья роль выше "
                                                     "роли бота.", ephemeral=True)
        await inter.response.send_message(f"🔇 {member.mention} замучен на {minutes} мин.", ephemeral=True)
        await self.bot.log(inter.guild, f"🔇 {member.mention} мут на {minutes} мин ({inter.user.mention}): {reason}")

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="отпуск", description="Уйти в отпуск (не будешь считаться неактивным)")
    @app_commands.rename(days="дней", reason="причина")
    async def vacation(self, inter: discord.Interaction, days: app_commands.Range[int, 1, 60], reason: str):
        await answer_after(inter, self.start_vacation(inter.guild, inter.user, days, reason), ephemeral=True)

    async def start_vacation(self, guild, member, days, reason):
        """Отпуск (команда, кнопка и речь «ухожу в отпуск на неделю»). → текст ответа."""
        if not self.bot.in_family(member):
            return "Только для членов семьи."
        days = max(1, min(60, int(days or 7)))
        reason = str(reason or "без причины")[:200]
        until = now() + dt.timedelta(days=days)
        self.bot.db.q("INSERT OR REPLACE INTO vacations(guild_id, user_id, until, reason) VALUES(?,?,?,?)",
                      guild.id, member.id, until.isoformat(), reason)
        r = self.bot.role(guild, "vacation")
        if r:
            try:
                await member.add_roles(r)
            except discord.HTTPException:
                pass
        await self.bot.log(guild, f"🌙 {member.mention} в отпуске до {discord.utils.format_dt(until, 'D')}: {reason}",
                           "hrlog")
        return f"🌙 Отпуск до {discord.utils.format_dt(until, 'D')}. Отдыхай!"

    async def end_vacation(self, guild, member):
        self.bot.db.q("DELETE FROM vacations WHERE guild_id=? AND user_id=?", guild.id, member.id)
        r = self.bot.role(guild, "vacation")
        if r and r in member.roles:
            await member.remove_roles(r)
        return "👋 С возвращением!"

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="вернулся", description="Вернуться из отпуска")
    async def back(self, inter: discord.Interaction):
        await answer_after(inter, self.end_vacation(inter.guild, inter.user), ephemeral=True)

    @tasks.loop(hours=1)
    @guarded
    async def vacation_loop(self):
        for row in self.bot.db.all("SELECT * FROM vacations WHERE until<?", now_iso()):
            for g in filter(None, [self.bot.get_guild(row["guild_id"])]):
                m = g.get_member(row["user_id"])
                r = self.bot.role(g, "vacation")
                if m and r and r in m.roles:
                    try:
                        await m.remove_roles(r)
                    except discord.HTTPException:
                        continue  # не вышло снять роль — запись оставляем, попробуем через час
                    await safe_dm(m, f"🌙 Твой отпуск в семье {config.FAMILY} закончился. Ждём в игре!")
            self.bot.db.q("DELETE FROM vacations WHERE guild_id=? AND user_id=?", row["guild_id"], row["user_id"])

    @vacation_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    bot.add_dynamic_items(WarnLimitButton)
    await bot.add_cog(Staff(bot))
