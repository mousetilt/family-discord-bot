# -*- coding: utf-8 -*-
"""Рынок семьи: объявления «продаю/куплю/обмен», приватные ветки для сделок, статистика цен."""
import datetime as dt
import io
import re
import statistics

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import family_only, guarded, now, now_iso, parse_dt

MULT = {"к": 1e3, "k": 1e3, "тыс": 1e3, "кк": 1e6, "kk": 1e6, "млн": 1e6, "ккк": 1e9, "kkk": 1e9, "млрд": 1e9}


def parse_price(s):
    s = (s or "").lower().replace(" ", "").replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)(ккк|kkk|кк|kk|млрд|млн|тыс|к|k)?", s)
    if not m:
        return None
    return int(float(m.group(1)) * MULT.get(m.group(2) or "", 1))


def fmt_price(n):
    return f"{int(n):,}".replace(",", " ") + " ₽" if n is not None else "договорная"


def words(q):
    return [w for w in re.findall(r"\w+", (q or "").lower().replace("ё", "е")) if len(w) > 1]


def matches(row, q_words):
    text = f"{row['title']} {row['description'] or ''}".lower().replace("ё", "е")
    return all(w[:5] in text for w in q_words)


class ListingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mk:(?P<a>contact|sold|bump|close):(?P<id>\d+)"):
    LABELS = {"contact": ("Связаться", "💬", discord.ButtonStyle.primary),
              "sold": ("Продано", "✅", discord.ButtonStyle.success),
              "bump": ("Поднять", "🔼", discord.ButtonStyle.secondary),
              "close": ("Снять", "🗑", discord.ButtonStyle.danger)}

    def __init__(self, a, lid):
        label, emoji, style = self.LABELS[a]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"mk:{a}:{lid}"))
        self.a, self.lid = a, lid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], int(match["id"]))

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Market").button(inter, self.a, self.lid)


class ListingModal(discord.ui.Modal):
    name = discord.ui.TextInput(label="Что продаёшь / ищешь", max_length=80, placeholder="BMW M5 F90, тюнинг 3 ступени")
    price = discord.ui.TextInput(label="Цена", max_length=40, placeholder="1.5кк · 1 500 000 · договорная")
    desc = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, max_length=800, required=False,
                                placeholder="Состояние, где посмотреть, торг уместен?")

    def __init__(self, kind, category, photo):
        super().__init__(title=f"{config.MARKET_KINDS[kind][1]} · {config.MARKET_CATEGORIES[category]}"[:45])
        self.kind, self.category, self.photo = kind, category, photo

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Market").create(inter, self.kind, self.category, self.name.value, self.price.value,
                                                    self.desc.value, self.photo)


class SoldModal(discord.ui.Modal, title="Сделка закрыта"):
    final = discord.ui.TextInput(label="За сколько продал (для статистики цен)", max_length=40, required=False)

    def __init__(self, lid):
        super().__init__()
        self.lid = lid

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Market").mark_sold(inter, self.lid, self.final.value)


class Market(commands.Cog):
    group = app_commands.Group(name="рынок", description="Рынок семьи")

    def __init__(self, bot):
        self.bot = bot
        self.contacts = set()
        self.expire.start()

    def cog_unload(self):
        self.expire.cancel()

    def link(self, guild, row):
        c = self.bot.ch(guild, "market")
        return f"https://discord.com/channels/{guild.id}/{c.id if c else 0}/{row['message_id']}"

    def embed(self, row, guild, image=False):
        emoji, kind, color = config.MARKET_KINDS.get(row["kind"], ("", "", config.COLOR))
        st = row["status"]
        head = {"sold": "🔴 ПРОДАНО", "expired": "⌛ ИСТЕКЛО", "closed": "🗑 СНЯТО"}.get(st, f"{emoji} {kind.upper()}")
        e = self.bot.embed(f"{head} · {config.MARKET_CATEGORIES.get(row['category'], '')}",
                           f"## {row['title']}\n💰 **{row['price']}**" +
                           (f"  ·  ≈ {fmt_price(row['price_num'])}" if row["price_num"] and not re.search(r"\d{4,}", row["price"].replace(" ", "")) else "") +
                           (f"\n\n{row['description']}" if row["description"] else ""),
                           color if st == "active" else 0x757575, guild)
        e.add_field(name="Автор", value=f"<@{row['user_id']}>", inline=True)
        e.add_field(name="Объявление", value=f"№{row['id']}", inline=True)
        e.add_field(name="Обновлено", value=discord.utils.format_dt(parse_dt(row["bumped_at"]), "R"), inline=True)
        if st == "sold":
            e.add_field(name="Итог", value=f"Продано{' за ' + fmt_price(row['sold_price']) if row['sold_price'] else ''}",
                        inline=False)
        if image:
            e.set_image(url="attachment://photo.png")
        return e

    def view(self, row):
        v = discord.ui.View(timeout=None)
        if row["status"] == "active":
            for a in ("contact", "sold", "bump", "close"):
                v.add_item(ListingButton(a, row["id"]))
        elif row["status"] == "expired":
            v.add_item(ListingButton("bump", row["id"]))
            v.add_item(ListingButton("close", row["id"]))
        return v

    async def send_listing(self, guild, row, image_bytes):
        c = self.bot.ch(guild, "market")
        kw = {"embed": self.embed(row, guild, image=bool(image_bytes)), "view": self.view(row)}
        if image_bytes:
            kw["file"] = discord.File(io.BytesIO(image_bytes), filename="photo.png")
        ping = self.bot.role(guild, "n_market")
        msg = await c.send(content=ping.mention if ping else None, allowed_mentions=discord.AllowedMentions(roles=True),
                           **kw)
        self.bot.db.q("UPDATE market SET message_id=? WHERE id=?", msg.id, row["id"])
        return msg

    # ---------- создание ----------
    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="продать", description="Разместить объявление на рынке семьи")
    @app_commands.rename(category="категория", kind="тип", photo="фото")
    @app_commands.choices(category=[app_commands.Choice(name=v, value=k) for k, v in config.MARKET_CATEGORIES.items()],
                          kind=[app_commands.Choice(name=f"{e} {n}", value=k) for k, (e, n, _) in config.MARKET_KINDS.items()])
    @family_only()
    async def sell(self, inter: discord.Interaction, category: app_commands.Choice[str],
                   kind: app_commands.Choice[str] = None, photo: discord.Attachment = None):
        if not self.bot.ch(inter.guild, "market"):
            return await inter.response.send_message("Канал рынка не настроен — запусти /настройка.", ephemeral=True)
        active = self.bot.db.one("SELECT COUNT(*) n FROM market WHERE guild_id=? AND user_id=? AND status='active'",
                                 inter.guild.id, inter.user.id)["n"]
        if active >= 10:
            return await inter.response.send_message("У тебя уже 10 активных объявлений — сними лишние.", ephemeral=True)
        await inter.response.send_modal(ListingModal(kind.value if kind else "sell", category.value, photo))

    async def create(self, inter, kind, category, title, price, desc, photo):
        await inter.response.defer(ephemeral=True)
        image = None
        if photo and (photo.content_type or "").startswith("image/") and photo.size < 8 * 1024 * 1024:
            try:
                image = await photo.read()
            except discord.HTTPException:
                image = None
        t = now_iso()
        cur = self.bot.db.q("INSERT INTO market(guild_id, user_id, kind, category, title, price, price_num, description, "
                            "created_at, bumped_at, has_image) VALUES(?,?,?,?,?,?,?,?,?,?,?)", inter.guild.id, inter.user.id, kind, category, title,
                            price, parse_price(price), desc or None, t, t, int(bool(image)))
        row = self.bot.db.one("SELECT * FROM market WHERE id=?", cur.lastrowid)
        msg = await self.send_listing(inter.guild, row, image)
        await inter.followup.send(f"✅ Объявление №{row['id']} опубликовано: {msg.jump_url}\n"
                                  f"Через {config.MARKET_DAYS} дн. оно истечёт — его можно будет поднять.", ephemeral=True)

    # ---------- кнопки ----------
    async def button(self, inter, a, lid):
        bot = self.bot
        row = bot.db.one("SELECT * FROM market WHERE id=?", lid)
        if not row:
            return await inter.response.send_message("Объявление не найдено.", ephemeral=True)
        owner = inter.user.id == row["user_id"]
        if a == "contact":
            if owner:
                return await inter.response.send_message("Это твоё объявление 🙂", ephemeral=True)
            if (lid, inter.user.id) in self.contacts:
                return await inter.response.send_message("Ветка сделки уже создана — посмотри в списке веток канала.",
                                                         ephemeral=True)
            seller = inter.guild.get_member(row["user_id"])
            if not seller:
                return await inter.response.send_message("Автор объявления покинул сервер.", ephemeral=True)
            if row["status"] != "active":
                return await inter.response.send_message("Объявление уже неактивно.", ephemeral=True)
            self.contacts.add((lid, inter.user.id))
            await inter.response.defer(ephemeral=True)
            try:
                th = await inter.channel.create_thread(name=f"Сделка №{lid} · {inter.user.display_name}"[:100],
                                                       type=discord.ChannelType.private_thread, invitable=False,
                                                       auto_archive_duration=1440)
            except discord.HTTPException:
                self.contacts.discard((lid, inter.user.id))
                return await inter.followup.send("❌ Не получилось создать ветку — попробуй ещё раз.", ephemeral=True)
            for u in (seller, inter.user):
                try:
                    await th.add_user(u)
                except discord.HTTPException:
                    pass
            await th.send(f"🤝 {seller.mention}, {inter.user.mention} интересуется объявлением **№{lid} «{row['title']}»**.\n"
                          "Договаривайтесь здесь. Передавайте имущество только через игровые механики и не платите "
                          "заранее незнакомым. Нужен гарант — позовите руководство семьи.\n"
                          "⚠️ Сделки за реальные деньги запрещены правилами проекта (PermBan).")
            return await inter.followup.send(f"✅ Ветка сделки: {th.mention}", ephemeral=True)
        if not (owner or bot.has(inter.user, config.LEAD)):
            return await inter.response.send_message("Это может сделать только автор объявления.", ephemeral=True)
        if a == "sold":
            return await inter.response.send_modal(SoldModal(lid))
        if a == "close":
            bot.db.q("UPDATE market SET status='closed' WHERE id=?", lid)
            await inter.response.send_message("🗑 Объявление снято.", ephemeral=True)
            try:
                await inter.message.delete()
            except discord.HTTPException:
                pass
            return
        if a == "bump":
            last = parse_dt(row["bumped_at"])
            if row["status"] == "active" and now() - last < dt.timedelta(hours=24):
                left = last + dt.timedelta(hours=24)
                return await inter.response.send_message(f"Поднять можно {discord.utils.format_dt(left, 'R')}.",
                                                         ephemeral=True)
            await inter.response.defer(ephemeral=True)
            image = None
            if inter.message.attachments:
                try:
                    image = await inter.message.attachments[0].read()
                except discord.HTTPException:
                    pass
            bot.db.q("UPDATE market SET status='active', bumped_at=? WHERE id=?", now_iso(), lid)
            row = bot.db.one("SELECT * FROM market WHERE id=?", lid)
            try:
                await inter.message.delete()
            except discord.HTTPException:
                pass
            msg = await self.send_listing(inter.guild, row, image)
            await inter.followup.send(f"🔼 Объявление поднято: {msg.jump_url}", ephemeral=True)

    async def mark_sold(self, inter, lid, final):
        bot = self.bot
        price = parse_price(final) if final else None
        row = bot.db.one("SELECT * FROM market WHERE id=?", lid)
        if not row or row["status"] != "active":
            return await inter.response.send_message("Объявление уже закрыто.", ephemeral=True)
        bot.db.q("UPDATE market SET status='sold', sold_price=?, sold_at=? WHERE id=?",
                 price or row["price_num"], now_iso(), lid)
        row = bot.db.one("SELECT * FROM market WHERE id=?", lid)
        await inter.response.edit_message(embed=self.embed(row, inter.guild, image=bool(inter.message.attachments)),
                                          view=None, attachments=inter.message.attachments)

    # ---------- поиск и цены ----------
    @group.command(name="поиск", description="Найти объявления")
    @app_commands.rename(query="запрос", category="категория")
    @app_commands.choices(category=[app_commands.Choice(name=v, value=k) for k, v in config.MARKET_CATEGORIES.items()])
    @family_only()
    async def search(self, inter: discord.Interaction, query: str = None, category: app_commands.Choice[str] = None):
        rows = self.bot.db.all("SELECT * FROM market WHERE guild_id=? AND status='active' ORDER BY bumped_at DESC",
                               inter.guild.id)
        q = words(query)
        rows = [r for r in rows if (not category or r["category"] == category.value) and (not q or matches(r, q))]
        lines = [f"{config.MARKET_KINDS[r['kind']][0]} [{r['title'][:60]}]({self.link(inter.guild, r)}) — **{r['price']}**"
                 for r in rows[:20]]
        await inter.response.send_message(embed=self.bot.embed(f"🔎 Найдено: {len(rows)}", "\n".join(lines) or "Ничего",
                                                               config.COLOR, inter.guild), ephemeral=True)

    @group.command(name="мои", description="Мои объявления")
    async def mine(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM market WHERE guild_id=? AND user_id=? AND status IN ('active','expired') "
                               "ORDER BY bumped_at DESC", inter.guild.id, inter.user.id)
        lines = [f"{'🟢' if r['status'] == 'active' else '⌛'} №{r['id']} [{r['title'][:60]}]({self.link(inter.guild, r)})"
                 f" — {r['price']}" for r in rows]
        await inter.response.send_message(embed=self.bot.embed("🛒 Мои объявления", "\n".join(lines) or "Пусто",
                                                               config.COLOR, inter.guild), ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="цена", description="Сколько стоит вещь — по прошлым сделкам на рынке семьи")
    @app_commands.rename(query="что")
    @family_only()
    async def price(self, inter: discord.Interaction, query: str):
        q = words(query)
        gid = inter.guild.id
        sold = [r for r in self.bot.db.all("SELECT * FROM market WHERE guild_id=? AND status='sold' AND sold_price IS "
                                           "NOT NULL ORDER BY sold_at DESC", gid) if matches(r, q)]
        active = [r for r in self.bot.db.all("SELECT * FROM market WHERE guild_id=? AND status='active' AND price_num "
                                             "IS NOT NULL AND kind='sell'", gid) if matches(r, q)]
        e = self.bot.embed(f"📈 Цена: {query[:100]}", None, config.GOLD, inter.guild)
        if sold:
            prices = [r["sold_price"] for r in sold]
            e.add_field(name=f"Сделки ({len(sold)})",
                        value=f"Медиана: **{fmt_price(statistics.median(prices))}**\nМин: {fmt_price(min(prices))} · "
                              f"Макс: {fmt_price(max(prices))}", inline=False)
            e.add_field(name="Последние", value="\n".join(f"{r['sold_at'][:10]} — {r['title'][:50]}: "
                                                          f"{fmt_price(r['sold_price'])}" for r in sold[:5]), inline=False)
        if active:
            ap = [r["price_num"] for r in active]
            e.add_field(name=f"Сейчас продают ({len(active)})",
                        value=f"от {fmt_price(min(ap))} до {fmt_price(max(ap))}", inline=False)
        if not e.fields:
            e.description = "Пока нет данных. Статистика копится, когда продавцы жмут «✅ Продано» и указывают цену."
        await inter.response.send_message(embed=e)

    @tasks.loop(hours=6)
    @guarded
    async def expire(self):
        border = (now() - dt.timedelta(days=config.MARKET_DAYS)).isoformat()
        rows = self.bot.db.all("SELECT * FROM market WHERE status='active' AND bumped_at<?", border)
        for row in rows:
            self.bot.db.q("UPDATE market SET status='expired' WHERE id=?", row["id"])
            row = self.bot.db.one("SELECT * FROM market WHERE id=?", row["id"])
            for g in filter(None, [self.bot.get_guild(row["guild_id"])]):
                c = self.bot.ch(g, "market")
                if not c or not row["message_id"]:
                    continue
                try:
                    m = await c.fetch_message(row["message_id"])
                    await m.edit(embed=self.embed(row, g, image=bool(m.attachments)), view=self.view(row))
                except discord.HTTPException:
                    pass

    @expire.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    bot.add_dynamic_items(ListingButton)
    await bot.add_cog(Market(bot))
