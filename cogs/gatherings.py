# -*- coding: utf-8 -*-
"""Сборы семьи: карточка с кнопками «Иду / Возможно / Не смогу» и лимитом мест."""
import asyncio
import datetime as dt
import json
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, staff_only

REMIND_BEFORE = 15 * 60   # за сколько до начала пинговать тех, кто отметился «Иду»
CHECK_AFTER = 10 * 60     # через сколько после начала отмечать, кто реально пришёл в войс


def parse_when(text, base=None):
    """«сегодня 20:00», «завтра в 19.30», «15.11 21:00», «через 40 минут» → время начала (UTC) или None.
    Время считаем по часовому поясу семьи (config.GATHER_UTC_OFFSET, по умолчанию Москва)."""
    tz = dt.timezone(dt.timedelta(hours=config.GATHER_UTC_OFFSET))
    base = (base or now()).astimezone(tz)
    s = (text or "").lower().replace("ё", "е")
    m = re.search(r"через\s+(\d{1,3})?\s*(мин|час)", s)
    if m:
        n = int(m.group(1) or 1)
        return (base + (dt.timedelta(minutes=n) if m.group(2) == "мин" else dt.timedelta(hours=n))).astimezone(dt.timezone.utc)
    t = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", s)
    date = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)", s if t else "")
    if not t:
        t = re.search(r"(?<!\d)([01]?\d|2[0-3])\.([0-5]\d)(?!\d)", s)
    if not t:
        return None
    when = base.replace(hour=int(t.group(1)), minute=int(t.group(2)), second=0, microsecond=0)
    try:
        if date:
            y = int(date.group(3) or base.year)
            when = when.replace(year=y + 2000 if y < 100 else y, month=int(date.group(2)), day=int(date.group(1)))
        elif "послезавтра" in s:
            when += dt.timedelta(days=2)
        elif "завтра" in s:
            when += dt.timedelta(days=1)
        elif when < base - dt.timedelta(minutes=5) and "сегодня" not in s:
            when += dt.timedelta(days=1)  # «в 02:00» поздним вечером — это уже завтра
    except ValueError:
        return None
    if when < base - dt.timedelta(minutes=5) or when > base + dt.timedelta(days=30):
        return None
    return when.astimezone(dt.timezone.utc)

RSVP = {"yes": ("Иду", "✅", discord.ButtonStyle.success),
        "maybe": ("Возможно", "🤔", discord.ButtonStyle.secondary),
        "no": ("Не смогу", "❌", discord.ButtonStyle.danger)}


def event_embed(bot, d, guild):
    ts = d.get("ts")
    exact = f" — <t:{ts}:F> (<t:{ts}:R>)" if ts else ""
    e = bot.embed(f"📣 СБОР: {d['what']}",
                  f"🕐 **Когда:** {d['when']}{exact}\n📍 **Где:** {d.get('where') or 'уточнится'}\n"
                  f"👤 **Организатор:** <@{d['author']}>", config.COLOR, guild)
    lim = d.get("limit") or 0
    for k, (label, emoji, _) in RSVP.items():
        users = d[k]
        title = f"{emoji} {label} ({len(users)}{'/' + str(lim) if k == 'yes' and lim else ''})"
        e.add_field(name=title, value="\n".join(f"<@{u}>" for u in users)[:1024] or "—", inline=True)
    if d.get("checked") is True and d.get("yes"):
        came = set(d.get("came") or [])
        e.add_field(name=f"📋 Пришли: {len(came)} из {len(d['yes'])}",
                    value=" ".join(f"{'✅' if u in came else '❌'}<@{u}>" for u in d["yes"])[:1024], inline=False)
    return e


class RsvpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rsvp:(?P<k>yes|maybe|no)"):
    def __init__(self, k):
        label, emoji, style = RSVP[k]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"rsvp:{k}"))
        self.k = k

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["k"])

    async def callback(self, inter: discord.Interaction):
        db = inter.client.db
        row = db.one("SELECT data FROM events WHERE message_id=?", inter.message.id)
        if not row:
            return await inter.response.send_message("Сбор не найден.", ephemeral=True)
        d = json.loads(row["data"])
        uid = inter.user.id
        if self.k == "yes" and d.get("limit") and uid not in d["yes"] and len(d["yes"]) >= d["limit"]:
            return await inter.response.send_message("😔 Мест больше нет. Отметься «Возможно».", ephemeral=True)
        for k in RSVP:
            if uid in d[k]:
                d[k].remove(uid)
        d[self.k].append(uid)
        db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(d), inter.message.id)
        await inter.response.edit_message(embed=event_embed(inter.client, d, inter.guild))


class Gatherings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def attendance(self, member):
        """(пришёл, отмечался «Иду») по сборам его семьи, где бот проверял явку."""
        uid = member.id
        came = total = 0
        for row in self.bot.db.all("SELECT data FROM events WHERE guild_id=?", member.guild.id):
            try:
                d = json.loads(row["data"])
            except ValueError:
                continue
            if d.get("checked") is True and uid in d.get("yes", []):
                total += 1
                came += uid in (d.get("came") or [])
        return came, total

    @tasks.loop(minutes=1)
    @guarded
    async def tick(self):
        """Напоминание за 15 минут тем, кто идёт, и отметка явки через 10 минут после начала."""
        db, t = self.bot.db, now().timestamp()
        border = (now() - dt.timedelta(days=31)).isoformat()
        for row in db.all("SELECT message_id, guild_id, data FROM events WHERE created_at>?", border):
            try:
                d = json.loads(row["data"])
            except ValueError:
                continue
            ts = d.get("ts")
            if not ts or d.get("checked"):
                continue
            changed = False
            for g in filter(None, [self.bot.get_guild(row["guild_id"])]):
                ch = self.bot.ch(g, "gather")
                msg = ch.get_partial_message(row["message_id"]) if ch else None
                if not d.get("reminded") and t >= ts - REMIND_BEFORE:
                    d["reminded"], changed = True, True
                    if t < ts and d["yes"] and msg and ts - d.get("made", 0) > REMIND_BEFORE + 120:
                        try:
                            await msg.reply(f"⏰ Сбор **{d['what']}** начнётся <t:{ts}:R>. Ждём: " +
                                            " ".join(f"<@{u}>" for u in d["yes"][:40]),
                                            allowed_mentions=discord.AllowedMentions(users=True))
                        except discord.HTTPException:
                            pass
                if t >= ts + CHECK_AFTER:
                    d["checked"], changed = True, True
                    if t < ts + 3 * 3600:  # бот был выключен дольше — явку уже не восстановить, не выдумываем
                        afk = self.bot.ch(g, "afk")
                        d["came"] = [u for u in d["yes"] if (m := g.get_member(u)) and m.voice and m.voice.channel
                                     and not (afk and m.voice.channel.id == afk.id)]
                        if msg:
                            try:
                                await msg.edit(embed=event_embed(self.bot, d, g))
                            except discord.HTTPException:
                                pass
                    else:
                        d["checked"] = "skipped"
            if changed:
                # кнопки могли поменять списки, пока мы работали — обновляем только свои поля
                fresh = json.loads(db.one("SELECT data FROM events WHERE message_id=?", row["message_id"])["data"])
                for k in ("reminded", "checked", "came"):
                    if k in d:
                        fresh[k] = d[k]
                db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(fresh), row["message_id"])

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.default_permissions(create_events=True)
    @app_commands.command(name="сбор", description="Объявить сбор семьи")
    @app_commands.rename(what="что", when="когда", where="где", limit="мест", remind="напомнить_через")
    @app_commands.describe(what="Что делаем", when="Время по Москве: «сегодня 20:00», «завтра 19:30», «через 40 минут»", where="Место сбора",
                           limit="Сколько людей нужно (0 — без лимита)",
                           remind="Через сколько минут бот голосом позовёт всех на сбор (0 — только сразу)")
    @staff_only(config.ORGANIZERS)
    async def gather(self, inter: discord.Interaction, what: str, when: str, where: str = None,
                     limit: app_commands.Range[int, 0, 100] = 0, remind: app_commands.Range[int, 0, 240] = 0):
        await inter.response.defer(ephemeral=True)
        msg = await self.create(inter.guild, inter.user, what, when, where, limit, remind, inter.channel)
        await inter.followup.send(f"✅ Сбор создан: {msg.jump_url}" +
                                  (f"\n🔊 Через {remind} мин бот позовёт всех голосом." if remind else ""),
                                  ephemeral=True)

    async def create(self, guild, author, what, when, where=None, limit=0, remind=0, fallback=None):
        """Создать сбор (команда /сбор и помощник «Швейцар»)."""
        c = self.bot.ch(guild, "gather") or fallback
        d = {"what": what, "when": when, "where": where, "limit": limit, "author": author.id,
             "yes": [author.id], "maybe": [], "no": []}
        start = parse_when(when)
        if start:
            d["ts"], d["made"] = int(start.timestamp()), int(now().timestamp())
        v = discord.ui.View(timeout=None)
        for k in RSVP:
            v.add_item(RsvpButton(k))
        ping = self.bot.role(guild, "n_gather")
        msg = await c.send(content=ping.mention if ping else None, embed=event_embed(self.bot, d, guild), view=v,
                           allowed_mentions=discord.AllowedMentions(roles=True))
        self.bot.db.q("INSERT INTO events(message_id, guild_id, data, created_at) VALUES(?,?,?,?)", msg.id, guild.id,
                      json.dumps(d), now_iso())
        music = self.bot.get_cog("Music")
        if music:
            place = f" Место: {where}." if where else ""
            asyncio.create_task(music.announce(guild, [c.id for c in music.voice_targets(guild)],
                                               text=f"Внимание, семья! Объявлен сбор: {what}. Время: {when}.{place} "
                                                    "Отметьтесь в канале сборов."))
            if remind:
                async def later():
                    await asyncio.sleep(remind * 60)
                    targets = [c.id for c in music.voice_targets(guild)]
                    await music.announce(guild, targets, siren=False,
                                         text=f"Сбор семьи начинается! {what}.{place} Все, кто отметился, — на выход!")
                asyncio.create_task(later())
        return msg


async def setup(bot):
    bot.add_dynamic_items(RsvpButton)
    await bot.add_cog(Gatherings(bot))
