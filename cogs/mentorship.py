# -*- coding: utf-8 -*-
"""Наставничество: каждому принятому новичку — наставник, личная ветка, чек-лист и решение через N дней."""
import datetime as dt
import json
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import fmt_hours, guarded, now, now_iso, parse_dt, safe_dm, staff_only

log = logging.getLogger("family-bot.mentor")
DECISIONS = {
    "promote": ("Перевести в Члены семьи", "✅", discord.ButtonStyle.success),
    "extend": ("Продлить на 3 дня", "⏳", discord.ButtonStyle.secondary),
    "fail": ("Не прошёл — исключить", "❌", discord.ButtonStyle.danger),
}


class MentorCheck(discord.ui.DynamicItem[discord.ui.Button], template=r"mchk:(?P<mid>\d+):(?P<i>\d+)"):
    def __init__(self, mid, i, done=False):
        super().__init__(discord.ui.Button(label=str(i + 1), emoji="✅" if done else "⬜",
                                           style=discord.ButtonStyle.success if done else discord.ButtonStyle.secondary,
                                           custom_id=f"mchk:{mid}:{i}"))
        self.mid, self.i = mid, i

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]), int(match["i"]))

    async def callback(self, inter):
        await inter.client.get_cog("Mentorship").toggle(inter, self.mid, self.i)


class MentorStat(discord.ui.DynamicItem[discord.ui.Button], template=r"mstat:(?P<mid>\d+)"):
    def __init__(self, mid):
        super().__init__(discord.ui.Button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mstat:{mid}"))
        self.mid = mid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]))

    async def callback(self, inter):
        cog = inter.client.get_cog("Mentorship")
        row = inter.client.db.one("SELECT * FROM mentorships WHERE id=?", self.mid)
        if not row:
            return await inter.response.send_message("Не найдено.", ephemeral=True)
        await inter.response.edit_message(embed=cog.card(inter.guild, row), view=cog.view(row))


class MentorDecision(discord.ui.DynamicItem[discord.ui.Button], template=r"mdec:(?P<mid>\d+):(?P<a>promote|extend|fail)"):
    def __init__(self, mid, action):
        label, emoji, style = DECISIONS[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"mdec:{mid}:{action}"))
        self.mid, self.action = mid, action

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]), match["a"])

    async def callback(self, inter):
        await inter.client.get_cog("Mentorship").decide(inter, self.mid, self.action)


class Mentorship(commands.Cog):
    group = app_commands.Group(name="наставник", description="Наставничество для новичков", default_permissions=discord.Permissions(move_members=True))

    def __init__(self, bot):
        self.bot = bot
        self.due_loop.start()

    def cog_unload(self):
        self.due_loop.cancel()

    # ---------- данные ----------
    def stats(self, row):
        db = self.bot.db
        a = db.one("SELECT voice_seconds FROM activity WHERE user_id=?", row["newbie_id"])
        voice = max(0, (a["voice_seconds"] if a else 0) - (row["voice_start"] or 0))
        gatherings = 0
        for ev in db.all("SELECT data FROM events WHERE created_at>=?", row["started_at"]):
            try:
                if row["newbie_id"] in json.loads(ev["data"]).get("yes", []):
                    gatherings += 1
            except (ValueError, TypeError):
                pass
        warns = db.one("SELECT COUNT(*) n FROM warns WHERE user_id=? AND created_at>=? AND active=1",
                       row["newbie_id"], row["started_at"])["n"]
        return voice, gatherings, warns

    def card(self, guild, row):
        checklist = json.loads(row["checklist"] or "[]")
        done = sum(1 for x in checklist if x)
        total = len(config.MENTOR_CHECKLIST)
        bar = "▰" * done + "▱" * (total - done)
        voice, gatherings, warns = self.stats(row)
        status = {"active": "🟢 Идёт испытательный срок", "done": "🎓 Пройден", "failed": "❌ Не пройден",
                  "left": "🚪 Ушёл с сервера"}.get(row["status"], row["status"])
        lines = "\n".join(f"{'✅' if i < len(checklist) and checklist[i] else '⬜'} **{i + 1}.** {t}"
                          for i, t in enumerate(config.MENTOR_CHECKLIST))
        e = self.bot.embed(
            "🎓 Испытательный срок",
            f"**Новичок:** <@{row['newbie_id']}>\n**Наставник:** <@{row['mentor_id']}>\n**Статус:** {status}\n"
            f"**Начало:** {discord.utils.format_dt(parse_dt(row['started_at']), 'D')} · "
            f"**Решение:** {discord.utils.format_dt(parse_dt(row['due_at']), 'R')}\n\n"
            f"**Чек-лист** `{bar}` {done}/{total}\n{lines}\n\n"
            f"📊 **За срок:** 🎙 {fmt_hours(voice)} · 📣 сборов: {gatherings} · ⚖️ выговоров: {warns}",
            config.GOLD, guild)
        e.set_footer(text="Отмечать пункты может наставник или руководство · " + config.FOOTER)
        return e

    def view(self, row):
        v = discord.ui.View(timeout=None)
        if row["status"] != "active":
            return v
        checklist = json.loads(row["checklist"] or "[]")
        for i in range(len(config.MENTOR_CHECKLIST)):
            v.add_item(MentorCheck(row["id"], i, i < len(checklist) and bool(checklist[i])))
        v.add_item(MentorStat(row["id"]))
        if checklist and all(checklist) and len(checklist) >= len(config.MENTOR_CHECKLIST):
            v.add_item(MentorDecision(row["id"], "promote"))
        return v

    async def get_thread(self, guild, tid):
        th = guild.get_thread(tid) if tid else None
        if th is None and tid:
            try:
                th = await self.bot.fetch_channel(tid)
            except discord.HTTPException:
                return None
        if th is not None and getattr(th, "archived", False):
            try:
                await th.edit(archived=False)
            except discord.HTTPException:
                pass
        return th

    def can_manage(self, user, row):
        return user.id == row["mentor_id"] or self.bot.has(user, config.MENTORS)

    # ---------- назначение ----------
    def pick_mentor(self, guild, exclude_id):
        role = self.bot.role(guild, "mentor")
        vac = self.bot.role(guild, "vacation")
        if not role:
            return None
        cands = [m for m in role.members if not m.bot and m.id != exclude_id and not (vac and vac in m.roles)]
        if not cands:
            return None
        load = {m.id: self.bot.db.one("SELECT COUNT(*) n FROM mentorships WHERE mentor_id=? AND status='active'",
                                      m.id)["n"] for m in cands}
        low = min(load.values())
        return random.choice([m for m in cands if load[m.id] == low])

    async def assign(self, guild, newbie, mentor=None, by=None):
        bot, db = self.bot, self.bot.db
        mentor = mentor or self.pick_mentor(guild, newbie.id)
        if not mentor:
            await bot.log(guild, f"⚠️ Для {newbie.mention} не нашлось свободного наставника. Назначьте вручную: "
                                 f"`/наставник назначить`", "hrlog", 0xFB8C00)
            return None
        ch = bot.ch(guild, "mentoring")
        if not ch:
            return None
        thread = await ch.create_thread(name=f"🎓 {newbie.display_name}"[:100], type=discord.ChannelType.private_thread,
                                        invitable=False, auto_archive_duration=10080)
        for u in (newbie, mentor):
            try:
                await thread.add_user(u)
            except discord.HTTPException:
                pass
        a = db.one("SELECT voice_seconds FROM activity WHERE user_id=?", newbie.id)
        start = now()
        cur = db.q("INSERT INTO mentorships(newbie_id, mentor_id, thread_id, checklist, started_at, due_at, voice_start)"
                   " VALUES(?,?,?,?,?,?,?)", newbie.id, mentor.id, thread.id,
                   json.dumps([False] * len(config.MENTOR_CHECKLIST)), start.isoformat(),
                   (start + dt.timedelta(days=config.MENTOR_DAYS)).isoformat(), a["voice_seconds"] if a else 0)
        row = db.one("SELECT * FROM mentorships WHERE id=?", cur.lastrowid)
        msg = await thread.send(content=f"{newbie.mention} {mentor.mention}", embed=self.card(guild, row),
                                view=self.view(row))
        db.q("UPDATE mentorships SET message_id=? WHERE id=?", msg.id, row["id"])
        await thread.send(f"👋 {newbie.mention}, это твоя личная ветка с наставником {mentor.mention}. "
                          f"Здесь можно задавать любые вопросы — о семье, правилах, игре.\n"
                          f"Испытательный срок — **{config.MENTOR_DAYS} дн.** Наставник отмечает пункты чек-листа, "
                          f"а в конце решает, переводить ли тебя в 👥 Члены семьи. Удачи!")
        await safe_dm(mentor, f"🎓 Тебе назначен новичок **{newbie.display_name}**. Ветка: {thread.jump_url}")
        await bot.log(guild, f"🎓 {mentor.mention} — наставник для {newbie.mention}{' (назначил ' + by.mention + ')' if by else ''}",
                      "hrlog", 0x26A69A)
        bot.emit(guild, "mentor_assigned", member=newbie, mentor=mentor)
        return row

    @commands.Cog.listener()
    async def on_family_event(self, guild, kind, data):
        if kind == "accepted" and data.get("member"):
            try:
                await self.assign(guild, data["member"])
            except Exception:
                log.exception("Не удалось назначить наставника")
        # исключили или вручную повысили — испытательный срок закрываем, чтобы бот потом не спрашивал решение
        m = data.get("member")
        if not m or not (kind == "expelled" or (kind == "rank" and data.get("rank") in ("member", "senior"))):
            return
        status = "failed" if kind == "expelled" else "done"
        for row in self.bot.db.all("SELECT * FROM mentorships WHERE newbie_id=? AND status='active'", m.id):
            self.bot.db.q("UPDATE mentorships SET status=?, closed_at=? WHERE id=?", status, now_iso(), row["id"])
            row = self.bot.db.one("SELECT * FROM mentorships WHERE id=?", row["id"])
            th = await self.get_thread(guild, row["thread_id"])
            if th:
                try:
                    if row["message_id"]:
                        await th.get_partial_message(row["message_id"]).edit(embed=self.card(guild, row), view=None)
                    await th.send("🎓 Испытательный срок завершён: новичок переведён в члены семьи." if status == "done"
                                  else "🚪 Новичок исключён из семьи. Ветка закрыта.")
                    await th.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        for row in self.bot.db.all("SELECT * FROM mentorships WHERE newbie_id=? AND status='active'", member.id):
            self.bot.db.q("UPDATE mentorships SET status='left', closed_at=? WHERE id=?", now_iso(), row["id"])
            th = await self.get_thread(member.guild, row["thread_id"])
            if th:
                try:
                    await th.send("🚪 Новичок покинул сервер. Ветка закрыта.")
                    await th.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass

    # ---------- кнопки ----------
    async def toggle(self, inter, mid, i):
        row = self.bot.db.one("SELECT * FROM mentorships WHERE id=?", mid)
        if not row or row["status"] != "active":
            return await inter.response.send_message("Испытательный срок уже завершён.", ephemeral=True)
        if not self.can_manage(inter.user, row):
            return await inter.response.send_message("Отмечать пункты может только наставник или руководство.",
                                                     ephemeral=True)
        checklist = json.loads(row["checklist"] or "[]")
        checklist += [False] * (len(config.MENTOR_CHECKLIST) - len(checklist))
        checklist[i] = not checklist[i]
        self.bot.db.q("UPDATE mentorships SET checklist=? WHERE id=?", json.dumps(checklist), mid)
        row = self.bot.db.one("SELECT * FROM mentorships WHERE id=?", mid)
        await inter.response.edit_message(embed=self.card(inter.guild, row), view=self.view(row))

    async def decide(self, inter, mid, action):
        bot, db = self.bot, self.bot.db
        row = db.one("SELECT * FROM mentorships WHERE id=?", mid)
        if not row or row["status"] != "active":
            return await inter.response.send_message("Решение уже принято.", ephemeral=True)
        if not self.can_manage(inter.user, row):
            return await inter.response.send_message("Решение принимает наставник или руководство.", ephemeral=True)
        newbie = inter.guild.get_member(row["newbie_id"])
        if action == "extend":
            due = parse_dt(row["due_at"]) + dt.timedelta(days=3)
            db.q("UPDATE mentorships SET due_at=?, asked=0 WHERE id=?", due.isoformat(), mid)
            await inter.response.edit_message(view=None)
            return await inter.followup.send(f"⏳ Испытательный срок продлён до {discord.utils.format_dt(due, 'D')} "
                                             f"({inter.user.mention}).")
        await inter.response.defer()
        if not newbie:
            db.q("UPDATE mentorships SET status='left', closed_at=? WHERE id=?", now_iso(), mid)
            return await inter.followup.send("Новичок уже не на сервере.")
        if action == "promote":
            await bot.set_rank(newbie, "member", inter.user, reason="Испытательный срок пройден")
            db.q("UPDATE mentorships SET status='done', closed_at=? WHERE id=?", now_iso(), mid)
            await safe_dm(newbie, f"🎓 Поздравляем! Испытательный срок пройден — теперь ты 👥 Член семьи {config.FAMILY}!")
            text = f"🎓 {newbie.mention} прошёл испытательный срок и стал 👥 Членом семьи! Решение: {inter.user.mention}"
            mentor = inter.guild.get_member(row["mentor_id"])
            bot.emit(inter.guild, "mentor_done", member=newbie, mentor=mentor)
        else:
            await bot.expel(newbie, inter.user, "Не прошёл испытательный срок")
            db.q("UPDATE mentorships SET status='failed', closed_at=? WHERE id=?", now_iso(), mid)
            text = f"❌ {newbie.mention} не прошёл испытательный срок. Решение: {inter.user.mention}"
        try:
            await inter.edit_original_response(view=None)
        except discord.HTTPException:
            pass
        await inter.followup.send(text)
        row = db.one("SELECT * FROM mentorships WHERE id=?", mid)
        th = await self.get_thread(inter.guild, row["thread_id"])
        if th and row["message_id"]:
            try:
                m = await th.fetch_message(row["message_id"])
                await m.edit(embed=self.card(inter.guild, row), view=None)
                await th.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass

    # ---------- срок вышел ----------
    @tasks.loop(minutes=30)
    @guarded
    async def due_loop(self):
        db = self.bot.db
        for row in db.all("SELECT * FROM mentorships WHERE status='active' AND asked=0 AND due_at<=?", now_iso()):
            for guild in self.bot.home_guilds:
                th = await self.get_thread(guild, row["thread_id"])
                if not th:
                    continue
                v = discord.ui.View(timeout=None)
                for a in DECISIONS:
                    v.add_item(MentorDecision(row["id"], a))
                hr = self.bot.role(guild, "hr_head")
                try:
                    await th.send(content=f"<@{row['mentor_id']}> {hr.mention if hr else ''}",
                                  embed=self.bot.embed("⏰ Испытательный срок закончился",
                                                       f"Пора решить судьбу <@{row['newbie_id']}>. "
                                                       "Решение принимает наставник или руководство.", config.GOLD, guild),
                                  view=v, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
                except discord.HTTPException:
                    continue
                db.q("UPDATE mentorships SET asked=1 WHERE id=?", row["id"])
                break

    @due_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    @group.command(name="назначить", description="Назначить (или сменить) наставника новичку")
    @app_commands.rename(newbie="новичок", mentor="наставник")
    @staff_only(config.MENTORS)
    async def assign_cmd(self, inter: discord.Interaction, newbie: discord.Member, mentor: discord.Member = None):
        db = self.bot.db
        row = db.one("SELECT * FROM mentorships WHERE newbie_id=? AND status='active'", newbie.id)
        if row:
            if not mentor:
                return await inter.response.send_message("У новичка уже есть наставник. Укажи нового, чтобы сменить.",
                                                         ephemeral=True)
            db.q("UPDATE mentorships SET mentor_id=? WHERE id=?", mentor.id, row["id"])
            th = await self.get_thread(inter.guild, row["thread_id"])
            if th:
                try:
                    await th.add_user(mentor)
                    await th.send(f"🔁 Новый наставник: {mentor.mention} (назначил {inter.user.mention})")
                except discord.HTTPException:
                    pass
            await self.bot.log(inter.guild, f"🔁 Наставник {newbie.mention} → {mentor.mention} ({inter.user.mention})",
                               "hrlog", 0x26A69A)
            return await inter.response.send_message("✅ Наставник сменён.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        r = await self.assign(inter.guild, newbie, mentor, by=inter.user)
        await inter.followup.send("✅ Наставник назначен." if r else "❌ Не нашлось наставника (нужна роль 🎓 Наставник).",
                                  ephemeral=True)

    @group.command(name="список", description="Новички на испытательном сроке")
    @staff_only(config.MENTORS + ["recruiter"])
    async def list_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM mentorships WHERE status='active' ORDER BY due_at")
        lines = []
        for r in rows:
            done = sum(1 for x in json.loads(r["checklist"] or "[]") if x)
            lines.append(f"<@{r['newbie_id']}> ← <@{r['mentor_id']}> · {done}/{len(config.MENTOR_CHECKLIST)} · "
                         f"решение {discord.utils.format_dt(parse_dt(r['due_at']), 'R')}")
        await inter.response.send_message(embed=self.bot.embed(f"🎓 На испытательном сроке: {len(rows)}",
                                                               "\n".join(lines)[:4000] or "Никого", config.GOLD,
                                                               inter.guild), ephemeral=True)

    @group.command(name="рейтинг", description="Лучшие наставники")
    async def rating_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("""SELECT mentor_id, SUM(status='done') ok, SUM(status='failed') bad,
                                  SUM(status='active') act FROM mentorships GROUP BY mentor_id
                                  ORDER BY ok DESC, act DESC LIMIT 15""")
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['mentor_id']}> — 🎓 выпустил: **{r['ok'] or 0}** · "
                 f"сейчас: {r['act'] or 0} · не прошли: {r['bad'] or 0}" for i, r in enumerate(rows)]
        await inter.response.send_message(embed=self.bot.embed("🏆 Рейтинг наставников", "\n".join(lines) or "Пока пусто",
                                                               config.GOLD, inter.guild))


async def setup(bot):
    bot.add_dynamic_items(MentorCheck, MentorStat, MentorDecision)
    await bot.add_cog(Mentorship(bot))
