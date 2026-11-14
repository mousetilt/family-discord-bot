# -*- coding: utf-8 -*-
"""Чёрный список семьи: по Discord, Static ID или нику. Проверяется при заявке и при входе на сервер."""
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, norm_nick, now, now_iso, parse_dt, staff_only

BL_STAFF = config.LEAD + ["hr_head"]


def bl_text(r):
    who = []
    if r["discord_id"]:
        who.append(f"<@{r['discord_id']}>")
    if r["nick"]:
        who.append(f"**{r['nick']}**")
    if r["static_id"]:
        who.append(f"ID `{r['static_id']}`")
    until = f"до {discord.utils.format_dt(parse_dt(r['until']), 'D')}" if r["until"] else "навсегда"
    return f"`№{r['id']}` {' · '.join(who) or '—'} — {r['reason']} ({until})"


class Blacklist(commands.Cog):
    group = app_commands.Group(name="чс", description="Чёрный список семьи", default_permissions=discord.Permissions(manage_nicknames=True))

    def __init__(self, bot):
        self.bot = bot
        self.expire_loop.start()

    def cog_unload(self):
        self.expire_loop.cancel()

    @group.command(name="добавить", description="Внести в чёрный список (нужен хотя бы один признак: участник, ID или ник)")
    @app_commands.rename(reason="причина", member="участник", static="static_id", nick="ник", days="дней",
                         expel="исключить_из_семьи")
    @app_commands.describe(days="0 — навсегда", expel="Если человек в семье — снять все семейные роли")
    @staff_only(BL_STAFF)
    async def add(self, inter: discord.Interaction, reason: str, member: discord.Member = None, static: str = None,
                  nick: str = None, days: app_commands.Range[int, 0, 3650] = 0, expel: bool = True):
        if not (member or static or nick):
            return await inter.response.send_message("Укажи участника, Static ID или ник.", ephemeral=True)
        if static and not static.strip().isdigit():
            return await inter.response.send_message("Static ID — только цифры.", ephemeral=True)
        bot = self.bot
        if member and expel and bot.in_family(member) and not bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя внести в ЧС того, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        if member and not static:
            app = bot.db.one("SELECT static_id, nick FROM applications WHERE guild_id=? AND user_id=? AND static_id IS "
                             "NOT NULL ORDER BY id DESC LIMIT 1", inter.guild.id, member.id)
            if app:
                static = static or app["static_id"]
                nick = nick or app["nick"]
        until = (now() + dt.timedelta(days=days)).isoformat() if days else None
        cur = bot.db.q("INSERT INTO blacklist(guild_id, discord_id, static_id, nick, reason, added_by, created_at, until) "
                       "VALUES(?,?,?,?,?,?,?,?)", inter.guild.id, member.id if member else None, static.strip() if static else None,
                       nick, reason, inter.user.id, now_iso(), until)
        r = bot.db.one("SELECT * FROM blacklist WHERE id=?", cur.lastrowid)
        if member and expel and bot.in_family(member):
            await bot.expel(member, inter.user, f"Чёрный список: {reason}")
        c = bot.ch(inter.guild, "blacklist_ch")
        if c:
            e = bot.embed("⛔ Внесён в чёрный список", bl_text(r) + f"\n\nВнёс: {inter.user.mention}", 0x212121,
                          inter.guild)
            await c.send(embed=e)
        await bot.log(inter.guild, f"⛔ ЧС №{r['id']}: {bl_text(r)} — {inter.user.mention}", "hrlog", 0x212121)
        await inter.followup.send(f"✅ Внесено в ЧС под №{r['id']}.", ephemeral=True)

    @group.command(name="убрать", description="Убрать из чёрного списка")
    @app_commands.rename(bl_id="номер")
    @staff_only(BL_STAFF)
    async def remove(self, inter: discord.Interaction, bl_id: int):
        r = self.bot.db.one("SELECT * FROM blacklist WHERE guild_id=? AND id=?", inter.guild.id, bl_id)
        if not r:
            return await inter.response.send_message("Нет такой записи.", ephemeral=True)
        self.bot.db.q("UPDATE blacklist SET active=0 WHERE id=?", bl_id)
        c = self.bot.ch(inter.guild, "blacklist_ch")
        if c:
            await c.send(embed=self.bot.embed("✅ Убран из чёрного списка", bl_text(r) + f"\n\nУбрал: {inter.user.mention}",
                                              config.GREEN, inter.guild))
        await inter.response.send_message("✅ Убрано.", ephemeral=True)

    @group.command(name="проверить", description="Проверить человека по нику, Static ID или Discord")
    @app_commands.rename(query="ник_или_id", member="участник")
    @staff_only(config.FAMILY_RANKS)
    async def check(self, inter: discord.Interaction, query: str = None, member: discord.Member = None):
        if not (query or member):
            return await inter.response.send_message("Укажи ник/Static ID или участника.", ephemeral=True)
        bot = self.bot
        hit = bot.blacklist_find(inter.guild, discord_id=member.id if member else None,
                                 static=query if query and query.strip().isdigit() else None,
                                 nick=query if query and not query.strip().isdigit() else None)
        if not hit and query:
            q = norm_nick(query)
            for r in bot.db.all("SELECT * FROM blacklist WHERE guild_id=? AND active=1", inter.guild.id):
                if q and r["nick"] and q in norm_nick(r["nick"]):
                    hit = r
                    break
        if hit:
            await inter.response.send_message(embed=bot.embed("⛔ Найден в чёрном списке", bl_text(hit), 0x212121,
                                                              inter.guild), ephemeral=True)
        else:
            await inter.response.send_message("✅ В чёрном списке не найден.", ephemeral=True)

    @group.command(name="список", description="Весь чёрный список")
    @staff_only(config.FAMILY_RANKS)
    async def list_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM blacklist WHERE guild_id=? AND active=1 ORDER BY id DESC LIMIT 40",
                               inter.guild.id)
        await inter.response.send_message(embed=self.bot.embed(f"⛔ Чёрный список ({len(rows)})",
                                                               "\n".join(bl_text(r) for r in rows)[:4000] or "Пусто",
                                                               0x212121, inter.guild), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        r = self.bot.blacklist_find(member.guild, discord_id=member.id)
        if r:
            hr = self.bot.role(member.guild, "hr_head")
            c = self.bot.ch(member.guild, "hrlog")
            if c:
                await c.send(content=hr.mention if hr else None,
                             embed=self.bot.embed("⛔ На сервер зашёл человек из ЧС", bl_text(r), 0x212121, member.guild),
                             allowed_mentions=discord.AllowedMentions(roles=True))

    @tasks.loop(hours=6)
    @guarded
    async def expire_loop(self):
        self.bot.db.q("UPDATE blacklist SET active=0 WHERE active=1 AND until IS NOT NULL AND until<?", now_iso())

    @expire_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
