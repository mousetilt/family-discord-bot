# -*- coding: utf-8 -*-
"""Кадры и дисциплина: ранги, должности, исключение, выговоры, мут, отпуск, объявления, авто-состав."""
import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, safe_dm, staff_only


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
        self._roster_task = None
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
        if self._roster_task and not self._roster_task.done():
            return

        async def later():
            await asyncio.sleep(20)
            await self.update_roster(guild)
        self._roster_task = asyncio.create_task(later())

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
        if member.id != inter.user.id and not self.bot.outranks(inter.user, member):
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
    @app_commands.rename(member="участник", reason="причина")
    @staff_only(config.LEAD + ["hr_head"])
    async def warn(self, inter: discord.Interaction, member: discord.Member, reason: str):
        if member.bot:
            return await inter.response.send_message("Боту выговор не выдать 🙂", ephemeral=True)
        if not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя выдать выговор тому, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        n = await self.bot.give_warn(inter.guild, member, inter.user, reason)
        await inter.followup.send(f"✅ Выговор выдан ({n}/3).", ephemeral=True)

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

    @app_commands.command(name="отпуск", description="Уйти в отпуск (не будешь считаться неактивным)")
    @app_commands.rename(days="дней", reason="причина")
    async def vacation(self, inter: discord.Interaction, days: app_commands.Range[int, 1, 60], reason: str):
        if not self.bot.in_family(inter.user):
            return await inter.response.send_message("Только для членов семьи.", ephemeral=True)
        until = now() + dt.timedelta(days=days)
        self.bot.db.q("INSERT OR REPLACE INTO vacations(guild_id, user_id, until, reason) VALUES(?,?,?,?)",
                      inter.guild.id, inter.user.id, until.isoformat(), reason)
        await inter.response.send_message(f"🌙 Отпуск до {discord.utils.format_dt(until, 'D')}. Отдыхай!", ephemeral=True)
        r = self.bot.role(inter.guild, "vacation")
        if r:
            try:
                await inter.user.add_roles(r)
            except discord.HTTPException:
                pass
        await self.bot.log(inter.guild, f"🌙 {inter.user.mention} в отпуске до {discord.utils.format_dt(until, 'D')}: "
                                        f"{reason}", "hrlog")

    @app_commands.command(name="вернулся", description="Вернуться из отпуска")
    async def back(self, inter: discord.Interaction):
        self.bot.db.q("DELETE FROM vacations WHERE guild_id=? AND user_id=?", inter.guild.id, inter.user.id)
        r = self.bot.role(inter.guild, "vacation")
        if r and r in inter.user.roles:
            await inter.user.remove_roles(r)
        await inter.response.send_message("👋 С возвращением!", ephemeral=True)

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
    await bot.add_cog(Staff(bot))
