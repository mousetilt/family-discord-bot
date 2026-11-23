# -*- coding: utf-8 -*-
"""Сборы семьи: карточка с кнопками «Иду / Возможно / Не смогу» и лимитом мест.
• events:discord — у сбора со временем ещё и событие сервера Discord (видно в шапке сервера, Discord сам напоминает
  отметившимся «Интересно»). «Интересно» в событии = «Иду» на карточке (d["via_event"] — кого так добавили).
• events:results — через 10 минут после начала: итоги (кто пришёл) ответом на карточку + кнопка «Премия пришедшим»
  (выплата из казны, только TREASURY).
• events:dm — за час до начала: ЛС отметившимся «Иду»/«Возможно» с кнопками «Приду» / «Не смогу» (меняют карточку).
• upcoming() — ближайшие сборы строками (речь «какие сборы на неделе», утренняя сводка)."""
import asyncio
import datetime as dt
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, staff_only
from core.parse import parse_when as _parse_when

log = logging.getLogger("family-bot.gather")
REMIND_BEFORE = 15 * 60   # за сколько до начала пинговать тех, кто отметился «Иду»
DM_BEFORE = 60 * 60       # за сколько до начала — ЛС с кнопками «Приду / Не смогу»
EVENT_LEN = 2 * 3600      # событие Discord «снаружи» обязано иметь конец — считаем, что сбор идёт 2 часа
CHECK_AFTER = 10 * 60     # через сколько после начала отмечать, кто реально пришёл в войс


def parse_when(text, base=None):
    """Время сбора по часовому поясу семьи → datetime (UTC) или None. Общий разборщик (core/parse.py):
    до 30 дней вперёд, начавшийся 5 минут назад сбор — ещё «сейчас»."""
    return _parse_when(text, base, max_days=30, grace_min=5)


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
        err = mark(d, inter.user.id, self.k)
        if err:
            return await inter.response.send_message(err, ephemeral=True)
        db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(d), inter.message.id)
        await inter.response.edit_message(embed=event_embed(inter.client, d, inter.guild))


def mark(d, uid, k):
    """Перенести человека в список k (yes/maybe/no) с учётом лимита мест. → None или текст отказа."""
    if k == "yes" and d.get("limit") and uid not in d["yes"] and len(d["yes"]) >= d["limit"]:
        return "😔 Мест больше нет. Отметься «Возможно»."
    for key in RSVP:
        if uid in d[key]:
            d[key].remove(uid)
    d[k].append(uid)
    return None


class DmRsvp(discord.ui.DynamicItem[discord.ui.Button], template=r"gdm:(?P<mid>\d+):(?P<k>yes|no)"):
    """Кнопки в ЛС за час до сбора: работают без сервера (сбор ищем по номеру сообщения карточки)."""

    def __init__(self, mid, k):
        label, emoji, style = ("Приду", "✅", discord.ButtonStyle.success) if k == "yes" else \
            ("Не смогу", "❌", discord.ButtonStyle.danger)
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"gdm:{mid}:{k}"))
        self.mid, self.k = int(mid), k

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["mid"], match["k"])

    async def callback(self, inter: discord.Interaction):
        bot = inter.client
        row = bot.db.one("SELECT guild_id, data FROM events WHERE message_id=?", self.mid)
        if not row:
            return await inter.response.send_message("Сбор уже не найден.", ephemeral=True)
        d = json.loads(row["data"])
        if d.get("checked") or (d.get("ts") and now().timestamp() > d["ts"] + CHECK_AFTER):
            return await inter.response.edit_message(content="Сбор уже прошёл.", view=None)
        err = mark(d, inter.user.id, self.k)
        if err:
            return await inter.response.send_message(err, ephemeral=True)
        bot.db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(d), self.mid)
        await inter.response.edit_message(
            content=(f"✅ Записал: идёшь на **{d['what']}** <t:{d['ts']}:R>." if self.k == "yes"
                     else f"❌ Понял, на **{d['what']}** не придёшь. Передумаешь — кнопка на карточке сбора."),
            view=None)
        g = bot.get_guild(row["guild_id"])
        ch = bot.ch(g, "gather") if g else None
        if ch:
            try:
                await ch.get_partial_message(self.mid).edit(embed=event_embed(bot, d, g))
            except discord.HTTPException:
                pass


class BonusButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gbonus:(?P<mid>\d+)"):
    """Под итогами сбора: премия всем пришедшим из казны."""
    def __init__(self, mid):
        super().__init__(discord.ui.Button(label="Премия пришедшим", emoji="💸", style=discord.ButtonStyle.success,
                                           custom_id=f"gbonus:{mid}"))
        self.mid = int(mid)

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["mid"])

    async def callback(self, inter: discord.Interaction):
        tr = inter.client.get_cog("Treasury")
        if not tr or not tr.allowed(inter.user):
            return await inter.response.send_message("⛔ Премию выдаёт казна (Глава, зам, совет, казначей).",
                                                     ephemeral=True)
        await inter.response.send_modal(BonusModal(self.mid))


class BonusModal(discord.ui.Modal, title="Премия за сбор"):
    amount = discord.ui.TextInput(label="Каждому, сколько (например 20к)", max_length=20)

    def __init__(self, mid):
        super().__init__()
        self.mid = mid

    async def on_submit(self, inter: discord.Interaction):
        from core.parse import parse_amount
        bot, g = inter.client, inter.guild
        row = bot.db.one("SELECT data FROM events WHERE message_id=? AND guild_id=?", self.mid, g.id)
        n = parse_amount(self.amount.value)
        if not row or not n:
            return await inter.response.send_message("❌ Не понял сумму или сбор не найден.", ephemeral=True)
        d = json.loads(row["data"])
        if d.get("bonus_paid"):
            return await inter.response.send_message("✅ Премию за этот сбор уже выдали.", ephemeral=True)
        pairs = [(g.get_member(u), n) for u in d.get("came") or [] if g.get_member(u)]
        await inter.response.defer(ephemeral=True)
        text = await bot.get_cog("Treasury").payout(g, inter.user, pairs, f"премия за сбор: {d['what']}"[:120])
        if text.startswith(("💸", "✅")):
            fresh = json.loads(bot.db.one("SELECT data FROM events WHERE message_id=?", self.mid)["data"])
            fresh["bonus_paid"] = inter.user.id
            bot.db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(fresh), self.mid)
        await inter.followup.send(text[:1900], ephemeral=True)


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
                if not d.get("dm_reminded") and t >= ts - DM_BEFORE:
                    d["dm_reminded"], changed = True, True
                    if t < ts - REMIND_BEFORE and ts - d.get("made", 0) > DM_BEFORE + 120 and \
                            self.bot.feature(g, "events:dm"):
                        await self.dm_remind(g, row["message_id"], d, msg)
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
                            if self.bot.feature(g, "events:results"):
                                await self.results(g, msg, row["message_id"], d)
                    else:
                        d["checked"] = "skipped"
            if changed:
                # кнопки могли поменять списки, пока мы работали — обновляем только свои поля
                fresh = json.loads(db.one("SELECT data FROM events WHERE message_id=?", row["message_id"])["data"])
                for k in ("reminded", "dm_reminded", "checked", "came"):
                    if k in d:
                        fresh[k] = d[k]
                db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(fresh), row["message_id"])

    async def dm_remind(self, g, mid, d, msg):
        """За час: ЛС тем, кто отметился «Иду» или «Возможно», — с кнопками «Приду» / «Не смогу»."""
        from core.botcore import safe_dm
        v = discord.ui.View(timeout=None)
        v.add_item(DmRsvp(mid, "yes"))
        v.add_item(DmRsvp(mid, "no"))
        link = f"\n{msg.jump_url}" if msg else ""
        for uid in list(dict.fromkeys((d.get("yes") or []) + (d.get("maybe") or [])))[:60]:
            m = g.get_member(uid)
            if not m or m.bot:
                continue
            await safe_dm(m, f"⏰ Через час сбор семьи {config.FAMILY}: **{d['what']}** — <t:{d['ts']}:t>"
                             f"{(', ' + d['where']) if d.get('where') else ''}. Будешь?{link}", view=v)
            await asyncio.sleep(0.5)

    def upcoming(self, guild, days=7):
        """Ближайшие сборы строками (для речи и сводки)."""
        t0, out = now().timestamp(), []
        for row in self.bot.db.all("SELECT message_id, data FROM events WHERE guild_id=?", guild.id):
            try:
                d = json.loads(row["data"])
            except ValueError:
                continue
            ts = d.get("ts")
            if isinstance(ts, int) and t0 - CHECK_AFTER <= ts <= t0 + days * 86400:
                lim = f"/{d['limit']}" if d.get("limit") else ""
                out.append((ts, f"📣 <t:{ts}:f> — **{d['what']}**{(' · ' + d['where']) if d.get('where') else ''}"
                                f" · идут {len(d.get('yes') or [])}{lim}, возможно {len(d.get('maybe') or [])}"))
        return [s for _, s in sorted(out)]

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def results(self, g, msg, mid, d):
        came, yes = d.get("came") or [], d.get("yes") or []
        if not yes:
            return
        lost = [u for u in yes if u not in came]
        text = (f"📋 **Итоги сбора «{d['what']}»**: пришли {len(came)} из {len(yes)} отметившихся."
                + ("\n🙌 Спасибо: " + " ".join(f"<@{u}>" for u in came[:40]) if came else "")
                + ("\n🙈 Обещали и не пришли: " + " ".join(f"<@{u}>" for u in lost[:40]) if lost else ""))
        v = discord.ui.View(timeout=None)
        if came and self.bot.feature(g, "treasury"):
            v.add_item(BonusButton(mid))
        try:
            await msg.reply(text[:1900], view=v, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---------- событие сервера Discord ----------
    async def make_event(self, guild, d, start, where):
        if not self.bot.feature(guild, "events:discord") or not guild.me.guild_permissions.manage_events:
            return None
        try:
            ev = await guild.create_scheduled_event(
                name=f"📣 {d['what']}"[:100], start_time=start, end_time=start + dt.timedelta(seconds=EVENT_LEN),
                entity_type=discord.EntityType.external, location=(where or "Сбор семьи")[:100],
                privacy_level=discord.PrivacyLevel.guild_only,
                description="Сбор семьи. Отметься «Интересно» — бот запишет тебя в «Иду».")
            return ev.id
        except (discord.HTTPException, ValueError, TypeError) as e:
            log.info("Событие сервера для сбора не создано: %s", e)
            return None

    def event_row(self, guild_id, event_id):
        for row in self.bot.db.all("SELECT message_id, data FROM events WHERE guild_id=?", guild_id):
            try:
                d = json.loads(row["data"])
            except ValueError:
                continue
            if d.get("event_id") == event_id:
                return row["message_id"], d
        return None, None

    async def sync_interest(self, event, user, add):
        g = event.guild
        if g is None or not self.bot.is_family(g):
            return
        mid, d = self.event_row(g.id, event.id)
        if not mid:
            return
        uid = user.id
        via = d.setdefault("via_event", [])
        if add:
            if any(uid in d[k] for k in RSVP):
                return  # уже отметился кнопкой — не трогаем
            if d.get("limit") and len(d["yes"]) >= d["limit"]:
                d["maybe"].append(uid)
            else:
                d["yes"].append(uid)
            via.append(uid)
        else:
            if uid not in via:
                return
            via.remove(uid)
            for k in RSVP:
                if uid in d[k]:
                    d[k].remove(uid)
        self.bot.db.q("UPDATE events SET data=? WHERE message_id=?", json.dumps(d), mid)
        ch = self.bot.ch(g, "gather")
        if ch:
            try:
                await ch.get_partial_message(mid).edit(embed=event_embed(self.bot, d, g))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(self, event, user):
        await self.sync_interest(event, user, True)

    @commands.Cog.listener()
    async def on_scheduled_event_user_remove(self, event, user):
        await self.sync_interest(event, user, False)

    @app_commands.default_permissions(create_events=True)
    @app_commands.command(name="сбор", description="Объявить сбор семьи")
    @app_commands.rename(what="что", when="когда", where="где", limit="мест", remind="напомнить_через")
    @app_commands.describe(what="Что делаем", when="По Москве: «сегодня 20:00», «завтра 19:30», «суббота 21:00», «через 40 минут»", where="Место сбора",
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
            if start > now() + dt.timedelta(minutes=2):  # событие Discord нельзя создать «в прошлом»
                eid = await self.make_event(guild, d, start, where)
                if eid:
                    d["event_id"] = eid
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
            auto = self.bot.get_cog("Automation")
            if remind and auto:  # в базу — переживёт перезапуск бота
                auto.remind_later(guild, "gather", now().timestamp() + remind * 60,
                                  text=f"Сбор семьи начинается! {what}.{place} Все, кто отметился, — на выход!")
            elif remind:
                async def later():
                    await asyncio.sleep(remind * 60)
                    targets = [c.id for c in music.voice_targets(guild)]
                    await music.announce(guild, targets, siren=False,
                                         text=f"Сбор семьи начинается! {what}.{place} Все, кто отметился, — на выход!")
                asyncio.create_task(later())
        return msg


async def setup(bot):
    bot.add_dynamic_items(RsvpButton, BonusButton, DmRsvp)
    await bot.add_cog(Gatherings(bot))
