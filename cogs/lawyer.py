# -*- coding: utf-8 -*-
"""ИИ-юрист: ответы по ООС-правилам проекта и IC-законам сервера, разбор ситуаций, составление документов.
Без ИИ-ключа работает как поиск по базе знаний с цитатами пунктов."""
import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.ai import AIUnavailable
from core.kb import FetchBlocked, LoginRequired, fetch_url, new_session, read_file
from core.forum import SECTIONS
from core.vision import read_images
from core.legal import SECTION_TEMPLATE, TEMPLATES, pick_template, verify

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
    "Никогда не выдумывай номера статей и наказания — каждую ссылку бот сверит с базой и пометит выдуманную. "
    "Если среди документов есть «Практика: …» (решения судов и ответы прокуратуры) — опирайся на них как на "
    "прецеденты: «в деле №… суд решил…», со ссылкой на тему."
)
CLARIFY = (
    "Тебе описали ситуацию для юридического разбора на RP-проекте. Хватает ли фактов, чтобы дать конкретный разбор: "
    "что именно произошло, когда (дата/время), кто участники (ники, должности), есть ли доказательства (видео, "
    "скриншоты, свидетели), о каком документе/суде речь? Если не хватает ВАЖНОГО — задай 1–3 коротких вопроса "
    "(по-русски, каждый до 100 знаков). Если фактов достаточно или это общий вопрос о законе — enough=true.\n"
    "Верни JSON: {\"enough\": true|false, \"questions\": [\"...\"]}")
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
            "выдумывай факты — где данных нет, пиши [заполнить]. Если ниже дан ОБРАЗЕЦ ФОРМЫ или ШАБЛОН — строго "
            "соблюдай его структуру и порядок полей, все поля [заполнить] сохрани."),
    "defense": ("Защита по делу",
                "Член семьи — ответчик/обвиняемый по делу, оно описано ниже в «ПОДРОБНО ПО ДЕЛУ». Подготовь защиту:\n"
                "🛡 **Позиция** — линия защиты, слабые места обвинения\n📌 **Нормы** — что применимо (с цитатами)\n"
                "🏛 **Практика** — похожие решения из базы, если есть\n🧾 **Доказательства** — что собрать (видео с "
                "таймкодами, скриншоты, свидетели, логи)\n⏰ **Сроки** — что и когда подать (отзыв, ходатайства, "
                "апелляция); если сроков нет в документах — так и скажи, не выдумывай\n✍️ **Следующий шаг** — "
                "одно конкретное действие сегодня."),
}
ASK_MODES = ("case", "doc", "defense")  # тут сначала проверяем, хватает ли фактов
DOC_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in TEMPLATES.items()]


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


class ClarifyModal(discord.ui.Modal, title="Уточнения для юриста"):
    def __init__(self, view):
        super().__init__()
        self.v = view
        self.fields = []
        for i, q in enumerate(view.questions[:3], 1):
            f = discord.ui.TextInput(label=f"{i}. {q}"[:45], placeholder=q[:100], style=discord.TextStyle.paragraph,
                                     required=False, max_length=600)
            self.add_item(f)
            self.fields.append((q, f))

    async def on_submit(self, inter: discord.Interaction):
        await self.v.run(inter, "\n".join(f"— {q} {f.value.strip()}" for q, f in self.fields if f.value.strip()))


class ClarifyView(discord.ui.View):
    def __init__(self, cog, author_id, gid, question, mode, template, questions):
        super().__init__(timeout=1800)
        self.cog, self.author_id, self.gid = cog, author_id, gid
        self.question, self.mode, self.template, self.questions = question, mode, template, questions

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Это не твой вопрос юристу.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ответить на вопросы", emoji="✍️", style=discord.ButtonStyle.primary)
    async def reply(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(ClarifyModal(self))

    @discord.ui.button(label="Разобрать как есть", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def as_is(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.run(inter, "")

    async def run(self, inter, extra):
        self.stop()
        await inter.response.defer(thinking=True)
        try:
            await inter.message.edit(view=None)
        except (discord.HTTPException, AttributeError):
            pass
        q = self.question + (f"\n\nУточнения:\n{extra}" if extra else "\n\nУточнения: нет, разбирай как есть")
        res = await self.cog.safe_full(self.gid, q, self.mode, member=inter.user, template=self.template)
        await self.cog.send_answer(inter.followup.send, self.question, res["text"], res["sources"], res["mode"],
                                   inter.guild)


class Lawyer(commands.Cog):
    kb = app_commands.Group(name="база", description="База знаний юриста: правила проекта, законы сервера", default_permissions=discord.Permissions(manage_messages=True))

    def __init__(self, bot):
        self.bot = bot
        self.cool = {}

    async def fetch(self, guild, url, session=None):
        """Документ по ссылке: как гость, а закрытую тему форума — аккаунтом семьи (/форум вход), если он есть."""
        try:
            return await fetch_url(url, session=session)
        except LoginRequired:
            fw = self.bot.get_cog("ForumWatch")
            got = await fw.fetch_doc(guild, url) if fw else None
            if not got:
                raise
            return got

    def cooldown_left(self, uid, sec=15):
        last = self.cool.get(uid, 0)
        left = sec - (time.monotonic() - last)
        if left > 0:
            return int(left) + 1
        self.cool[uid] = time.monotonic()
        return 0

    async def answer(self, gid, question, mode="q"):
        """→ (текст, источники, использован_ли_ИИ). gid — сервер семьи (её база знаний). Для других модулей."""
        res = await self.answer_full(gid, question, mode)
        return res["text"], res["sources"], res["ai"]

    def template_text(self, gid, question, key=None):
        """Образец документа: реальная форма из закреплённой темы форума (раздел «forms»), иначе встроенный шаблон."""
        key = key if key in TEMPLATES else pick_template(question)
        if not key:
            return ("ТИПЫ ДОКУМЕНТОВ: " + ", ".join(v[0] for v in TEMPLATES.values()) +
                    ". Выбери подходящий и соблюдай его обычную структуру.")
        name, body = TEMPLATES[key]
        sec = next((SECTIONS[f][0] for f, k in SECTION_TEMPLATE.items() if k == key), None)
        forms = [d for d in self.bot.kb.docs(gid, ["forms"]) if sec and sec.lower() in d["title"].lower()]
        if forms:
            return (f"ОБРАЗЕЦ ФОРМЫ С ФОРУМА ({forms[0]['title']}) — это официальный формат, соблюдай его:\n"
                    f"{forms[0]['content'][:3000]}")
        return f"ШАБЛОН «{name}» (структура форума; поля [..] сохрани, где нет данных):\n{body}"

    async def clarify(self, ai, question):
        """1–3 уточняющих вопроса, если для разбора не хватает фактов (иначе [])."""
        if "Уточнения:" in question or len(question) > 1500:
            return []
        try:
            data = await ai.ask_json(CLARIFY, question[:1500], max_tokens=250, light=True)
        except Exception as e:  # noqa: BLE001 — лимит/сеть: разбираем как есть
            log.info("Юрист: уточнения не получились (%s)", type(e).__name__)
            return []
        if not isinstance(data, dict) or data.get("enough", True) is not False:
            return []
        return [str(q).strip()[:100] for q in (data.get("questions") or []) if str(q).strip()][:3]

    def known_fields(self, gid, member):
        """Поля документа, которые бот знает сам (анкета, форум-список, профиль семьи) — вставить как есть.
        Ничего личного сверх игрового ника и статика."""
        lines = []
        if member is not None:
            r = self.bot.db.one("SELECT nick, static_id FROM applications WHERE guild_id=? AND user_id=? "
                                "AND COALESCE(nick, '')<>'' ORDER BY (status='accepted') DESC, id DESC LIMIT 1",
                                gid, member.id)
            p = self.bot.db.one("SELECT name, static_id FROM forum_people WHERE guild_id=? AND user_id=?", gid,
                                member.id) if r is None else None
            name = (r and r["nick"]) or (p and p["name"])
            static = (r and r["static_id"]) or (p and p["static_id"])
            if name:
                lines.append(f"Заявитель (от кого): {name}" + (f", static {static}" if static else ""))
        guild = self.bot.get_guild(gid)
        fam = self.bot.family(guild) if guild else {}
        if fam.get("server"):
            lines.append(f"Сервер: {fam['server']}")
        import datetime as _dt
        msk = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=3)
        lines.append("Дата: " + msk.strftime("%d.%m.%Y"))
        return ("ИЗВЕСТНЫЕ ПОЛЯ — вставь их в документ как есть; всё, чего нет здесь, в деле или в вопросе, оставь "
                "[заполнить]:\n" + "\n".join(lines)) if lines else ""

    async def answer_full(self, gid, question, mode="q", member=None, template=None, clarify=False, images=None,
                          case_id=None):
        """→ {text, sources, ai, mode, questions}. questions — не хватает фактов (тогда text пуст, спроси человека).
        member из семьи — юрист знает о делах против семьи (forumwatch); просят защиту по делу — режим defense.
        images — [(mime, bytes)] скрины жалобы/документа: читает Gemini или Claude; без зрения — только текст.
        case_id — дело из forum_cases (/юрист дело:): его карточка целиком идёт в запрос (поля документа из неё)."""
        bot = self.bot
        ai = bot.ai_for(gid)
        res = {"text": "", "sources": [], "ai": False, "mode": mode, "questions": []}
        if not ai.on:
            hits = bot.kb.search(gid, question, k=3)
            if not hits:
                res["text"] = ("В базе знаний ничего не нашлось по этому вопросу. Руководство может добавить документы: "
                               "`/база правила-проекта`, `/база ссылка`.")
                return res
            res["text"] = "🔎 Нашёл в правилах и законах (ИИ не подключён — показываю самые подходящие пункты):\n\n" + \
                "\n\n".join(f"📄 **{h['title']}**\n{quote(h['text'])}" for h in hits)
            res["sources"] = [(h["title"], h["url"]) for h in hits]
            return res
        fam, defense = "", False
        fw, guild = bot.get_cog("ForumWatch"), bot.get_guild(gid)
        if fw and guild and member is not None and bot.in_family(member) and bot.feature(guild, "forum"):
            try:
                fam, defense = fw.cases_context(guild, question)
            except Exception:  # noqa: BLE001 — без контекста дел юрист всё равно ответит
                log.exception("Юрист: контекст дел")
        card = fw.case_card(guild, case_id) if fw and guild and case_id else ""
        if card:
            fam = (fam + "\n\n" if fam else "") + card
            defense = defense or mode in ("q", "case")
        if defense and mode in ("q", "case"):
            mode = "defense"
        res["mode"] = mode
        images = images if images and ai.vision else None
        if clarify and mode in ASK_MODES and not images and not card:  # скрин/карточка дела — факты уже есть
            res["questions"] = await self.clarify(ai, question)
            if res["questions"]:
                return res
        tmpl = self.template_text(gid, question, template) if mode == "doc" else ""
        if mode == "doc":
            known = self.known_fields(gid, member)
            tmpl = (tmpl + "\n\n" + known).strip() if known else tmpl
        context, sources, _ = await bot.kb.context_smart(gid, question, ai)  # смысл + слова; без Gemini — по словам
        system = (f"{bot.brand(gid, BASE_PROMPT)}\n\nЗАДАЧА: {MODES[mode][1]}" + (f"\n\n{fam}" if fam else "") +
                  (f"\n\n{tmpl}" if tmpl else "") + f"\n\nДОКУМЕНТЫ:\n{context}")
        if images:
            system += "\n\nК вопросу приложены скриншоты — опирайся и на них (кто, против кого, суть, даты)."
        long = mode in ("doc", "defense")
        # Claude пишет документ целиком (дороже, но это и есть его работа); бесплатным — прежний объём
        tokens = (3500 if ai.is_claude else 1600) if long else 900
        reply = await ai.ask(system, question, max_tokens=tokens, smart=True, cache=True, images=images)
        text, missing = verify(reply or "ИИ не дал ответа, попробуй переформулировать.", bot.kb.docs(gid))
        if missing:  # самопроверка: ссылок, которых нет в базе, ИИ мог выдумать
            text += ("\n\n⚠️ Не нашёл в базе: " + ", ".join(missing[:6]) +
                     " — эти ссылки могут быть неточными, сверь с первоисточником.")
        res.update(text=text, sources=sources, ai=True)
        return res

    async def safe_full(self, gid, question, mode="q", **kw):
        try:
            return await self.answer_full(gid, question, mode, **kw)
        except AIUnavailable as e:
            return {"text": f"⚠️ {e}", "sources": [], "ai": False, "mode": mode, "questions": []}
        except Exception as e:  # noqa: BLE001
            log.exception("Юрист")
            return {"text": f"❌ Ошибка: {e}", "sources": [], "ai": False, "mode": mode, "questions": []}

    async def ask_back(self, send, question, res, author_id, gid, template=None):
        """Не хватает фактов — вопросы и кнопки «ответить» / «разобрать как есть»."""
        e = self.bot.embed(f"⚖️ {MODES[res['mode']][0]}: нужно пару уточнений",
                           "Чтобы разбор был по делу, ответь:\n" + "\n".join(f"**{i}.** {q}" for i, q in
                                                                          enumerate(res["questions"], 1)),
                           config.COLOR, self.bot.get_guild(gid))
        await send(embed=e, view=ClarifyView(self, author_id, gid, question, res["mode"], template, res["questions"]))

    async def send_answer(self, send, question, text, sources, mode, guild):
        title = f"⚖️ {MODES[mode][0]}: {question[:200]}"
        src = [f"[{t}]({u})" if u else t for t, u in sources[:6]]
        if mode == "doc" or len(text) > 3900:
            body = text if len(text) <= 3900 and mode != "doc" else text[:1500] + "…"
            e = self.bot.embed(title, body + "\n\n📎 Готовый текст к копированию — в файле.", config.COLOR, guild)
            if src:
                e.add_field(name="Источники", value="\n".join(src)[:1024], inline=False)
            await send(embed=e, file=discord.File(io.BytesIO(text.encode("utf-8")), filename="документ.txt"))
        else:
            e = self.bot.embed(title, text, config.COLOR, guild)
            if src:
                e.add_field(name="Источники", value="\n".join(src)[:1024], inline=False)
            e.set_footer(text=f"ИИ: {self.bot.ai_for(guild).name} · проверяй важное по первоисточнику · "
                              f"{self.bot.brand(guild, config.FOOTER)}")
            await send(embed=e)

    # ---------- команды для всех ----------
    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="юрист", description="ИИ-юрист: вопрос, разбор ситуации или составление документа")
    @app_commands.rename(question="вопрос_или_ситуация", mode="режим", doc="документ", case="дело")
    @app_commands.describe(doc="Какой документ составить (по формату форума): иск, апелляция, обращение…",
                           case="Дело с форума из списка семьи — юрист возьмёт стороны, номер, текст и решение")
    @app_commands.choices(mode=[app_commands.Choice(name=v[0], value=k) for k, v in MODES.items()], doc=DOC_CHOICES)
    async def lawyer_cmd(self, inter: discord.Interaction, question: str, mode: app_commands.Choice[str] = None,
                         doc: app_commands.Choice[str] = None, case: str = None):
        mode = "doc" if doc else (mode.value if mode else "q")
        template = doc.value if doc else None
        left = self.cooldown_left(inter.user.id)
        if left:
            return await inter.response.send_message(f"⏳ Подожди {left} сек.", ephemeral=True)
        await inter.response.defer(thinking=True)
        case_id = int(case) if case and str(case).isdigit() else None
        res = await self.safe_full(inter.guild.id, question, mode, member=inter.user, template=template, clarify=True,
                                   case_id=case_id)
        if res["questions"]:
            return await self.ask_back(inter.followup.send, question, res, inter.user.id, inter.guild.id, template)
        await self.send_answer(inter.followup.send, question, res["text"], res["sources"], res["mode"], inter.guild)

    @lawyer_cmd.autocomplete("case")
    async def case_ac(self, inter: discord.Interaction, current: str):
        cur = (current or "").lower()
        rows = self.bot.db.all("SELECT id, title, defendant, case_no FROM forum_cases WHERE guild_id=? "
                               "ORDER BY (status='pending') DESC, updated_ts DESC LIMIT 60", inter.guild.id)
        out = []
        for r in rows:
            name = f"{r['title'] or 'дело'} — {r['defendant'] or '?'}"
            if cur and cur not in name.lower() and cur != str(r["case_no"] or ""):
                continue
            out.append(app_commands.Choice(name=name[:100], value=str(r["id"])))
        return out[:25]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not (message.content or message.attachments):
            return
        if not self.bot.feature(message.guild, "lawyer:channel"):
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
        images = await read_images(message) if self.bot.ai_for(message.guild).vision else []
        if images and len(question) < 3:
            question = "Разбери скриншот: что это за документ/жалоба, кто против кого, суть, что делать."
        if len(question) < 3:
            if message.attachments and not images and in_channel:
                await message.reply("Картинки мой ИИ сейчас не читает — пришли суть текстом.", mention_author=False)
            return
        left = self.cooldown_left(message.author.id)
        if left:
            return await message.reply(f"⏳ Подожди {left} сек.", delete_after=8, mention_author=False)
        mode = "case" if len(question) >= 150 else "q"  # длинное сообщение — это описание ситуации

        async def send(**kw):
            await message.reply(mention_author=False, **kw)
        async with message.channel.typing():
            res = await self.safe_full(message.guild.id, question, mode, member=message.author, clarify=True,
                                       images=images or None)
        if res["questions"]:
            return await self.ask_back(send, question, res, message.author.id, message.guild.id)
        await self.send_answer(send, question, res["text"], res["sources"], res["mode"], message.guild)

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
            t, content, url2 = await self.fetch(inter.guild, url)
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
            [:3800] + f"\n\nВсего ≈ {total // 1000} тыс. знаков · ИИ: {self.bot.ai_for(inter.guild).name}", config.COLOR, inter.guild),
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
        ok, bad, later = 0, [], []
        progress = None
        async with new_session() as s:  # одна сессия на прогон: общие куки, keep-alive
            for i, r in enumerate(rows, 1):
                if i > 1:
                    await asyncio.sleep(2)  # вежливо, чтобы форум не включал анти-флуд
                try:
                    t, content, url2 = await self.fetch(inter.guild, r["url"], session=s)
                    # на месте, по номеру: ссылка закрытой темы приходит в коротком виде — по ней был бы дубль
                    self.bot.db.q("UPDATE kb_docs SET content=?, updated_at=? WHERE id=?", content,
                                  discord.utils.utcnow().isoformat(), r["id"])
                    self.bot.kb.invalidate()
                    ok += 1
                except FetchBlocked:
                    later.append(f"⏳ №{r['id']} {r['title'][:60]} — временно недоступен, попробуй позже")
                except Exception as e:
                    bad.append(f"❌ №{r['id']} {r['title'][:60]} — {e}")
                if i % 5 == 0 and i < len(rows):
                    text = f"⏳ Обновляю базу: {i}/{len(rows)} (готово {ok}, ошибок {len(bad) + len(later)})…"
                    try:
                        if progress is None:
                            progress = await inter.followup.send(text, ephemeral=True, wait=True)
                        else:
                            await progress.edit(content=text)
                    except discord.HTTPException:
                        pass
        report = f"🔄 Обновлено: {ok}/{len(rows)}\n" + "\n".join(bad + later)
        if progress is not None:
            try:
                return await progress.edit(content=report[:1900])
            except discord.HTTPException:
                pass
        await inter.followup.send(report[:1900], ephemeral=True)


async def setup(bot):
    await bot.add_cog(Lawyer(bot))
