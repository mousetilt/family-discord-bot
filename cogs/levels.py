# -*- coding: utf-8 -*-
"""Уровни, достижения и магазин семьи (модуль «levels» в /настройки).
• Очки и уровень — core/levels.py (войс, явка на сборы, взносы, стаж; выговоры отнимают). Видно в «👤 Карточке».
• Достижения: раз в 3 часа бот проверяет семью; новые — одной строкой в общий чат и в летопись. Первый проход после
  включения — молча (иначе все старожилы получили бы «Год в семье» разом).
• /уровни — топ семьи и твой прогресс. /магазин — товары за очки: руководство добавляет (что угодно: личная роль,
  выходной от взноса, место в первом ряду), участник покупает → заявка руководству с кнопками «Выдано / Отказать»
  (отказ возвращает очки). Выдаёт человек — бот ничего не выдаёт сам."""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now
from core.levels import ACHIEVEMENTS, bar, earned, guild_stats, next_at

log = logging.getLogger("family-bot.levels")
SCHEMA = """
CREATE TABLE IF NOT EXISTS achievements(guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, key TEXT NOT NULL,
                                        ts INTEGER, PRIMARY KEY(guild_id, user_id, key));
CREATE TABLE IF NOT EXISTS shop_items(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT,
                                      price INTEGER, description TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS shop_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                                       user_id INTEGER, item_id INTEGER, price INTEGER, status TEXT DEFAULT 'wait',
                                       ts INTEGER, decided_by INTEGER);
"""
SHOP_ADMINS = config.LEAD + ["treasurer"]


class OrderButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shop:(?P<a>ok|no):(?P<id>\d+)"):
    def __init__(self, a, oid):
        super().__init__(discord.ui.Button(label="Выдано" if a == "ok" else "Отказать", emoji="✅" if a == "ok" else "✖️",
                                           style=discord.ButtonStyle.success if a == "ok" else discord.ButtonStyle.danger,
                                           custom_id=f"shop:{a}:{oid}"))
        self.a, self.oid = a, int(oid)

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], match["id"])

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Levels").decide(inter, self.oid, self.a == "ok")


class Levels(commands.Cog):
    shop = app_commands.Group(name="магазин", description="Магазин семьи: товары за очки активности", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ---------- достижения ----------
    @tasks.loop(hours=3)
    @guarded
    async def loop(self):
        for g in list(self.bot.home_guilds):
            if self.bot.feature(g, "levels:achievements"):
                await self.check(g)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def check(self, g):
        db = self.bot.db
        silent = not db.gget(g, "levels:init")
        have = {(r["user_id"], r["key"]) for r in db.all("SELECT user_id, key FROM achievements WHERE guild_id=?", g.id)}
        new, ts = [], int(now().timestamp())
        for uid, st in guild_stats(self.bot, g).items():
            for k in earned(st):
                if (uid, k) not in have:
                    db.q("INSERT OR IGNORE INTO achievements(guild_id, user_id, key, ts) VALUES(?,?,?,?)", g.id, uid, k, ts)
                    new.append((uid, k))
        db.gset(g, "levels:init", 1)
        if silent or not new:
            return
        c = self.bot.ch(g, "chat")
        lines = [f"{ACHIEVEMENTS[k][0]} <@{uid}> — **{ACHIEVEMENTS[k][1]}**" for uid, k in new[:12]]
        if c:
            try:
                await c.send(embed=self.bot.embed("🏅 Новые достижения", "\n".join(lines)
                                                  + (f"\n…и ещё {len(new) - 12}" if len(new) > 12 else ""),
                                                  config.GOLD, g), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        for uid, k in new[:12]:
            self.bot.emit(g, "achievement", member=g.get_member(uid), key=k, title=ACHIEVEMENTS[k][1])

    def card_line(self, member):
        """Строка для «👤 Карточки»: уровень, прогресс, очки, достижения."""
        if not self.bot.feature(member.guild, "levels"):
            return None
        st = guild_stats(self.bot, member.guild, [member]).get(member.id)
        if not st:
            return None
        ach = [ACHIEVEMENTS[r["key"]][0] for r in self.bot.db.all(
            "SELECT key FROM achievements WHERE guild_id=? AND user_id=?", member.guild.id, member.id)
               if r["key"] in ACHIEVEMENTS]
        return (f"🏅 Уровень **{st['level']}** `{bar(st['xp'])}` {st['xp']}/{next_at(st['level'])} · "
                f"очков в магазине: {st['points']}" + (f"\n{''.join(ach)}" if ach else ""))

    @app_commands.command(name="уровни", description="Уровни семьи: топ и твой прогресс")
    @app_commands.guild_only()
    async def levels_cmd(self, inter: discord.Interaction):
        if not self.bot.feature(inter.guild, "levels"):
            return await inter.response.send_message("🏅 Уровни у семьи выключены.", ephemeral=True)
        stats = guild_stats(self.bot, inter.guild)
        top = sorted(stats.items(), key=lambda x: x[1]["xp"], reverse=True)[:10]
        lines = [f"{i + 1}. <@{uid}> — ур. **{s['level']}** · {s['xp']} очк." for i, (uid, s) in enumerate(top)]
        me = stats.get(inter.user.id)
        if me:
            lines.append(f"\n**Ты:** ур. {me['level']} `{bar(me['xp'])}` {me['xp']}/{next_at(me['level'])} · "
                         f"в магазине можно потратить {me['points']}")
        lines.append("\n-# Очки: войс (1 за 10 мин), явка на сбор (50), взнос (30), стаж (2 в день); выговор −100.")
        await inter.response.send_message(embed=self.bot.embed("🏅 Уровни семьи", "\n".join(lines)[:4000], config.GOLD,
                                                               inter.guild), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    # ---------- магазин ----------
    def gate(self, guild):
        return self.bot.feature(guild, "levels:shop")

    async def item_auto(self, inter: discord.Interaction, current: str):
        rows = self.bot.db.all("SELECT * FROM shop_items WHERE guild_id=? AND active=1 ORDER BY price", inter.guild.id)
        cur = (current or "").lower()
        return [app_commands.Choice(name=f"{r['name']} — {r['price']} очк."[:100], value=str(r["id"]))
                for r in rows if cur in r["name"].lower()][:25]

    @shop.command(name="список", description="Что можно купить за очки")
    async def s_list(self, inter: discord.Interaction):
        if not self.gate(inter.guild):
            return await inter.response.send_message("🛍 Магазин у семьи выключен.", ephemeral=True)
        rows = self.bot.db.all("SELECT * FROM shop_items WHERE guild_id=? AND active=1 ORDER BY price", inter.guild.id)
        st = guild_stats(self.bot, inter.guild, [inter.user]).get(inter.user.id) if self.bot.in_family(inter.user) else None
        text = "\n".join(f"**{r['name']}** — {r['price']} очк." + (f"\n-# {r['description']}" if r["description"] else "")
                         for r in rows) or "Пока пусто. Руководство добавляет товары: `/магазин добавить`."
        if st:
            text += f"\n\nУ тебя: **{st['points']}** очк."
        await inter.response.send_message(embed=self.bot.embed("🛍 Магазин семьи", text[:4000], config.GOLD, inter.guild),
                                          ephemeral=True)

    @shop.command(name="купить", description="Купить товар за очки (выдаёт руководство)")
    @app_commands.rename(item="товар")
    @app_commands.autocomplete(item=item_auto)
    async def s_buy(self, inter: discord.Interaction, item: str):
        g, db = inter.guild, self.bot.db
        if not self.gate(g) or not self.bot.in_family(inter.user):
            return await inter.response.send_message("🛍 Магазин — для семьи (и он должен быть включён).", ephemeral=True)
        try:
            it = db.one("SELECT * FROM shop_items WHERE guild_id=? AND id=? AND active=1", g.id, int(item))
        except ValueError:
            it = None
        if not it:
            return await inter.response.send_message("❌ Нет такого товара — выбери из подсказок.", ephemeral=True)
        st = guild_stats(self.bot, g, [inter.user])[inter.user.id]
        if st["points"] < it["price"]:
            return await inter.response.send_message(f"❌ Не хватает очков: у тебя {st['points']}, нужно {it['price']}.",
                                                     ephemeral=True)
        oid = db.q("INSERT INTO shop_orders(guild_id, user_id, item_id, price, ts) VALUES(?,?,?,?,?)", g.id,
                   inter.user.id, it["id"], it["price"], int(now().timestamp())).lastrowid
        c = self.bot.ch(g, "botcmd") or self.bot.ch(g, "council_chat")
        if c:
            v = discord.ui.View(timeout=None)
            v.add_item(OrderButton("ok", oid))
            v.add_item(OrderButton("no", oid))
            try:
                await c.send(embed=self.bot.embed(f"🛍 Заказ №{oid}", f"{inter.user.mention} купил **{it['name']}** за "
                                                  f"{it['price']} очк. Выдайте и нажмите «Выдано».", config.GOLD, g),
                             view=v)
            except discord.HTTPException:
                pass
        await inter.response.send_message(f"🛍 Заказ №{oid}: {it['name']}. Руководство выдаст — сообщу в ЛС.", ephemeral=True)

    async def decide(self, inter, oid, ok):
        g, db = inter.guild, self.bot.db
        if not self.bot.has(inter.user, SHOP_ADMINS):
            return await inter.response.send_message("⛔ Выдаёт руководство.", ephemeral=True)
        o = db.one("SELECT * FROM shop_orders WHERE guild_id=? AND id=?", g.id, oid)
        if not o or o["status"] != "wait":
            return await inter.response.send_message("Уже решено.", ephemeral=True)
        db.q("UPDATE shop_orders SET status=?, decided_by=? WHERE id=?", "done" if ok else "rejected", inter.user.id, oid)
        it = db.one("SELECT name FROM shop_items WHERE id=?", o["item_id"])
        m = g.get_member(o["user_id"])
        if m:
            try:
                await m.send(f"🛍 Заказ №{oid} ({it['name'] if it else 'товар'}): "
                             + ("выдан ✅" if ok else "отклонён — очки вернулись."))
            except discord.HTTPException:
                pass
        await inter.response.edit_message(content=f"{'✅ Выдано' if ok else '✖️ Отказано (очки вернулись)'} — "
                                                  f"{inter.user.mention}", view=None)

    @shop.command(name="добавить", description="Добавить товар (руководство)")
    @app_commands.rename(name="название", price="цена", description="описание")
    async def s_add(self, inter: discord.Interaction, name: app_commands.Range[str, 1, 60],
                    price: app_commands.Range[int, 1, 1000000], description: app_commands.Range[str, 0, 200] = None):
        if not self.bot.has(inter.user, SHOP_ADMINS):
            return await inter.response.send_message("⛔ Товары добавляет руководство.", ephemeral=True)
        self.bot.db.q("INSERT INTO shop_items(guild_id, name, price, description) VALUES(?,?,?,?)", inter.guild.id,
                      name.strip(), price, (description or "").strip() or None)
        await inter.response.send_message(f"✅ В магазине: **{name}** — {price} очк.", ephemeral=True)

    @shop.command(name="убрать", description="Убрать товар (руководство)")
    @app_commands.rename(item="товар")
    @app_commands.autocomplete(item=item_auto)
    async def s_del(self, inter: discord.Interaction, item: str):
        if not self.bot.has(inter.user, SHOP_ADMINS):
            return await inter.response.send_message("⛔ Только руководство.", ephemeral=True)
        try:
            self.bot.db.q("UPDATE shop_items SET active=0 WHERE guild_id=? AND id=?", inter.guild.id, int(item))
        except ValueError:
            return await inter.response.send_message("❌ Выбери из подсказок.", ephemeral=True)
        await inter.response.send_message("🗑 Убрал.", ephemeral=True)


async def setup(bot):
    bot.add_dynamic_items(OrderButton)
    await bot.add_cog(Levels(bot))
