# -*- coding: utf-8 -*-
"""/визитка — карточка семьи для пиара + готовый пост для форума в BB-коде."""
import asyncio
import io

import discord
from discord import app_commands
from discord.ext import commands

from core import config, images
from core.botcore import parse_dt

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь"]


def plural(n, one, few, many):
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


class ForumView(discord.ui.View):
    def __init__(self, bb):
        super().__init__(timeout=900)
        self.bb = bb

    @discord.ui.button(label="Пост для форума (BB-код)", emoji="📋", style=discord.ButtonStyle.secondary)
    async def forum(self, inter: discord.Interaction, _):
        f = discord.File(io.BytesIO(self.bb.encode("utf-8")), filename="post_dlya_foruma.txt")
        text = ("Скопируй текст и вставь на форум. Картинку визитки скачай и загрузи на форум через "
                "«Вставить изображение» — ссылки на картинки из Discord со временем перестают работать.")
        if len(self.bb) < 1800:
            text += f"\n```\n{self.bb}\n```"
        await inter.response.send_message(text, file=f, ephemeral=True)


def qr_png(url):
    """QR-код ссылки (PNG) — если стоит необязательная библиотека qrcode, иначе None."""
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(url, border=2, box_size=10)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Promo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def invite_url(self, guild):
        code = self.bot.db.gget(guild, "invite_code")
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            invites = []
        if code and any(i.code == code for i in invites):
            return f"https://discord.gg/{code}"
        forever = [i for i in invites if not i.max_age and not i.max_uses and not i.temporary]
        if forever:
            code = forever[0].code
        else:
            ch = self.bot.ch(guild, "welcome") or guild.system_channel or guild.text_channels[0]
            try:
                code = (await ch.create_invite(max_age=0, max_uses=0, unique=False, reason="Визитка семьи")).code
            except discord.HTTPException:
                return None
        self.bot.db.gset(guild, "invite_code", code)
        return f"https://discord.gg/{code}"

    def collect(self, guild):
        bot = self.bot
        ids = bot.role_ids(guild, config.FAMILY_RANKS)
        members = [m for m in guild.members if not m.bot and any(r.id in ids for r in m.roles)]
        lead_role = bot.role(guild, "leader")
        leader = next((m for m in lead_role.members if not m.bot), None) if lead_role else None
        since = bot.db.gget(guild, "family_since")
        if not since:
            d = guild.created_at
            since = f"{MONTHS[d.month - 1]} {d.year}"
        allies = [r["family"] for r in bot.db.all("SELECT family FROM relations WHERE guild_id=? AND "
                                                    "status IN ('ally','pact') ORDER BY status, family", guild.id)]
        return members, leader, since, allies

    def forum_post(self, guild, n, leader, since, allies, invite, recruiting):
        ally_txt = ", ".join(allies) if allies else "открыты к союзу"
        return (
            f"[CENTER][SIZE=7][B][COLOR=#7A1022]Ульяновская Династия[/COLOR][/B][/SIZE]\n"
            f"[SIZE=5]Семья {config.FAMILY} · сервер {config.GAME_SERVER}[/SIZE]\n"
            f"[I]Честь · Верность · Семья[/I][/CENTER]\n\n"
            f"[B][SIZE=5]О семье[/SIZE][/B]\n"
            f"[LIST]\n[*]В семье: [B]{n} {plural(n, 'человек', 'человека', 'человек')}[/B]\n"
            f"[*]С нами с: [B]{since}[/B]\n"
            f"[*]Глава: [B]{leader.display_name if leader else '—'}[/B]\n"
            f"[*]Союзники: [B]{ally_txt}[/B]\n[/LIST]\n\n"
            f"[B][SIZE=5]Что тебя ждёт[/SIZE][/B]\n"
            f"[LIST]\n[*]Личный наставник на испытательный срок — не бросим одного\n"
            f"[*]Регулярные сборы, мероприятия и совместная работа\n"
            f"[*]Помощь с законами сервера и правилами проекта — у нас свой ИИ-юрист\n"
            f"[*]Свой Discord: музыка, личные голосовые комнаты, семейный рынок\n"
            f"[*]Рост внутри семьи: от новичка до совета\n[/LIST]\n\n"
            f"[B][SIZE=5]Как вступить[/SIZE][/B]\n"
            f"[LIST=1]\n[*]Зайди в наш Discord: [URL={invite}]{invite.replace('https://', '')}[/URL]\n"
            f"[*]Прочитай правила и нажми «Подать заявку» в канале #подать-заявку\n"
            f"[*]Пройди короткий тест по правилам и собеседование\n[/LIST]\n\n"
            f"[CENTER][SIZE=6][B][URL={invite}]» {'ВСТУПИТЬ В СЕМЬЮ' if recruiting else 'НАШ DISCORD'} «[/URL]"
            f"[/B][/SIZE]\n"
            f"{'[COLOR=#2E7D32][B]Набор открыт[/B][/COLOR]' if recruiting else '[COLOR=#9E9E9E]Набор временно закрыт[/COLOR]'}"
            f"[/CENTER]"
        )

    @app_commands.command(name="визитка", description="Карточка семьи для пиара + пост для форума")
    @app_commands.rename(recruiting="набор_открыт", since="с_нами_с")
    @app_commands.describe(recruiting="Показать «Набор открыт» (по умолчанию да)",
                           since="Когда основана семья, например «март 2025» (сохранится; меняет только руководство)")
    async def card_cmd(self, inter: discord.Interaction, recruiting: bool = True, since: str = None):
        bot, g = self.bot, inter.guild
        if not bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("⛔ Визитка — для семьи.", ephemeral=True)
        if since:
            if not bot.has(inter.user, config.LEAD):
                return await inter.response.send_message("⛔ Дату основания меняет только руководство.", ephemeral=True)
            bot.db.gset(g, "family_since", since.strip()[:24])
        await inter.response.defer(thinking=True)
        members, leader, since_txt, allies = self.collect(g)
        invite = await self.invite_url(g) or ""
        n = len(members)
        ally_short = ", ".join(allies[:2]) + (f" +{len(allies) - 2}" if len(allies) > 2 else "") if allies else \
            "открыты к союзу"
        data = {"members": f"{n} {plural(n, 'человек', 'человека', 'человек')}", "since": since_txt,
                "leader": leader.display_name[:28] if leader else "—", "allies": ally_short,
                "invite": invite.replace("https://", ""), "recruiting": recruiting,
                "family": bot.family(g)["name"], "server": bot.family(g)["server"], "title": bot.family(g)["title"]}
        png = await asyncio.to_thread(images.family_card, data)
        bb = bot.brand(g, self.forum_post(g, n, leader, since_txt, allies, invite, recruiting))
        files = [discord.File(png, filename="vizitka.png")]
        qr = await asyncio.to_thread(qr_png, invite) if invite else None
        if qr:  # QR на приглашение — для скринов, стримов и листовок в игре
            files.append(discord.File(io.BytesIO(qr), filename="qr-priglashenie.png"))
        await inter.followup.send(
            content=f"🏛 **{bot.family(g)['title']}** — {'набор открыт, ' if recruiting else ''}вступить: {invite}",
            files=files, view=ForumView(bb))


async def setup(bot):
    await bot.add_cog(Promo(bot))
