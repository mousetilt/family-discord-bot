# -*- coding: utf-8 -*-
"""Обращения к руководству: кнопка в 📬・руководству → приватная ветка «автор + руководство».
Анонимно: текст уходит в совет без имени, руководство отвечает через бота — автор получает ответ в ЛС."""
import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import now, now_iso, parse_dt, safe_dm

KINDS = {
    "complaint": ("⚠️", "Жалоба", discord.ButtonStyle.danger, "На кого и за что? Что произошло, когда, есть ли доказательства"),
    "question": ("❓", "Вопрос", discord.ButtonStyle.primary, "Что хочешь узнать у руководства?"),
    "request": ("🙏", "Просьба", discord.ButtonStyle.secondary, "О чём просишь: отпуск, перевод, помощь, повышение…"),
    "idea": ("💡", "Предложение", discord.ButtonStyle.success, "Что предлагаешь изменить или добавить в семье?"),
    "anon": ("🕶", "Анонимно", discord.ButtonStyle.secondary, "Руководство не увидит, кто ты. Ответ придёт в ЛС от бота"),
}
COOLDOWN = 600


def lead_mentions(bot, guild):
    return " ".join(r.mention for r in (bot.role(guild, k) for k in ("leader", "deputy", "council")) if r)


class AppealModal(discord.ui.Modal):
    def __init__(self, kind):
        emoji, name, _, hint = KINDS[kind]
        super().__init__(title=f"{emoji} {name} — руководству семьи"[:45])
        self.kind = kind
        self.topic = discord.ui.TextInput(label="Тема", max_length=80, placeholder="Коротко, в двух-трёх словах")
        self.text = discord.ui.TextInput(label="Подробно", style=discord.TextStyle.paragraph, max_length=1800,
                                         placeholder=hint[:100])
        self.add_item(self.topic)
        self.add_item(self.text)

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Appeals").create(inter, self.kind, str(self.topic), str(self.text))


class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"appeal:new:(?P<kind>[a-z]+)"):
    def __init__(self, kind):
        emoji, name, style, _ = KINDS[kind]
        super().__init__(discord.ui.Button(label=name, emoji=emoji, style=style, custom_id=f"appeal:new:{kind}"))
        self.kind = kind

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["kind"]) if match["kind"] in KINDS else None

    async def callback(self, inter: discord.Interaction):
        last = inter.client.db.one("SELECT created_at FROM appeals WHERE guild_id=? AND user_id=? ORDER BY id DESC",
                                   inter.guild.id, inter.user.id)
        if last and (now() - parse_dt(last["created_at"])).total_seconds() < COOLDOWN:
            return await inter.response.send_message("⏳ Новое обращение можно отправить через 10 минут после "
                                                     "предыдущего. Дополнить старое — напиши в его ветку.",
                                                     ephemeral=True)
        await inter.response.send_modal(AppealModal(self.kind))


class AppealAction(discord.ui.DynamicItem[discord.ui.Button], template=r"appeal:(?P<a>close|reply|reopen):(?P<id>\d+)"):
    LABELS = {"close": ("Закрыть", "✅", discord.ButtonStyle.success),
              "reply": ("Ответить автору", "✉️", discord.ButtonStyle.primary),
              "reopen": ("Открыть снова", "↩️", discord.ButtonStyle.secondary)}

    def __init__(self, a, aid):
        label, emoji, style = self.LABELS[a]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"appeal:{a}:{aid}"))
        self.a, self.aid = a, aid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], int(match["id"]))

    async def callback(self, inter: discord.Interaction):
        bot = inter.client
        if not bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        row = bot.db.one("SELECT * FROM appeals WHERE id=?", self.aid)
        if not row:
            return await inter.response.send_message("Обращение не найдено.", ephemeral=True)
        cog = bot.get_cog("Appeals")
        if self.a == "reply":
            return await inter.response.send_modal(ReplyModal(self.aid))
        if self.a == "close":
            await cog.close(inter, row)
        else:
            bot.db.q("UPDATE appeals SET status='open', closed_by=NULL, closed_at=NULL WHERE id=?", self.aid)
            await inter.response.edit_message(view=cog.controls(row, closed=False))
            if row["thread_id"]:
                th = inter.guild.get_thread(row["thread_id"])
                if th:
                    try:
                        await th.edit(archived=False, locked=False)
                    except discord.HTTPException:
                        pass


class ReplyModal(discord.ui.Modal, title="✉️ Ответ на анонимное обращение"):
    text = discord.ui.TextInput(label="Ответ (придёт автору в ЛС от бота)", style=discord.TextStyle.paragraph,
                                max_length=1800)

    def __init__(self, aid):
        super().__init__()
        self.aid = aid

    async def on_submit(self, inter: discord.Interaction):
        bot = inter.client
        row = bot.db.one("SELECT * FROM appeals WHERE id=?", self.aid)
        if not row:
            return await inter.response.send_message("Обращение не найдено.", ephemeral=True)
        user = bot.get_user(row["user_id"])
        if not user:
            try:
                user = await bot.fetch_user(row["user_id"])
            except (discord.HTTPException, TypeError):
                user = None
        e = bot.embed(f"✉️ Ответ руководства на твоё обращение №{self.aid}",
                      f"**Тема:** {row['topic']}\n\n{self.text}", config.COLOR, inter.guild)
        e.set_footer(text=f"Семья {config.FAMILY} · ответ от руководства (кто именно — не указывается)")
        ok = await safe_dm(user, embed=e) if user else False
        await inter.response.send_message(
            embed=bot.embed(None, f"✉️ **Ответ отправлен** ({inter.user.mention}):\n>>> {self.text}", config.GREEN)
            if ok else bot.embed(None, "⚠️ Не удалось доставить: у автора закрыты ЛС.", 0xFB8C00))


class Appeals(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def controls(self, row, closed):
        v = discord.ui.View(timeout=None)
        if row["anon"] and not closed:
            v.add_item(AppealAction("reply", row["id"]))
        v.add_item(AppealAction("reopen" if closed else "close", row["id"]))
        return v

    def card(self, guild, row, author=None):
        emoji, name, *_ = KINDS[row["kind"]]
        who = "🕶 **Аноним** — имя скрыто даже от руководства" if row["anon"] else f"**От:** {author.mention}"
        e = self.bot.embed(f"{emoji} {name} №{row['id']} · {row['topic']}",
                           f"{who}\n\n>>> {row['text']}", 0x546E7A if row["anon"] else config.COLOR, guild)
        e.timestamp = parse_dt(row["created_at"])
        return e

    async def create(self, inter, kind, topic, text):
        bot, g, user = self.bot, inter.guild, inter.user
        anon = kind == "anon"
        await inter.response.defer(ephemeral=True, thinking=True)
        cur = bot.db.q("INSERT INTO appeals(guild_id, user_id, kind, anon, topic, text, created_at) VALUES(?,?,?,?,?,?,?)",
                       g.id, user.id, kind, int(anon), topic[:80], text[:1800], now_iso())
        row = bot.db.one("SELECT * FROM appeals WHERE id=?", cur.lastrowid)
        if anon:
            desk = bot.ch(g, "council_chat")
            if not desk:
                return await inter.followup.send("❌ Канал совета не найден — пусть руководство запустит /настройка.",
                                                 ephemeral=True)
            msg = await desk.send(content=lead_mentions(bot, g), embed=self.card(g, row),
                                  view=self.controls(row, False), allowed_mentions=discord.AllowedMentions(roles=True))
            bot.db.q("UPDATE appeals SET message_id=? WHERE id=?", msg.id, row["id"])
            return await inter.followup.send(f"🕶 Обращение №{row['id']} отправлено анонимно. Руководство не видит, кто "
                                             "ты. Если ответят — ответ придёт в ЛС от бота.", ephemeral=True)
        base = bot.ch(g, "appeals") or inter.channel
        try:
            th = await base.create_thread(name=f"{KINDS[kind][0]} №{row['id']} · {topic}"[:100],
                                          type=discord.ChannelType.private_thread, invitable=False,
                                          auto_archive_duration=10080)
            await th.add_user(user)
        except discord.HTTPException:
            return await inter.followup.send("❌ Не получилось создать ветку. Попроси руководство проверить права бота.",
                                             ephemeral=True)
        msg = await th.send(content=f"{user.mention} {lead_mentions(bot, g)}", embed=self.card(g, row, user),
                            view=self.controls(row, False),
                            allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        await th.send("Эту ветку видишь только ты и руководство. Можно дописать подробности и приложить скриншоты.")
        bot.db.q("UPDATE appeals SET thread_id=?, message_id=? WHERE id=?", th.id, msg.id, row["id"])
        await bot.log(g, f"📬 Обращение №{row['id']} ({KINDS[kind][1].lower()}) от {user.mention}: {th.mention}")
        await inter.followup.send(f"✅ Обращение №{row['id']} создано: {th.mention}", ephemeral=True)

    async def close(self, inter, row):
        bot = self.bot
        bot.db.q("UPDATE appeals SET status='closed', closed_by=?, closed_at=? WHERE id=?",
                 inter.user.id, now_iso(), row["id"])
        await inter.response.edit_message(view=self.controls(row, closed=True))
        note = f"✅ Обращение закрыл(а) {inter.user.mention}."
        if row["anon"]:
            await inter.followup.send(note)
            return
        th = inter.guild.get_thread(row["thread_id"]) if row["thread_id"] else None
        if th:
            try:
                await th.send(note + " Если вопрос не решён — создай новое обращение.")
                await th.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
        author = inter.guild.get_member(row["user_id"])
        if author:
            await safe_dm(author, f"✅ Твоё обращение №{row['id']} «{row['topic']}» рассмотрено и закрыто руководством "
                                  f"семьи {config.FAMILY}.")

    async def post_panels(self, guild, repost):
        bot = self.bot

        async def panel():
            v = discord.ui.View(timeout=None)
            for k in KINDS:
                v.add_item(AppealButton(k))
            e = bot.embed("📬 Написать руководству",
                          "Есть жалоба, вопрос, просьба или идея — нажми кнопку и опиши.\n\n"
                          "⚠️ **Жалоба** — на участника, ситуацию, нарушение\n"
                          "❓ **Вопрос** — что угодно к Главе и совету\n"
                          "🙏 **Просьба** — отпуск, перевод, помощь, повышение\n"
                          "💡 **Предложение** — как сделать семью лучше\n"
                          "🕶 **Анонимно** — руководство не узнает, кто написал. Ответ придёт в ЛС от бота\n\n"
                          "Бот создаст **приватную ветку**: её видишь только ты и руководство. "
                          "Ничего не потеряется в личках — каждое обращение под номером.",
                          config.COLOR, guild)
            return {"embed": e, "view": v}

        await bot.panel_once(guild, "appeals", "appeals", repost, panel, version=1)

    @app_commands.command(name="обращения", description="Открытые обращения к руководству")
    @app_commands.default_permissions(moderate_members=True)
    async def list_cmd(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        rows = self.bot.db.all("SELECT * FROM appeals WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 25",
                               inter.guild.id)
        lines = []
        for r in rows:
            emoji, name, *_ = KINDS[r["kind"]]
            where = f"<#{r['thread_id']}>" if r["thread_id"] else "в совете (анонимно)"
            lines.append(f"{emoji} **№{r['id']}** {r['topic']} — {where} · <t:{int(parse_dt(r['created_at']).timestamp())}:R>")
        total = self.bot.db.one("SELECT COUNT(*) n FROM appeals WHERE guild_id=?", inter.guild.id)["n"]
        await inter.response.send_message(embed=self.bot.embed(
            f"📬 Открытые обращения: {len(rows)}", "\n".join(lines) or "Всё разобрано 👌",
            config.COLOR, inter.guild).set_footer(text=f"Всего обращений за всё время: {total}"), ephemeral=True)


async def setup(bot):
    bot.add_dynamic_items(AppealButton, AppealAction)
    await bot.add_cog(Appeals(bot))
