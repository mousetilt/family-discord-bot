# -*- coding: utf-8 -*-
"""Собеседования.
• В «Собеседование 1/2» сами заходят только рекрутеры, отдел кадров и руководство — кандидатов переносит бот.
• Кандидат (не семья и не друг семьи) заходит в «⏳ Ожидание собеседования»:
    – его рекрутер (взял заявку или вызвал на собеседование) сидит в комнате собеседования → перенос к нему;
    – заявку ещё никто не взял → перенос к первому свободному рекрутеру, заявка закрепляется за ним;
    – иначе бот здоровается голосом и зовёт рекрутера.
• Рекрутер заходит в комнату собеседования → бот подтягивает к нему его кандидата (или первого свободного) из ожидания.
• Своих (семья, друзья семьи) бот в ожидании не трогает. Гостю без заявки подсказывает подать заявку."""
import asyncio
import logging
import time

import discord
from discord.ext import commands

from core import config
from core.botcore import now_iso

log = logging.getLogger("family-bot.interviews")
OPEN = ("new", "taken", "interview")
GREET_COOLDOWN = 600


class Interviews(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.greeted = {}
        self.lock = asyncio.Lock()
        self.queued = {}  # (guild_id, user_id) → когда кандидат зашёл в ожидание (для номера в очереди)

    def position(self, member):
        """Сколько кандидатов с заявкой ждут дольше этого человека."""
        waiting = self.bot.ch(member.guild, "waiting")
        if not waiting:
            return 0
        mine = self.queued.get((member.guild.id, member.id), time.monotonic())
        return sum(1 for m in waiting.members if m.id != member.id and self.candidate(m) and self.app(m)
                   and self.queued.get((member.guild.id, m.id), 0) < mine)

    # ---------- помощники ----------
    def rooms(self, guild):
        return [c for c in (self.bot.ch(guild, "interview1"), self.bot.ch(guild, "interview2")) if c]

    def staff(self, m):
        return isinstance(m, discord.Member) and not m.bot and self.bot.has(m, config.HR)

    def candidate(self, m):
        return isinstance(m, discord.Member) and not m.bot and not self.bot.has(m, config.FAMILY_PLUS) \
            and not self.staff(m)

    def app(self, member):
        return self.bot.db.one(f"SELECT * FROM applications WHERE guild_id=? AND user_id=? AND status IN {OPEN} "
                               "ORDER BY id DESC", member.guild.id, member.id)

    @staticmethod
    def has_space(room, extra=1):
        return not room.user_limit or len(room.members) + extra <= room.user_limit

    def free_room(self, guild):
        """Комната, где сидит рекрутер и нет кандидата."""
        for room in self.rooms(guild):
            people = [m for m in room.members if not m.bot]
            st = [m for m in people if self.staff(m)]
            if st and len(st) == len(people) and self.has_space(room):
                return room, st[0]
        return None, None

    async def move(self, cand, room, recruiter, app):
        try:
            await cand.move_to(room, reason=f"Собеседование у {recruiter.display_name}")
        except discord.HTTPException as e:
            log.warning("Перенос на собеседование: %s", e)
            return False
        db = self.bot.db
        rec = self.bot.get_cog("Recruit")
        if rec and app:  # перенос на собеседование = обзвон в статистику рекрутёра
            rec.record(cand.guild, app["id"], recruiter.id, "interview")
        if app and not app["reviewer_id"]:
            db.q("UPDATE applications SET reviewer_id=?, status='interview' WHERE id=?", recruiter.id, app["id"])
        elif app and app["status"] == "new":
            db.q("UPDATE applications SET status='interview' WHERE id=?", app["id"])
        hr = self.bot.ch(cand.guild, "hr_chat") or self.bot.ch(cand.guild, "review")
        num = f" · заявка №{app['id']}" if app else ""
        if hr:
            try:
                await hr.send(f"🔀 {cand.mention} перенесён на собеседование к {recruiter.mention} в {room.mention}{num}.",
                              allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        return True

    async def greet(self, member, app, reviewer):
        key = (member.guild.id, member.id)
        t = time.monotonic()
        if t - self.greeted.get(key, -10 ** 9) < GREET_COOLDOWN:
            return
        self.greeted[key] = t
        g = member.guild
        waiting = self.bot.ch(g, "waiting")
        name = member.display_name.split("[")[0].replace("_", " ").strip()
        if not app:
            text = (f"Здравствуйте, {name}! Чтобы пройти собеседование в семью {config.FAMILY_GEN}, "
                    "сначала подайте заявку в канале «подать заявку». После этого вас позовут.")
        else:
            hr = self.bot.ch(g, "hr_chat") or self.bot.ch(g, "review")
            who = reviewer.mention if reviewer else (self.bot.role(g, "recruiter").mention
                                                     if self.bot.role(g, "recruiter") else "")
            if hr:
                try:
                    await hr.send(f"🎤 {who} {member.mention} ждёт собеседования в {waiting.mention} "
                                  f"(заявка №{app['id']}). Зайди в свободную комнату собеседования — бот перенесёт "
                                  "кандидата сам.", allowed_mentions=discord.AllowedMentions(roles=True, users=True))
                except discord.HTTPException:
                    pass
            ahead = self.position(member)
            text = (f"Здравствуйте, {name}! Добро пожаловать к семье {config.FAMILY_GEN}. "
                    "Рекрутер уже получил уведомление. Как только он освободится, я перенесу вас на собеседование."
                    + (f" Перед вами в очереди: {ahead}." if ahead else " Вы первый в очереди."))
        text = self.bot.brand(g, text)
        music = self.bot.get_cog("Music")
        if music and waiting:
            await asyncio.sleep(1.5)
            if member.voice and member.voice.channel and member.voice.channel.id == waiting.id:
                await music.announce(g, [waiting.id], text=text)

    # ---------- события ----------
    async def on_candidate_waiting(self, member):
        g = member.guild
        app = self.app(member)
        if not app:
            asyncio.create_task(self.greet(member, None, None))  # голос — вне блокировки, чтобы не тормозить переносы
            return
        reviewer = g.get_member(app["reviewer_id"]) if app["reviewer_id"] else None
        rooms = self.rooms(g)
        if reviewer and reviewer.voice and reviewer.voice.channel in rooms and self.has_space(reviewer.voice.channel):
            if await self.move(member, reviewer.voice.channel, reviewer, app):
                return
        if not reviewer:
            room, rec = self.free_room(g)
            if room and await self.move(member, room, rec, app):
                return
        asyncio.create_task(self.greet(member, app, reviewer))

    async def on_staff_room(self, staff, room):
        waiting = self.bot.ch(staff.guild, "waiting")
        if not waiting:
            return
        if any(self.candidate(m) for m in room.members) or not self.has_space(room):
            return  # в комнате уже идёт собеседование
        queue = [m for m in waiting.members if self.candidate(m)]
        mine, free = None, None
        for m in queue:
            a = self.app(m)
            if not a:
                continue
            if a["reviewer_id"] == staff.id:
                mine = (m, a)
                break
            if not a["reviewer_id"] and not free:
                free = (m, a)
        pick = mine or free
        if pick:
            await self.move(pick[0], room, staff, pick[1])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel or not after.channel:
            return
        g = member.guild
        waiting = self.bot.ch(g, "waiting")
        if waiting and before.channel and before.channel.id == waiting.id:
            self.queued.pop((g.id, member.id), None)
        async with self.lock:
            if waiting and after.channel.id == waiting.id and self.candidate(member):
                self.queued.setdefault((g.id, member.id), time.monotonic())
                await self.on_candidate_waiting(member)
            elif after.channel in self.rooms(g) and self.staff(member):
                await self.on_staff_room(member, after.channel)


async def setup(bot):
    await bot.add_cog(Interviews(bot))
