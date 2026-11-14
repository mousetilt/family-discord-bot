# -*- coding: utf-8 -*-
"""Бусты сервера: мотивация и благодарность.
• Панель прогресса в 💎・бусты: сколько бустов, уровень, что откроем дальше (тег семьи, баннер, качество войса).
• Забустил → благодарность в чате и в ЛС, запись в летописи, место в «Зале почёта».
• Бонусы бустерам: личная роль со своим цветом и названием (/буст роль), 💎 в профиле.
• Раз в неделю — одно спокойное напоминание в общий чат, если до следующей цели не хватает бустов.
• Буст закончился — личная роль снимается сама."""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now

log = logging.getLogger("family-bot.boosts")
BOOST_COLOR = 0xF47FFF
REMIND_DAYS = 7
# цели: (бустов, что открываем)
GOALS = [
    (2, "1 уровень: анимированная иконка, лучше звук в войсах, больше слотов эмодзи"),
    (3, "тег семьи рядом с ником у всех нас (например ULNV) — видно в любом чате Discord"),
    (7, "2 уровень: баннер сервера, иконки ролей, стримы в 1080p, ещё лучше звук"),
    (14, "3 уровень: своя ссылка на сервер и анимированный баннер"),
]


def bar(cur, goal, width=12):
    filled = min(width, round(width * cur / goal)) if goal else width
    return "▰" * filled + "▱" * (width - filled)


class HowToBoost(discord.ui.DynamicItem[discord.ui.Button], template=r"boost:how"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Как забустить", emoji="💎", style=discord.ButtonStyle.primary,
                                           custom_id="boost:how"))

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls()

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_message(
            "**Как забустить сервер семьи**\n"
            "1. Нажми на название сервера слева сверху → **«Буст сервера»**.\n"
            "2. С подпиской Nitro у тебя уже есть 2 буста — их можно отдать нам бесплатно.\n"
            "3. Без Nitro буст можно купить отдельно там же.\n\n"
            "**Что получишь от семьи:** личную роль со своим цветом и названием (`/буст роль`), 💎 в профиле, "
            "место в «Зале почёта» и запись в летописи. Спасибо! 🙏", ephemeral=True)


class Boosts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.remind.start()

    async def upgrade_voice(self, guild):
        """Уровень бустов поднимает потолок качества звука — ставим максимум во всех войсах."""
        top, n = int(guild.bitrate_limit), 0
        for vc in guild.voice_channels:
            if vc.bitrate < top:
                try:
                    await vc.edit(bitrate=top, reason="Бусты: максимальное качество звука")
                    n += 1
                except discord.HTTPException:
                    pass
        return n, top

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.home_guilds:
            await self.upgrade_voice(g)

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        if before.premium_tier == after.premium_tier:
            return
        await self.refresh(after)
        if after.premium_tier > before.premium_tier:
            n, top = await self.upgrade_voice(after)
            await self.bot.log(after, f"💎 Сервер получил **{after.premium_tier}-й уровень** бустов. Качество звука в "
                                      f"войсах поднято до {top // 1000} кбит/с ({n} каналов).", color=BOOST_COLOR)

    def cog_unload(self):
        self.remind.cancel()

    # ---------- данные ----------
    @staticmethod
    def boosters(guild):
        return sorted([m for m in guild.premium_subscribers if not m.bot],
                      key=lambda m: m.premium_since or now())

    @staticmethod
    def next_goal(count):
        return next(((n, what) for n, what in GOALS if count < n), None)

    def panel_embed(self, guild):
        count = guild.premium_subscription_count or 0
        goal = self.next_goal(count)
        lines = [f"Бустов: **{count}** · уровень сервера: **{guild.premium_tier}**"]
        if goal:
            n, what = goal
            lines.append(f"`{bar(count, n)}` **{count}/{n}** — осталось **{n - count}**\n"
                         f"🎯 **Следующая цель:** {what}")
        else:
            keep = GOALS[-1][0]
            lines.append("🏆 **Максимальный, 3-й уровень!** Спасибо каждому, кто поддержал семью.\n"
                         f"Чтобы уровень не слетел, нужно держать не меньше **{keep}** бустов — "
                         f"сейчас запас: **{count - keep}**.")
        lines.append("")
        for n, what in GOALS:
            lines.append(f"{'✅' if count >= n else '🔒'} **{n}** — {what}")
        lines.append("\n**Бустерам от семьи:**\n"
                     "• личная роль со своим цветом и названием — `/буст роль`\n"
                     "• 💎 в семейном профиле\n• место в «Зале почёта» и запись в летописи")
        hall = self.boosters(guild)
        lines.append("\n**💎 Зал почёта:** " + (", ".join(m.mention for m in hall[:30]) or "пока пусто — стань первым!"))
        e = discord.Embed(title=f"💎 Поддержи семью {config.FAMILY}", description="\n".join(lines), color=BOOST_COLOR)
        e.set_footer(text="Бусты делают сервер красивее и удобнее для всей семьи")
        return e

    def view(self):
        v = discord.ui.View(timeout=None)
        v.add_item(HowToBoost())
        return v

    async def post_panels(self, guild, repost):
        await self.bot.panel_once(guild, "boosts", "boosts", repost,
                                  lambda: self._kw(guild), version=1)
        await self.refresh(guild)

    async def _kw(self, guild):
        return {"embed": self.panel_embed(guild), "view": self.view()}

    async def refresh(self, guild):
        """Обновить панель прогресса (без пересоздания)."""
        ch = self.bot.ch(guild, "boosts")
        mid = self.bot.db.get("panel:boosts")
        if not ch or not mid:
            return
        try:
            await ch.get_partial_message(int(mid)).edit(embed=self.panel_embed(guild), view=self.view())
        except (discord.HTTPException, ValueError):
            pass

    # ---------- события ----------
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.premium_since == after.premium_since or after.bot:
            return
        g = after.guild
        if after.premium_since and not before.premium_since:
            await self.thank(after)
        elif before.premium_since and not after.premium_since:
            await self.drop_role(after)
        await self.refresh(g)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if self.bot.db.get(f"boostrole:{member.id}"):
            await self.drop_role(member)  # ушёл с сервера — личная роль бустера не должна висеть
        if member.premium_since:
            await self.refresh(member.guild)

    async def thank(self, member):
        g = member.guild
        count = g.premium_subscription_count or 0
        goal = self.next_goal(count)
        left = f" До следующей цели осталось **{goal[0] - count}**: {goal[1]}." if goal else ""
        e = discord.Embed(title="💎 Спасибо за буст!",
                          description=f"{member.mention} поддержал(а) сервер семьи **{config.FAMILY}**!\n"
                                      f"Сейчас бустов: **{count}**.{left}", color=BOOST_COLOR)
        e.set_thumbnail(url=member.display_avatar.url)
        for key in ("boosts", "chat"):
            ch = self.bot.ch(g, key)
            if ch:
                try:
                    await ch.send(embed=e)
                except discord.HTTPException:
                    pass
        try:
            await member.send(f"💎 Спасибо, что забустил(а) сервер семьи {config.FAMILY}! Возьми личную роль со своим "
                              "цветом и названием: команда `/буст роль` на сервере.")
        except discord.HTTPException:
            pass
        chron = self.bot.get_cog("Chronicle")
        if chron:
            await chron.record(g, "boost", f"💎 {member.mention} поддержал(а) семью бустом сервера.", member.id)
            for n, what in GOALS:
                if count >= n and not self.bot.db.get(f"boostgoal:{n}"):
                    self.bot.db.set(f"boostgoal:{n}", 1)
                    await chron.record(g, "boost_goal", f"🎉 Семья собрала **{n}** бустов — открыто: {what}.")

    async def drop_role(self, member):
        rid = self.bot.db.get(f"boostrole:{member.id}")
        r = member.guild.get_role(int(rid)) if rid else None
        if r:
            try:
                await r.delete(reason="Буст закончился")
            except discord.HTTPException:
                pass
        self.bot.db.set(f"boostrole:{member.id}", "")

    @tasks.loop(hours=6)
    @guarded
    async def remind(self):
        for g in self.bot.home_guilds:
            count = g.premium_subscription_count or 0
            goal = self.next_goal(count)
            last = self.bot.db.get("boost:reminded")
            if not goal or (last and now().timestamp() - float(last) < REMIND_DAYS * 86400):
                continue
            ch = self.bot.ch(g, "chat")
            if not ch:
                continue
            self.bot.db.set("boost:reminded", now().timestamp())
            n, what = goal
            e = discord.Embed(title="💎 Немного не хватает",
                              description=f"У семьи **{count}** из **{n}** бустов. Ещё **{n - count}** — и откроем: "
                                          f"{what}.\n\nЕсли у тебя есть Nitro — в нём 2 бесплатных буста. Бустерам — "
                                          "личная роль со своим цветом и место в «Зале почёта».",
                              color=BOOST_COLOR)
            try:
                await ch.send(embed=e, view=self.view())
            except discord.HTTPException:
                pass

    @remind.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    group = app_commands.Group(name="буст", description="Бусты сервера семьи")

    @group.command(name="прогресс", description="Сколько бустов и что откроем дальше")
    async def progress(self, inter: discord.Interaction):
        await inter.response.send_message(embed=self.panel_embed(inter.guild), view=self.view(), ephemeral=True)

    @group.command(name="роль", description="Личная роль бустера: свой цвет и название")
    @app_commands.rename(color="цвет", name="название")
    @app_commands.describe(color="Цвет в формате #FF66CC", name="Название роли (до 30 символов)")
    async def role(self, inter: discord.Interaction, color: str, name: str = None):
        m = inter.user
        if not m.premium_since and not self.bot.has(m, config.LEAD):
            return await inter.response.send_message("💎 Личная роль — бонус для тех, кто бустит сервер. "
                                                     "`/буст прогресс` — как поддержать семью.", ephemeral=True)
        try:
            col = discord.Color(int(color.strip().lstrip("#"), 16))
        except ValueError:
            return await inter.response.send_message("Цвет нужен в формате #FF66CC.", ephemeral=True)
        title = f"💎 {(name or m.display_name).strip()[:30]}"
        g = inter.guild
        rid = self.bot.db.get(f"boostrole:{m.id}")
        r = g.get_role(int(rid)) if rid else None
        try:
            if r:
                await r.edit(name=title, color=col, reason="Бустер обновил роль")
            else:
                r = await g.create_role(name=title, color=col, hoist=False, mentionable=False,
                                        reason="Личная роль бустера")
                self.bot.db.set(f"boostrole:{m.id}", r.id)
                pos = max(1, g.me.top_role.position - 1)
                try:
                    await r.edit(position=pos)  # выше рангов — чтобы цвет было видно
                except discord.HTTPException:
                    pass
            if r not in m.roles:
                await m.add_roles(r, reason="Личная роль бустера")
        except discord.HTTPException:
            return await inter.response.send_message("Не получилось — проверь, что роль бота выше остальных.",
                                                     ephemeral=True)
        await inter.response.send_message(f"✅ Готово: {r.mention}. Спасибо за поддержку! 💎", ephemeral=True)


async def setup(bot):
    bot.add_dynamic_items(HowToBoost)
    await bot.add_cog(Boosts(bot))
