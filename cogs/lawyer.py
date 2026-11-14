# -*- coding: utf-8 -*-
"""ИИ-юрист: ответы по ООС-правилам проекта и IC-законам сервера, разбор ситуаций, составление документов.
Без ИИ-ключа работает как поиск по базе знаний с цитатами пунктов."""
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.ai import AIUnavailable
from core.kb import fetch_url, read_file

log = logging.getLogger("family-bot.lawyer")
KB_STAFF = config.LEAD + ["hr_head"]
CAT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in config.KB_CATEGORIES.items()]

BASE_PROMPT = (
    f"Ты — ИИ-юрист семьи «{config.FAMILY}» на RP-проекте «Россия Онлайн» (сервер {config.GAME_SERVER}). "
    "Отвечай по-русски, по делу. Опирайся ТОЛЬКО на документы ниже. Различай два мира:\n"
    "1) ООС-правила проекта — правила для игроков; нарушения наказывает администрация (бан, деморган, варн, мут);\n"
    "2) IC-законы сервера (Конституция, кодексы) — внутриигровое законодательство для отыгрыша (штрафы, сроки, суд).\n"
    "Всегда указывай источник: название документа и номер пункта/статьи, коротко цитируй ключевую фразу. "
    "Если ответа в документах нет — прямо скажи, что в базе этого нет, и посоветуй добавить документ. "
    "Никогда не выдумывай номера статей и наказания."
)
MODES = {
    "q": ("Вопрос", "Ответь на вопрос, до 10 предложений, с номерами пунктов."),
    "case": ("Разбор ситуации",
             "Разбери ситуацию пользователя. Структура ответа:\n⚖️ **Вывод** — кратко, кто прав/виноват\n"
             "📌 **Нормы** — какие пункты/статьи затронуты (с цитатами)\n🔨 **Возможные последствия**\n"
             "✅ **Что делать сейчас** — пошагово (жалоба, доказательства, сроки)."),
    "doc": ("Составить документ",
            "Составь документ по ситуации пользователя (исковое заявление, жалоба, заявление или ходатайство — выбери "
            "подходящее). Структура: шапка (кому / от кого — оставь [поля] для заполнения), суть обращения, ссылки на "
            "нормы с номерами, требования, перечень доказательств, дата и подпись. Официально-деловой стиль. Не "
            "выдумывай факты — где данных нет, пиши [заполнить]."),
}


def quote(text, n=700):
    text = text[:n] + ("…" if len(text) > n else "")
    return "\n".join("> " + ln for ln in text.splitlines() if ln.strip())


class TextDocModal(discord.ui.Modal, title="Добавить текст в базу знаний"):
    name = discord.ui.TextInput(label="Название документа", max_length=150)
    body = discord.ui.TextInput(label="Текст (можно вставить целиком)", style=discord.TextStyle.paragraph,
                                max_length=4000)

    def __init__(self, cat):
        super().__init__()
        self.cat = cat

    async def on_submit(self, inter: discord.Interaction):
        doc_id, _ = inter.client.kb.add(inter.guild.id, self.name.value, None, self.cat, self.body.value, inter.user.id)
        await inter.response.send_message(f"✅ Документ №{doc_id} «{self.name.value}» добавлен.", ephemeral=True)


class Lawyer(commands.Cog):
    kb = app_commands.Group(name="база", description="База знаний юриста: правила проекта, законы сервера", default_permissions=discord.Permissions(manage_messages=True))

    def __init__(self, bot):
        self.bot = bot
        self.cool = {}

    def cooldown_left(self, uid, sec=15):
        last = self.cool.get(uid, 0)
        left = sec - (time.monotonic() - last)
        if left > 0:
            return int(left) + 1
        self.cool[uid] = time.monotonic()
        return 0

    async def answer(self, gid, question, mode="q"):
        """→ (текст, источники, использован_ли_ИИ). gid — сервер семьи (её база знаний)."""
        bot = self.bot
        if not bot.ai.on:
            hits = bot.kb.search(gid, question, k=3)
            if not hits:
                return ("В базе знаний ничего не нашлось по этому вопросу. Руководство может добавить документы: "
                        "`/база правила-проекта`, `/база ссылка`."), [], False
            text = "🔎 Нашёл в правилах и законах (ИИ не подключён — показываю самые подходящие пункты):\n\n" + \
                   "\n\n".join(f"📄 **{h['title']}**\n{quote(h['text'])}" for h in hits)
            return text, [(h["title"], h["url"]) for h in hits], False
        context, sources, _ = bot.kb.context(gid, question)
        system = f"{BASE_PROMPT}\n\nЗАДАЧА: {MODES[mode][1]}\n\nДОКУМЕНТЫ:\n{context}"
        reply = await bot.ai.ask(system, question, max_tokens=1600 if mode == "doc" else 900, smart=True, cache=True)
        return reply or "ИИ не дал ответа, попробуй переформулировать.", sources, True

    async def send_answer(self, send, question, text, sources, mode, guild):
        title = f"⚖️ {MODES[mode][0]}: {question[:200]}"
        src = [f"[{t}]({u})" if u else t for t, u in sources[:6]]
        if len(text) > 3900:
            e = self.bot.embed(title, text[:1500] + "…\n\n📎 Полный текст — в файле.", config.COLOR, guild)
            if src:
                e.add_field(name="Источники", value="\n".join(src)[:1024], inline=False)
            await send(embed=e, file=discord.File(io.BytesIO(text.encode("utf-8")), filename="документ.txt"))
        else:
            e = self.bot.embed(title, text, config.COLOR, guild)
            if src:
                e.add_field(name="Источники", value="\n".join(src)[:1024], inline=False)
            e.set_footer(text=f"ИИ: {self.bot.ai.name} · проверяй важное по первоисточнику · {config.FOOTER}")
            await send(embed=e)

    # ---------- команды для всех ----------
    @app_commands.command(name="юрист", description="ИИ-юрист: вопрос, разбор ситуации или составление документа")
    @app_commands.rename(question="вопрос_или_ситуация", mode="режим")
    @app_commands.choices(mode=[app_commands.Choice(name=v[0], value=k) for k, v in MODES.items()])
    async def lawyer_cmd(self, inter: discord.Interaction, question: str, mode: app_commands.Choice[str] = None):
        mode = mode.value if mode else "q"
        left = self.cooldown_left(inter.user.id)
        if left:
            return await inter.response.send_message(f"⏳ Подожди {left} сек.", ephemeral=True)
        await inter.response.defer(thinking=True)
        try:
            text, sources, _ = await self.answer(inter.guild.id, question, mode)
        except AIUnavailable as e:
            text, sources = f"⚠️ {e}", []
        except Exception as e:
            log.exception("Юрист")
            text, sources = f"❌ Ошибка: {e}", []
        await self.send_answer(inter.followup.send, question, text, sources, mode, inter.guild)

    @app_commands.command(name="спросить", description="Спросить бота о правилах семьи, проекта и законах сервера")
    @app_commands.rename(question="вопрос")
    async def ask_cmd(self, inter: discord.Interaction, question: str):
        await self.lawyer_cmd.callback(self, inter, question, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        lawyer_ch = self.bot.ch(message.guild, "lawyer")
        in_channel = lawyer_ch and message.channel.id == lawyer_ch.id
        me = self.bot.user.id
        # только явное упоминание в тексте: ответ на сообщение бота (reply) — не вопрос юристу
        mentioned = (f"<@{me}>" in message.content or f"<@!{me}>" in message.content) and not message.mention_everyone
        if not (in_channel or mentioned):
            return
        asst = self.bot.get_cog("Assistant")
        if asst and asst.claims(message, in_lawyer_channel=bool(in_channel)):
            return  # это просьба помощнику «Швейцар» — он ответит сам, двух ответов не надо
        question = message.content.replace(self.bot.user.mention, "").replace(f"<@!{self.bot.user.id}>", "").strip()
        if len(question) < 3:
            return
        left = self.cooldown_left(message.author.id)
        if left:
            return await message.reply(f"⏳ Подожди {left} сек.", delete_after=8, mention_author=False)
        async with message.channel.typing():
            try:
                text, sources, _ = await self.answer(message.guild.id, question, "q")
            except AIUnavailable as e:
                text, sources = f"⚠️ {e}", []
            except Exception as e:
                log.exception("Юрист (канал)")
                text, sources = f"❌ Ошибка: {e}", []

        async def send(**kw):
            await message.reply(mention_author=False, **kw)
        await self.send_answer(send, question, text, sources, "q", message.guild)

    # ---------- база знаний ----------
    @kb.command(name="найти", description="Найти пункт в правилах и законах (работает без ИИ)")
    @app_commands.rename(query="запрос")
    async def kb_find(self, inter: discord.Interaction, query: str):
        hits = self.bot.kb.search(inter.guild.id, query, k=4)
        if not hits:
            return await inter.response.send_message("Ничего не нашлось.", ephemeral=True)
        e = self.bot.embed(f"🔎 {query[:200]}", None, config.COLOR, inter.guild)
        for h in hits:
            e.add_field(name=f"📄 {h['title'][:240]}", value=quote(h["text"], 900)[:1024], inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)

    @kb.command(name="правила-проекта", description="Загрузить основные правила проекта с форума (13 тем)")
    async def kb_project(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        ok, bad = [], []
        for i, url in enumerate(config.PROJECT_RULE_URLS, 1):
            try:
                title, content, url2 = await fetch_url(url)
                self.bot.kb.add(0, title, url2, "ooc", content, inter.user.id)  # правила проекта — общие для всех семей
                ok.append(f"✅ {title} ({len(content) // 1000} тыс. знаков)")
            except Exception as e:
                bad.append(f"❌ {url} — {e}")
            if i % 3 == 0:
                try:
                    await inter.edit_original_response(content=f"⏳ Загружаю… {i}/{len(config.PROJECT_RULE_URLS)}")
                except discord.HTTPException:
                    pass
        await inter.edit_original_response(content=None, embed=self.bot.embed(
            f"📘 Правила проекта: загружено {len(ok)}/{len(config.PROJECT_RULE_URLS)}", "\n".join(ok + bad)[:4000],
            config.GREEN if ok else config.RED, inter.guild))

    @kb.command(name="ссылка", description="Добавить документ по ссылке (тема форума, закон, кодекс)")
    @app_commands.rename(url="ссылка", cat="раздел", title="название")
    @app_commands.choices(cat=CAT_CHOICES)
    async def kb_url(self, inter: discord.Interaction, url: str, cat: app_commands.Choice[str], title: str = None):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            t, content, url2 = await fetch_url(url)
        except Exception as e:
            return await inter.followup.send(f"❌ Не получилось скачать: {e}\nМожно добавить текстом: `/база текст` "
                                             "или файлом: `/база файл`.", ephemeral=True)
        doc_id, new = self.bot.kb.add(inter.guild.id, title or t, url2, cat.value, content, inter.user.id)
        await inter.followup.send(f"✅ {'Добавлен' if new else 'Обновлён'} документ №{doc_id} «{title or t}» — "
                                  f"{len(content)} знаков.", ephemeral=True)

    @kb.command(name="файл", description="Добавить документ файлом (.txt, .md, .pdf, .html)")
    @app_commands.rename(file="файл", cat="раздел", title="название")
    @app_commands.choices(cat=CAT_CHOICES)
    async def kb_file(self, inter: discord.Interaction, file: discord.Attachment, cat: app_commands.Choice[str],
                      title: str = None):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        if file.size > 8 * 1024 * 1024:
            return await inter.response.send_message("Файл больше 8 МБ.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            content = read_file(file.filename, await file.read())
        except Exception as e:
            return await inter.followup.send(f"❌ Не смог прочитать файл: {e}", ephemeral=True)
        if len(content) < 20:
            return await inter.followup.send("В файле почти нет текста.", ephemeral=True)
        doc_id, _ = self.bot.kb.add(inter.guild.id, title or file.filename.rsplit(".", 1)[0], None, cat.value, content,
                                    inter.user.id)
        await inter.followup.send(f"✅ Документ №{doc_id} добавлен — {len(content)} знаков.", ephemeral=True)

    @kb.command(name="текст", description="Добавить документ, вставив текст")
    @app_commands.rename(cat="раздел")
    @app_commands.choices(cat=CAT_CHOICES)
    async def kb_text(self, inter: discord.Interaction, cat: app_commands.Choice[str]):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await inter.response.send_modal(TextDocModal(cat.value))

    @kb.command(name="список", description="Документы в базе знаний")
    async def kb_list(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT id, title, url, category, LENGTH(content) n, updated_at FROM kb_docs "
                               "WHERE guild_id IN (0, ?) ORDER BY category, id", inter.guild.id)
        lines = [f"`{r['id']}` {config.KB_CATEGORIES.get(r['category'], r['category'])[:2]} "
                 f"{'[' + r['title'][:70] + '](' + r['url'] + ')' if r['url'] else r['title'][:70]} · "
                 f"{r['n'] // 1000} тыс. · {r['updated_at'][:10]}" for r in rows]
        total = self.bot.kb.total_chars(inter.guild.id)
        await inter.response.send_message(embed=self.bot.embed(
            f"📚 База знаний: {len(rows)} док. + правила семьи", ("\n".join(lines) or "Пусто. Начни с `/база правила-проекта`")
            [:3800] + f"\n\nВсего ≈ {total // 1000} тыс. знаков · ИИ: {self.bot.ai.name}", config.COLOR, inter.guild),
            ephemeral=True)

    @kb.command(name="удалить", description="Удалить документ из базы")
    @app_commands.rename(doc_id="номер")
    async def kb_del(self, inter: discord.Interaction, doc_id: int):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        if not self.bot.db.one("SELECT 1 FROM kb_docs WHERE guild_id=? AND id=?", inter.guild.id, doc_id):
            return await inter.response.send_message("Нет такого документа (общие правила проекта удалить нельзя).",
                                                     ephemeral=True)
        self.bot.kb.delete(inter.guild.id, doc_id)
        await inter.response.send_message(f"🗑 Документ №{doc_id} удалён.", ephemeral=True)

    @kb.command(name="обновить", description="Заново скачать все документы со ссылками (если законы изменились)")
    async def kb_refresh(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, KB_STAFF):
            return await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        rows = self.bot.db.all("SELECT * FROM kb_docs WHERE guild_id IN (0, ?) AND url IS NOT NULL", inter.guild.id)
        ok, bad = 0, []
        for r in rows:
            try:
                t, content, url2 = await fetch_url(r["url"])
                self.bot.kb.add(r["guild_id"], r["title"], url2, r["category"], content, inter.user.id)
                ok += 1
            except Exception as e:
                bad.append(f"❌ №{r['id']} {r['title'][:60]} — {e}")
        await inter.followup.send(f"🔄 Обновлено: {ok}/{len(rows)}\n" + "\n".join(bad)[:1800], ephemeral=True)


async def setup(bot):
    await bot.add_cog(Lawyer(bot))
