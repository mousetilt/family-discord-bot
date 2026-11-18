# -*- coding: utf-8 -*-
"""Дипломатия: переговорные комнаты с другими семьями, доска отношений, договоры с архивом."""
import asyncio
import datetime as dt
import io
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, parse_dt, staff_only

log = logging.getLogger("family-bot.diplo")
STATUS_CHOICES = [app_commands.Choice(name=f"{e} {n}", value=k) for k, (e, n, _) in config.RELATIONS.items()]


def slug(s):
    s = re.sub(r"[^\w\- ]", "", s.lower()).strip().replace(" ", "-")
    return s[:60] or "семья"


NEGO_GAP = 10 * 60  # один человек — не чаще раза в 10 минут
NEGO_MAX = 5        # открытых переговорных на сервер (дипломатов ограничение не касается)


class DiploPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Запросить переговоры", emoji="🕊", style=discord.ButtonStyle.primary,
                       custom_id="diplo:request")
    async def request(self, inter: discord.Interaction, _):
        db = inter.client.db
        for r in db.all("SELECT channel_id FROM negotiations WHERE guild_id=? AND requester_id=? AND status='open'",
                        inter.guild.id, inter.user.id):
            if inter.guild.get_channel(r["channel_id"]):
                return await inter.response.send_message(f"У тебя уже открыты переговоры: <#{r['channel_id']}>",
                                                         ephemeral=True)
            # канал удалили вручную — запись больше не должна мешать
            db.q("UPDATE negotiations SET status='closed' WHERE channel_id=?", r["channel_id"])
        # защита от спама переговорными: не чаще раза в NEGO_GAP на человека и не больше NEGO_MAX открытых на сервер
        last = db.one("SELECT created_at FROM negotiations WHERE guild_id=? AND requester_id=? ORDER BY rowid DESC LIMIT 1",
                      inter.guild.id, inter.user.id)
        try:
            ago = (now() - parse_dt(last["created_at"])).total_seconds() if last and last["created_at"] else 10 ** 9
        except (TypeError, ValueError):
            ago = 10 ** 9
        if ago < NEGO_GAP and not inter.client.has(inter.user, config.DIPLO):
            return await inter.response.send_message(f"⏳ Новые переговоры можно запросить через "
                                                     f"{int((NEGO_GAP - ago) // 60) + 1} мин.", ephemeral=True)
        opened = db.one("SELECT COUNT(*) n FROM negotiations WHERE guild_id=? AND status='open'", inter.guild.id)["n"]
        if opened >= NEGO_MAX and not inter.client.has(inter.user, config.DIPLO):
            return await inter.response.send_message("⏳ Сейчас открыто много переговоров — дипломаты заняты, "
                                                     "попробуй чуть позже.", ephemeral=True)
        try:
            await inter.response.send_modal(DiploRequestModal())
        except discord.NotFound:
            pass  # кнопку нажали со старого сообщения — взаимодействие уже протухло


class DiploRequestModal(discord.ui.Modal, title="Запрос переговоров"):
    family = discord.ui.TextInput(label="Ваша семья / организация", max_length=60)
    who = discord.ui.TextInput(label="Ваш ник и должность", max_length=80, placeholder="Ivan_Ivanov, глава семьи")
    purpose = discord.ui.TextInput(label="Цель переговоров", style=discord.TextStyle.paragraph, max_length=600)

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Diplomacy").open_room(inter, self.family.value, self.who.value, self.purpose.value)


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"diplo:close:(?P<cid>\d+)"):
    def __init__(self, cid):
        super().__init__(discord.ui.Button(label="Завершить переговоры", emoji="🔒", style=discord.ButtonStyle.danger,
                                           custom_id=f"diplo:close:{cid}"))
        self.cid = cid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["cid"]))

    async def callback(self, inter: discord.Interaction):
        if not inter.client.has(inter.user, config.DIPLO):
            return await inter.response.send_message("Завершить переговоры может дипломат или руководство.",
                                                     ephemeral=True)
        await inter.response.send_modal(CloseModal(self.cid))


class CloseModal(discord.ui.Modal, title="Итог переговоров"):
    outcome = discord.ui.TextInput(label="Чем закончились переговоры?", style=discord.TextStyle.paragraph,
                                   max_length=800)

    def __init__(self, cid):
        super().__init__()
        self.cid = cid

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Diplomacy").close_room(inter, self.cid, self.outcome.value)


class TreatyModal(discord.ui.Modal, title="Новый договор"):
    terms = discord.ui.TextInput(label="Условия договора", style=discord.TextStyle.paragraph, max_length=3000)

    def __init__(self, family, title_, days):
        super().__init__()
        self.family, self.title_, self.days = family, title_, days

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Diplomacy").create_treaty(inter, self.family, self.title_, self.terms.value,
                                                              self.days)


class Diplomacy(commands.Cog):
    rel = app_commands.Group(name="отношения", description="Отношения с другими семьями", default_permissions=discord.Permissions(move_members=True))
    treaty = app_commands.Group(name="договор", description="Договоры с другими семьями", default_permissions=discord.Permissions(move_members=True))
    nego = app_commands.Group(name="переговоры", description="Переговорные комнаты", default_permissions=discord.Permissions(move_members=True))

    def __init__(self, bot):
        self.bot = bot
        self.expire.start()

    def cog_unload(self):
        self.expire.cancel()

    async def post_panels(self, guild, repost):
        bot = self.bot

        async def make():
            e = bot.embed("🕊 Переговоры с семьёй " + config.FAMILY,
                          "Представляете другую семью или организацию и хотите договориться — о союзе, перемирии, "
                          "сделке или спорной ситуации?\n\nНажмите кнопку ниже: бот создаст закрытый канал, где вас "
                          "встретят наши дипломаты. Всё, что обсуждается там, остаётся между сторонами.",
                          0x00ACC1, guild)
            return {"embed": e, "view": DiploPanel()}
        await bot.panel_once(guild, "diplo", "diplo_request", repost, make, version=1)
        await bot.panel_once(guild, "relations_board", "relations", repost,
                             {"embed": bot.embed("🌐 Отношения", "Загружается...", 0x00ACC1, guild)}, version=1)
        await self.update_board(guild)

    # ---------- доска отношений ----------
    async def update_board(self, guild):
        bot = self.bot
        c, mid = bot.ch(guild, "relations"), bot.db.gget(guild, "panel:relations_board")
        if not c or not mid:
            return
        rows = bot.db.all("SELECT * FROM relations WHERE guild_id=? ORDER BY family", guild.id)
        e = bot.embed(f"🌐 Отношения семьи {config.FAMILY_GEN}", None, 0x00ACC1, guild)
        for key, (emoji, name, _) in config.RELATIONS.items():
            fams = [r for r in rows if r["status"] == key]
            if fams:
                e.add_field(name=f"{emoji} {name} ({len(fams)})",
                            value="\n".join(f"**{r['family']}**{' — ' + r['note'] if r['note'] else ''}"
                                            for r in fams)[:1024], inline=False)
        allies = self.alliance_families(guild)
        if allies:  # семьи альянса — союзники автоматически, руками заносить не нужно
            e.add_field(name=f"🏰 Альянс ({len(allies)})", value=", ".join(f"**{f}**" for f in allies)[:1024],
                        inline=False)
        treaties = bot.db.all("SELECT * FROM treaties WHERE guild_id=? AND active=1 ORDER BY id DESC LIMIT 10", guild.id)
        if treaties:
            e.add_field(name="📜 Действующие договоры",
                        value="\n".join(f"`№{t['id']}` {t['title']} — **{t['family']}**"
                                        f"{' до ' + discord.utils.format_dt(parse_dt(t['until']), 'd') if t['until'] else ''}"
                                        for t in treaties)[:1024], inline=False)
        if not e.fields:
            e.description = "Пока ни с кем нет официальных отношений. Команда: `/отношения установить`"
        e.timestamp = now()
        try:
            m = await c.fetch_message(int(mid))
            await m.edit(embed=e)
        except discord.HTTPException:
            pass

    def alliance_families(self, guild):
        """Семьи альянса (кроме своей), если наша семья в нём."""
        ally = self.bot.get_cog("Alliance")
        if not ally or not ally.fam(guild.id) or ally.fam(guild.id)["status"] != "active":
            return []
        return [r["family"] for r in ally.active() if r["guild_id"] != guild.id]

    async def family_auto(self, inter: discord.Interaction, current: str):
        """Подсказки названий семей: доска отношений + альянс — меньше опечаток и дублей."""
        names = [r["family"] for r in self.bot.db.all("SELECT family FROM relations WHERE guild_id=? ORDER BY family",
                                                       inter.guild.id)]
        names += [f for f in self.alliance_families(inter.guild) if f not in names]
        cur = (current or "").lower()
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names if cur in n.lower()][:25]

    @rel.command(name="установить", description="Задать отношения с семьёй (союз, пакт, нейтралитет, война…)")
    @app_commands.autocomplete(family=family_auto)
    @app_commands.rename(family="семья", status="статус", note="заметка")
    @app_commands.choices(status=STATUS_CHOICES)
    @staff_only(config.DIPLO)
    async def rel_set(self, inter: discord.Interaction, family: str, status: app_commands.Choice[str], note: str = None):
        db = self.bot.db
        gid = inter.guild.id
        old = db.one("SELECT status FROM relations WHERE guild_id=? AND lower(family)=lower(?)", gid, family)
        db.q("DELETE FROM relations WHERE guild_id=? AND lower(family)=lower(?)", gid, family)
        db.q("INSERT INTO relations(guild_id, family, status, note, updated_by, updated_at) VALUES(?,?,?,?,?,?)",
             gid, family, status.value, note, inter.user.id, now_iso())
        await self.update_board(inter.guild)
        emoji, name, color = config.RELATIONS[status.value]
        await self.bot.log(inter.guild, f"{emoji} Отношения с **{family}**: {name} ({inter.user.mention})"
                                        f"{' — ' + note if note else ''}", "diplo_archive", color)
        self.bot.emit(inter.guild, "relation", family=family, status=status.value,
                      previous=old["status"] if old else None, by=inter.user)
        await inter.response.send_message(f"✅ {emoji} {family}: {name}", ephemeral=True)

    @rel.command(name="удалить", description="Убрать семью с доски отношений")
    @app_commands.autocomplete(family=family_auto)
    @app_commands.rename(family="семья")
    @staff_only(config.DIPLO)
    async def rel_del(self, inter: discord.Interaction, family: str):
        self.bot.db.q("DELETE FROM relations WHERE guild_id=? AND lower(family)=lower(?)", inter.guild.id, family)
        await self.update_board(inter.guild)
        await inter.response.send_message("🗑 Убрано.", ephemeral=True)

    # ---------- переговорные ----------
    async def open_room(self, inter, family, who, purpose):
        bot, g = self.bot, inter.guild
        await inter.response.defer(ephemeral=True)
        cat = bot.ch(g, "diplo_cat")
        PO = discord.PermissionOverwrite
        ow = {g.default_role: PO(view_channel=False),
              inter.user: PO(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
              g.me: PO(view_channel=True, send_messages=True, manage_channels=True, manage_permissions=True)}
        for k in config.DIPLO:
            r = bot.role(g, k)
            if r:
                ow[r] = PO(view_channel=True, send_messages=True, read_message_history=True)
        ch = await g.create_text_channel(f"🤝・{slug(family)}", category=cat, overwrites=ow,
                                         topic=f"Переговоры с {family} · {who}")
        bot.db.q("INSERT INTO negotiations(channel_id, guild_id, family, requester_id, purpose, created_at) "
                 "VALUES(?,?,?,?,?,?)", ch.id, g.id, family, inter.user.id, purpose, now_iso())
        v = discord.ui.View(timeout=None)
        v.add_item(CloseButton(ch.id))
        dip = bot.role(g, "diplomat")
        e = bot.embed(f"🤝 Переговоры: {family}", f"**Представитель:** {inter.user.mention} ({who})\n\n**Цель:**\n{purpose}",
                      0x00ACC1, g)
        await ch.send(content=f"{dip.mention if dip else ''} {inter.user.mention}", embed=e, view=v,
                      allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        await inter.followup.send(f"✅ Канал переговоров создан: {ch.mention}", ephemeral=True)

    async def close_room(self, inter, cid, outcome):
        bot, g = self.bot, inter.guild
        ch = g.get_channel(cid)
        row = bot.db.one("SELECT * FROM negotiations WHERE channel_id=?", cid)
        if not ch or not row:
            return await inter.response.send_message("Канал уже закрыт.", ephemeral=True)
        await inter.response.send_message("🔒 Сохраняю протокол и закрываю переговоры…")
        lines = []
        async for m in ch.history(limit=800, oldest_first=True):
            if m.content or m.attachments:
                att = " ".join(a.url for a in m.attachments)
                lines.append(f"[{m.created_at:%d.%m.%Y %H:%M}] {m.author.display_name}: {m.content} {att}".strip())
        transcript = "\n".join(lines)
        summary = None
        ai = bot.ai_for(g)
        if ai.on and len(transcript) > 200:
            try:
                summary = await ai.ask("Сделай краткое резюме переговоров между семьями: о чём договорились, кто что "
                                           "обещал, открытые вопросы. 4-8 пунктов, по-русски.", transcript[-15000:],
                                           max_tokens=600)
            except Exception as e:
                log.warning("Резюме переговоров: %s", e)
        bot.db.q("UPDATE negotiations SET status='closed' WHERE channel_id=?", cid)
        arch = bot.ch(g, "diplo_archive")
        if arch:
            e = bot.embed(f"🗄 Переговоры: {row['family']}",
                          f"**Представитель:** <@{row['requester_id']}>\n**Цель:** {row['purpose'][:500]}\n\n"
                          f"**Итог** ({inter.user.mention}):\n{outcome}", 0x00ACC1, g)
            if summary:
                e.add_field(name="🤖 Резюме ИИ", value=summary[:1024], inline=False)
            await arch.send(embed=e, file=discord.File(io.BytesIO(transcript.encode("utf-8")),
                                                       filename=f"переговоры-{slug(row['family'])}.txt"))
        await asyncio.sleep(5)
        try:
            await ch.delete(reason="Переговоры завершены")
        except discord.HTTPException:
            pass

    @nego.command(name="пригласить", description="Добавить участника в текущий канал переговоров")
    @app_commands.rename(member="участник")
    @staff_only(config.DIPLO)
    async def nego_invite(self, inter: discord.Interaction, member: discord.Member):
        if not self.bot.db.one("SELECT 1 FROM negotiations WHERE channel_id=? AND status='open'", inter.channel.id):
            return await inter.response.send_message("Это не канал переговоров.", ephemeral=True)
        await inter.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await inter.response.send_message(f"➕ {member.mention} добавлен в переговоры.")

    # ---------- договоры ----------
    @treaty.command(name="заключить", description="Заключить договор с семьёй")
    @app_commands.autocomplete(family=family_auto)
    @app_commands.rename(family="семья", title="название", days="срок_дней")
    @app_commands.describe(days="0 — бессрочно")
    @staff_only(config.DIPLO)
    async def treaty_new(self, inter: discord.Interaction, family: str, title: str,
                         days: app_commands.Range[int, 0, 365] = 0):
        await inter.response.send_modal(TreatyModal(family, title, days))

    async def create_treaty(self, inter, family, title, terms, days):
        bot = self.bot
        until = (now() + dt.timedelta(days=days)).isoformat() if days else None
        cur = bot.db.q("INSERT INTO treaties(guild_id, family, title, terms, until, created_by, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", inter.guild.id, family, title, terms, until, inter.user.id, now_iso())
        tid = cur.lastrowid
        e = bot.embed(f"📜 Договор №{tid}: {title}",
                      f"**Стороны:** семья {config.FAMILY} и **{family}**\n**Срок:** "
                      f"{discord.utils.format_dt(parse_dt(until), 'D') if until else 'бессрочно'}\n\n**Условия:**\n{terms}",
                      config.GOLD, inter.guild)
        e.set_footer(text=f"Заключил: {inter.user.display_name} · {bot.brand(inter.guild, config.FOOTER)}")
        c = bot.ch(inter.guild, "treaties")
        if c:
            m = await c.send(embed=e)
            bot.db.q("UPDATE treaties SET message_id=? WHERE id=?", m.id, tid)
        bot.emit(inter.guild, "treaty", family=family, title=title, by=inter.user)
        await self.update_board(inter.guild)
        await inter.response.send_message(f"✅ Договор №{tid} заключён.", ephemeral=True)

    @treaty.command(name="расторгнуть", description="Расторгнуть договор")
    @app_commands.rename(tid="номер", reason="причина")
    @staff_only(config.DIPLO)
    async def treaty_end(self, inter: discord.Interaction, tid: int, reason: str):
        t = self.bot.db.one("SELECT * FROM treaties WHERE guild_id=? AND id=?", inter.guild.id, tid)
        if not t or not t["active"]:
            return await inter.response.send_message("Нет такого действующего договора.", ephemeral=True)
        self.bot.db.q("UPDATE treaties SET active=0 WHERE id=?", tid)
        c = self.bot.ch(inter.guild, "treaties")
        if c:
            await c.send(embed=self.bot.embed(f"✂️ Договор №{tid} расторгнут", f"«{t['title']}» с **{t['family']}**\n"
                                              f"Причина: {reason}", config.RED, inter.guild))
        await self.update_board(inter.guild)
        await inter.response.send_message("✅ Расторгнут.", ephemeral=True)

    @treaty.command(name="список", description="Действующие договоры")
    async def treaty_list(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM treaties WHERE guild_id=? AND active=1 ORDER BY id DESC", inter.guild.id)
        text = "\n".join(f"`№{t['id']}` **{t['title']}** — {t['family']}"
                         f"{' (до ' + discord.utils.format_dt(parse_dt(t['until']), 'd') + ')' if t['until'] else ''}"
                         for t in rows)
        await inter.response.send_message(embed=self.bot.embed("📜 Действующие договоры", text[:4000] or "Нет",
                                                               config.GOLD, inter.guild), ephemeral=True)

    # ---------- скоро истекает: напоминание и продление ----------
    async def remind_treaties(self):
        """За 3 дня и за сутки до конца договора — дипломатам в канал договоров, с кнопками продления."""
        db = self.bot.db
        soon = (now() + dt.timedelta(days=3)).isoformat()
        for t in db.all("SELECT * FROM treaties WHERE active=1 AND until IS NOT NULL AND until<?", soon):
            g = self.bot.get_guild(t["guild_id"])
            if not g or not self.bot.is_family(g):
                continue
            try:
                left = parse_dt(t["until"]) - now()
            except (TypeError, ValueError):
                continue
            stage = 2 if left <= dt.timedelta(days=1) else 1
            if int(db.gget(g, f"treaty:rem:{t['id']}") or 0) >= stage:
                continue
            db.gset(g, f"treaty:rem:{t['id']}", stage)
            c = self.bot.ch(g, "treaties")
            if not c:
                continue
            roles = [r for r in (self.bot.role(g, k) for k in ("diplomat",)) if r]
            v = discord.ui.View(timeout=None)
            for d in (7, 30):
                v.add_item(TreatyExtend(t["id"], d))
            try:
                await c.send(content=" ".join(r.mention for r in roles) or None,
                             embed=self.bot.embed(f"⌛ Договор №{t['id']} скоро истекает",
                                                  f"«{t['title']}» с **{t['family']}** — до "
                                                  f"{discord.utils.format_dt(parse_dt(t['until']), 'f')} "
                                                  f"({discord.utils.format_dt(parse_dt(t['until']), 'R')}).\n"
                                                  "Продлить — кнопкой. Ничего не делать — договор истечёт сам.",
                                                  0xFB8C00, g),
                             view=v, allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException:
                pass

    async def extend(self, inter, tid, days):
        if not self.bot.has(inter.user, config.DIPLO):
            return await inter.response.send_message("⛔ Продлевают дипломаты и руководство.", ephemeral=True)
        t = self.bot.db.one("SELECT * FROM treaties WHERE id=? AND guild_id=? AND active=1", tid, inter.guild.id)
        if not t:
            return await inter.response.send_message("Договор уже не действует.", ephemeral=True)
        try:
            base = max(parse_dt(t["until"]), now()) if t["until"] else now()
        except (TypeError, ValueError):
            base = now()
        until = base + dt.timedelta(days=days)
        self.bot.db.q("UPDATE treaties SET until=? WHERE id=?", until.isoformat(), tid)
        self.bot.db.gdel(inter.guild, f"treaty:rem:{tid}")
        await inter.response.edit_message(view=None)
        await inter.followup.send(f"✅ Договор №{tid} продлён до {discord.utils.format_dt(until, 'D')} "
                                  f"({inter.user.mention}).")
        await self.update_board(inter.guild)

    @tasks.loop(hours=6)
    @guarded
    async def expire(self):
        try:
            await self.remind_treaties()
        except Exception:
            log.exception("Напоминания о договорах")
        for t in self.bot.db.all("SELECT * FROM treaties WHERE active=1 AND until IS NOT NULL AND until<?", now_iso()):
            self.bot.db.q("UPDATE treaties SET active=0 WHERE id=?", t["id"])
            for g in filter(None, [self.bot.get_guild(t["guild_id"])]):
                c = self.bot.ch(g, "treaties")
                if c:
                    try:
                        await c.send(embed=self.bot.embed(f"⌛ Истёк договор №{t['id']}",
                                                          f"«{t['title']}» с **{t['family']}**", 0x757575, g))
                    except discord.HTTPException:
                        pass
                await self.update_board(g)

    @expire.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


class TreatyExtend(discord.ui.DynamicItem[discord.ui.Button], template=r"tr:ext:(?P<id>\d+):(?P<d>\d+)"):
    def __init__(self, tid, days):
        super().__init__(discord.ui.Button(label=f"Продлить на {days} дн.", emoji="🔁",
                                           style=discord.ButtonStyle.success, custom_id=f"tr:ext:{tid}:{days}"))
        self.tid, self.days = int(tid), int(days)

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["id"], match["d"])

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Diplomacy").extend(inter, self.tid, self.days)


async def setup(bot):
    bot.add_view(DiploPanel())
    bot.add_dynamic_items(CloseButton, TreatyExtend)
    await bot.add_cog(Diplomacy(bot))
