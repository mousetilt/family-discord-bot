# -*- coding: utf-8 -*-
"""ИИ и автоматизация (Фаза 4). Модуль «auto» в /настройки, действия:
• search  — поиск в интернете: Gemini с поиском Google (бесплатно, на ключе Gemini семьи), запасной —
            официальное API DuckDuckGo. Сайты не скрейпим и антибот-защиту не обходим. Речь: web_search {"query"}.
• missed  — «что я пропустил»: ИИ-сводка видимых человеку каналов семьи за N часов. /пропустил, речь missed.
• drafts  — ИИ-черновики: анонс, ивент, пиар-пост, набор, поздравление. /черновик → опубликовать / ещё / запланировать.
• schedule — отложенные объявления (таблица scheduled, время по Москве). /запланировать, /запланированные,
            речь announce {"at": "в 20:00"}.
• relay   — ретранслятор (по умолчанию выкл): канал-источник (куда приходят анонсы через Discord «Подписаться»)
            → ИИ вытаскивает суть → пост в канал новостей сразу или черновиком на подтверждение (relay:*)."""
import datetime as dt
import json
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, staff_only

log = logging.getLogger("family-bot.auto")
UA = {"User-Agent": "DiscordFamilyBot/1.0 (self-hosted Discord bot; aiohttp)"}  # честно: мы бот
MSK = dt.timezone(dt.timedelta(hours=config.GATHER_UTC_OFFSET))
POSTERS = config.LEAD + ["events"]  # публикуют объявления и черновики
DRAFT_KINDS = {"announce": "объявление для семьи", "event": "описание ивента/мероприятия",
               "pr": "пиар-пост семьи для других серверов (набор, кто мы такие)",
               "recruit": "объявление о наборе в семью", "congrats": "поздравление"}
MISSED_CHANNELS = ["news", "chat", "gather", "ideas", "achievements", "flood", "treaties", "market", "market_chat",
                   "friends_chat", "guides", "newbie_q"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, author_id INTEGER, title TEXT, text TEXT,
    ping_all INTEGER DEFAULT 0, voice INTEGER DEFAULT 0, run_at INTEGER NOT NULL, status TEXT DEFAULT 'wait',
    created INTEGER);
CREATE INDEX IF NOT EXISTS ix_scheduled ON scheduled(status, run_at);
CREATE TABLE IF NOT EXISTS reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL, channel_id INTEGER,
    user_id INTEGER, text TEXT, run_at INTEGER NOT NULL, status TEXT DEFAULT 'wait');
CREATE INDEX IF NOT EXISTS ix_reminders ON reminders(status, run_at);
CREATE TABLE IF NOT EXISTS relay_posts(
    guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, title TEXT, text TEXT, status TEXT, ts INTEGER,
    PRIMARY KEY(guild_id, message_id));
"""


# ─────────────────────────────── время «по-человечески» ───────────────────────────────
def parse_when(text, base=None):
    """«20:00», «завтра 18:30», «через 2 часа», «25.11 19:00», «в 9» (по Москве) → unix-время в будущем или None."""
    s = " ".join(str(text or "").lower().replace("ё", "е").split())
    base = base or dt.datetime.now(MSK)
    m = re.search(r"через\s+(\d+)?\s*(мин|час|ден|дня|дней|сут)", s)
    if m:
        n = int(m.group(1) or 1)
        unit = {"мин": "minutes", "час": "hours"}.get(m.group(2), "days")
        t = base + dt.timedelta(**{unit: n})
    else:
        tm = re.search(r"(\d{1,2}):(\d{2})", s)
        date = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", s)
        if date and not tm and not date.group(3):  # «20.30» без другого времени — это время, не дата
            tm, date = date, None
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
        else:
            h = re.search(r"\bв\s+(\d{1,2})\b", s)
            if not h:
                return None
            hh, mm = int(h.group(1)), 0
        if hh > 23 or mm > 59:
            return None
        try:
            if date:
                y = date.group(3)
                y = (int(y) + 2000 if len(y) == 2 else int(y)) if y else base.year
                t = dt.datetime(y, int(date.group(2)), int(date.group(1)), hh, mm, tzinfo=MSK)
                if not date.group(3) and t <= base:
                    t = t.replace(year=t.year + 1)
            else:
                day = 2 if "послезавтра" in s else 1 if "завтра" in s else 0
                t = (base + dt.timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if day == 0 and t <= base:
                    t += dt.timedelta(days=1)  # «в 20:00», а уже 21:00 — значит завтра
        except ValueError:
            return None
    if t <= base or t - base > dt.timedelta(days=60):
        return None
    return int(t.timestamp())


# ─────────────────────────────── поиск ───────────────────────────────
async def web_search(query, n=5):
    """Запасной поиск без ключей — официальное API мгновенных ответов DuckDuckGo: [{title, url, snippet}].
    Хорошо находит конкретные названия (город, игра, человек), на вопросы целиком отвечает редко."""
    q = str(query or "").strip()[:200]
    if not q:
        return []
    out = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=UA) as s:
            async with s.get("https://api.duckduckgo.com/", params={"q": q, "format": "json", "no_html": 1,
                                                                     "skip_disambig": 1, "kl": "ru-ru"}) as r:
                d = json.loads(await r.text()) if r.status in (200, 202) else {}
    except Exception as e:
        log.warning("Поиск DuckDuckGo: %s", type(e).__name__)
        return []
    if d.get("AbstractText"):
        out.append({"title": d.get("Heading") or q, "url": d.get("AbstractURL") or "", "snippet": d["AbstractText"][:600]})
    if d.get("Answer"):
        out.append({"title": "Ответ", "url": "", "snippet": str(d["Answer"])[:300]})
    for t in d.get("RelatedTopics") or []:
        if isinstance(t, dict) and t.get("Text"):
            out.append({"title": t["Text"][:80], "url": t.get("FirstURL") or "", "snippet": t["Text"][:300]})
        if len(out) >= n:
            break
    return out


# ─────────────────────────────── UI ───────────────────────────────
class WhenModal(discord.ui.Modal, title="Когда опубликовать?"):
    when = discord.ui.TextInput(label="Когда (по Москве)", placeholder="20:00 · завтра 18:30 · через 2 часа · 25.11 19:00",
                                max_length=40)

    def __init__(self, cog, title_, text, ping_all=False, voice=False):
        super().__init__()
        self.cog, self.title_, self.text, self.ping_all, self.voice = cog, title_, text, ping_all, voice

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.send_message(
            self.cog.schedule(inter.guild, inter.user, self.title_, self.text, self.when.value, self.ping_all,
                              self.voice), ephemeral=True)


class AnnounceLaterModal(discord.ui.Modal, title="Отложенное объявление"):
    head = discord.ui.TextInput(label="Заголовок", max_length=200)
    body = discord.ui.TextInput(label="Текст", style=discord.TextStyle.paragraph, max_length=3500)

    def __init__(self, cog, when, ping_all, voice):
        super().__init__()
        self.cog, self.when, self.ping_all, self.voice = cog, when, ping_all, voice

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.send_message(
            self.cog.schedule(inter.guild, inter.user, self.head.value, self.body.value, self.when, self.ping_all,
                              self.voice), ephemeral=True)


class DraftView(discord.ui.View):
    def __init__(self, cog, author_id, kind, topic, title_, text):
        super().__init__(timeout=900)
        self.cog, self.author_id, self.kind, self.topic, self.title_, self.text = cog, author_id, kind, topic, title_, text

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Это не твой черновик.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Опубликовать", emoji="📢", style=discord.ButtonStyle.success)
    async def pub(self, inter: discord.Interaction, _):
        if not inter.client.has(inter.user, POSTERS):
            return await inter.response.send_message("⛔ Публикуют Глава, зам, совет и организаторы.", ephemeral=True)
        c = await self.cog.post_announce(inter.guild, inter.user, self.title_, self.text)
        self.stop()
        await inter.response.edit_message(content=f"📢 Опубликовано в {c.mention}." if c else "❌ Нет канала объявлений.",
                                          view=None)

    @discord.ui.button(label="Другой вариант", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def again(self, inter: discord.Interaction, _):
        await inter.response.defer()
        title_, text = await self.cog.draft(inter.guild, self.kind, self.topic)
        if not text:
            return await inter.followup.send(title_ or "ИИ не ответил.", ephemeral=True)
        self.title_, self.text = title_, text
        await inter.edit_original_response(embed=self.cog.draft_embed(inter.guild, title_, text), view=self)

    @discord.ui.button(label="Запланировать", emoji="⏰", style=discord.ButtonStyle.primary)
    async def later(self, inter: discord.Interaction, _):
        if not inter.client.has(inter.user, POSTERS):
            return await inter.response.send_message("⛔ Публикуют Глава, зам, совет и организаторы.", ephemeral=True)
        await inter.response.send_modal(WhenModal(self.cog, self.title_, self.text))


class RelayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"relay:(?P<a>pub|skip):(?P<m>\d+)"):
    def __init__(self, a, mid):
        super().__init__(discord.ui.Button(label="Опубликовать" if a == "pub" else "Пропустить",
                                           emoji="📰" if a == "pub" else "✖️",
                                           style=discord.ButtonStyle.success if a == "pub" else discord.ButtonStyle.secondary,
                                           custom_id=f"relay:{a}:{mid}"))
        self.a, self.mid = a, mid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], int(match["m"]))

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Automation").relay_decide(inter, self.a, self.mid)


class CancelSelect(discord.ui.Select):
    def __init__(self, cog, rows):
        self.cog = cog
        super().__init__(placeholder="Отменить объявление…", options=[
            discord.SelectOption(label=f"№{r['id']} · {r['title'][:80]}", value=str(r["id"]),
                                 description=dt.datetime.fromtimestamp(r["run_at"], MSK).strftime("%d.%m %H:%M МСК"))
            for r in rows[:25]])

    async def callback(self, inter: discord.Interaction):
        rid = int(self.values[0])
        r = self.cog.bot.db.one("SELECT * FROM scheduled WHERE id=? AND guild_id=?", rid, inter.guild.id)
        if not r or r["status"] != "wait":
            return await inter.response.send_message("Уже не в очереди.", ephemeral=True)
        if r["author_id"] != inter.user.id and not inter.client.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Отменить может автор или руководство.", ephemeral=True)
        self.cog.bot.db.q("UPDATE scheduled SET status='cancel' WHERE id=?", rid)
        await inter.response.edit_message(content=f"🗑 Объявление №{rid} отменено.", view=None)


# ─────────────────────────────── ког ───────────────────────────────
class Automation(commands.Cog):
    relay = app_commands.Group(name="ретранслятор", description="Внешние анонсы → коротко и по делу в новости семьи",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()
        self._search_last = {}
        self.sched_loop.start()

    def cog_unload(self):
        self.sched_loop.cancel()

    def on(self, guild, act):
        return self.bot.feature(guild, f"auto:{act}")

    async def gate(self, inter, act):
        if self.on(inter.guild, act):
            return True
        await inter.response.send_message("⚫ Эта функция у семьи выключена — `/настройки` → «Автоматизация».",
                                          ephemeral=True)
        return False

    # ---------- поиск ----------
    async def search_answer(self, guild, user_id, query):
        if not self.on(guild, "search"):
            return "🔎 Поиск в интернете у семьи выключен (`/настройки` → «Автоматизация»)."
        query = str(query or "").strip()[:200]
        if not query:
            return "🔎 А искать-то что?"
        if time.monotonic() - self._search_last.get(user_id, 0) < 15:
            return "🔎 Не так часто — дай передохнуть, секунд 15."
        self._search_last[user_id] = time.monotonic()
        ai = self.bot.ai_for(guild)
        system = self.bot.brand(guild, f"Ты Швейцар — помощник RP-семьи «{config.FAMILY}». Найди в интернете и ответь "
                                       "коротко (до 5 предложений), по-русски, в своём дерзком стиле, но точно, со "
                                       "свежими фактами. Не нашёл — так и скажи, не выдумывай.")
        # 1) Gemini + поиск Google — бесплатно на ключе Gemini семьи
        if ai.keys.get("gemini"):
            try:
                text, src = await ai.grounded(system, query, max_tokens=450)
                seen, links = set(), []
                for title_, url in src:
                    if url and title_ not in seen:
                        seen.add(title_)
                        links.append(f"[{title_[:40] or 'источник'}](<{url}>)")
                return text[:1600] + (f"\n\n🔎 {' · '.join(links[:3])}" if links else "")
            except Exception as e:
                log.warning("Поиск Gemini: %s", str(e)[:150])
        # 2) запасной — DuckDuckGo (мгновенные ответы)
        results = await web_search(query)
        if results and ai.on:
            try:
                text = await ai.ask(system + " Отвечай только по этим найденным данным.",
                                    json.dumps({"вопрос": query, "найдено": results}, ensure_ascii=False), max_tokens=450)
                url = next((r["url"] for r in results if r["url"]), "")
                return text[:1600] + (f"\n\n🔎 <{url}>" if url else "")
            except Exception:
                pass
        if results:
            return "🔎 Нашёл:\n" + "\n".join(f"• {r['snippet'][:200]}" + (f" <{r['url']}>" if r["url"] else "")
                                             for r in results[:3])
        if not ai.keys.get("gemini"):
            return ("🔎 Ничего не нашёл. Для нормального поиска в интернете нужен бесплатный ключ Gemini — "
                    "руководство вставляет его командой `/ии ключ` (aistudio.google.com).")
        return f"🔎 По «{query[:80]}» ничего толком не нашёл — или поиск сейчас перегружен. Попробуй позже."

    # ---------- что я пропустил ----------
    async def missed(self, member, hours=12):
        g = member.guild
        if not self.on(g, "missed"):
            return "Сводка «что пропустил» у семьи выключена (`/настройки` → «Автоматизация»)."
        hours = max(1, min(72, int(hours or 12)))
        since = discord.utils.utcnow() - dt.timedelta(hours=hours)
        lines = []
        for key in MISSED_CHANNELS:
            ch = self.bot.ch(g, key)
            if not isinstance(ch, discord.TextChannel) or not ch.permissions_for(member).read_message_history:
                continue
            try:
                async for m in ch.history(after=since, limit=60, oldest_first=True):
                    text = m.content or ""
                    if m.embeds:
                        e = m.embeds[0]
                        text = f"{e.title or ''} {e.description or ''} {text}"
                    text = " ".join(text.split())[:220]
                    if text and m.author.id != member.id:
                        who = "бот" if m.author.bot else m.author.display_name
                        lines.append(f"[#{ch.name}] {who}: {text}")
            except discord.HTTPException:
                continue
        for r in self.bot.db.all("SELECT text FROM chronicle WHERE guild_id=? AND created_at>? ORDER BY id DESC LIMIT 10",
                                 g.id, since.isoformat()):
            lines.append(f"[летопись] {str(r['text'])[:200]}")
        if not lines:
            return f"😴 За {hours} ч ничего не было. Тишина, как на кладбище."
        blob = "\n".join(lines)[-9000:]
        ai = self.bot.ai_for(g)
        if not ai.on:
            return f"За {hours} ч: {len(lines)} сообщений. ИИ не подключён — сводку сделать не могу."
        system = self.bot.brand(g, f"Ты Швейцар — помощник RP-семьи «{config.FAMILY}». Человек отсутствовал {hours} ч. "
                                   "Сделай сводку, что он пропустил: главное по пунктам (до 10), сначала важное — "
                                   "объявления, сборы, решения, кого приняли/наказали, затем движуха в чатах. Имена "
                                   "сохраняй. Коротко, в своём стиле.")
        try:
            text = await ai.ask(system, blob, max_tokens=700)
        except Exception as e:
            return f"ИИ сейчас не отвечает ({str(e)[:80]}). Сообщений за {hours} ч: {len(lines)}."
        return f"🕰 **Что ты пропустил за {hours} ч**\n{text[:3500]}"

    # ---------- черновики ----------
    async def draft(self, guild, kind, topic):
        """(заголовок, текст) или (ошибка, None)."""
        if not self.on(guild, "drafts"):
            return "Черновики у семьи выключены (`/настройки` → «Автоматизация»).", None
        ai = self.bot.ai_for(guild)
        if not ai.on:
            return "ИИ не подключён — `/ии ключ`.", None
        fam = self.bot.family(guild)
        system = self.bot.brand(guild, f"Ты пишешь тексты для RP-семьи «{config.FAMILY}» (игра «Россия Онлайн», сервер "
                                       f"{config.GAME_SERVER}). Нужно: {DRAFT_KINDS.get(kind, 'объявление')}. Живо, по "
                                       "делу, с эмодзи в меру, Discord-разметка (**жирный**, списки). Без выдуманных "
                                       "дат и цифр — если их нет в теме, оставь [скобки] для заполнения. "
                                       'Формат: {"title":"заголовок до 80 символов","text":"текст до 1500 символов"}')
        try:
            data = await ai.ask_json(system, f"Тема: {str(topic)[:500]}\nСемья: {fam.get('name')}", max_tokens=900)
        except Exception as e:
            return f"ИИ не ответил ({str(e)[:80]}).", None
        if not isinstance(data, dict) or not data.get("text"):
            return "ИИ выдал что-то не то — попробуй ещё раз.", None
        return str(data.get("title") or "Объявление")[:200], str(data["text"])[:3500]

    def draft_embed(self, guild, title_, text):
        e = self.bot.embed(f"📝 Черновик · {title_}", text, config.COLOR, guild)
        e.set_footer(text="Это черновик — его видишь только ты. Опубликовать, переписать или запланировать — кнопками.")
        return e

    async def post_announce(self, guild, author, title_, text, ping_all=False, voice=False):
        c = self.bot.ch(guild, "news")
        if not c:
            return None
        e = self.bot.embed(f"📢 {title_}"[:256], text[:4000], config.GOLD, guild)
        if author:
            e.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        news = self.bot.role(guild, "n_news")
        await c.send(content="@everyone" if ping_all else (news.mention if news else None), embed=e,
                     allowed_mentions=discord.AllowedMentions(everyone=True, roles=True))
        music = self.bot.get_cog("Music")
        if voice and music:
            await music.announce(guild, [x.id for x in music.voice_targets(guild)],
                                 text=f"Объявление семьи. {title_}. {text[:400]}")
        return c

    # ---------- отложенные объявления ----------
    def schedule(self, guild, author, title_, text, when, ping_all=False, voice=False):
        if not self.on(guild, "schedule"):
            return "Отложенные объявления у семьи выключены (`/настройки` → «Автоматизация»)."
        ts = parse_when(when)
        if not ts:
            return ("❌ Не понял время. Примеры (по Москве): `20:00`, `завтра 18:30`, `через 2 часа`, `25.11 19:00`. "
                    "Не дальше 60 дней.")
        cur = self.bot.db.q("INSERT INTO scheduled(guild_id, author_id, title, text, ping_all, voice, run_at, created) "
                            "VALUES(?,?,?,?,?,?,?,?)", guild.id, author.id, str(title_)[:200], str(text)[:3500],
                            int(bool(ping_all)), int(bool(voice)), ts, int(time.time()))
        return f"⏰ Объявление №{cur.lastrowid} «{str(title_)[:60]}» выйдет <t:{ts}:f> (<t:{ts}:R>). Список: `/запланированные`."

    # ---------- напоминания и голосовые призывы (переживают перезапуск) ----------
    def remind_later(self, guild, kind, run_at, channel_id=None, user_id=None, text=""):
        """kind: remind — напомнить человеку (канал + голосом, если он в войсе); gather — позвать всех на сбор голосом."""
        self.bot.db.q("INSERT INTO reminders(guild_id, kind, channel_id, user_id, text, run_at) VALUES(?,?,?,?,?,?)",
                      guild.id, kind, channel_id, user_id, str(text)[:400], int(run_at))

    async def fire_reminder(self, r):
        g = self.bot.get_guild(r["guild_id"])
        if not g or not self.bot.is_family(g):
            return
        music = self.bot.get_cog("Music")
        if r["kind"] == "gather":
            if music:
                await music.announce(g, [c.id for c in music.voice_targets(g)], siren=False, text=r["text"])
            return
        who = g.get_member(r["user_id"] or 0)
        ch = self.bot.get_channel(r["channel_id"] or 0)
        if ch is None and r["channel_id"]:
            try:
                ch = await self.bot.fetch_channel(r["channel_id"])
            except discord.HTTPException:
                ch = None
        text = f"⏰ {who.mention if who else ''}, напоминаю: {r['text']}"
        try:
            if ch:
                await ch.send(text, allowed_mentions=discord.AllowedMentions(users=[who] if who else []))
            elif who:
                await who.send(text)
        except discord.HTTPException:
            pass
        if music and who and who.voice and who.voice.channel:
            await music.announce(g, [who.voice.channel.id], f"Напоминаю: {r['text']}")

    @tasks.loop(seconds=30)
    @guarded
    async def sched_loop(self):
        db = self.bot.db
        for r in db.all("SELECT * FROM reminders WHERE status='wait' AND run_at<=?", int(time.time())):
            db.q("UPDATE reminders SET status='done' WHERE id=?", r["id"])
            if time.time() - r["run_at"] > 3 * 3600:
                continue  # бот был выключен дольше — напоминать уже поздно
            try:
                await self.fire_reminder(r)
            except Exception:
                log.exception("Напоминание №%s", r["id"])
        for r in db.all("SELECT * FROM scheduled WHERE status='wait' AND run_at<=?", int(time.time())):
            db.q("UPDATE scheduled SET status='done' WHERE id=?", r["id"])
            g = self.bot.get_guild(r["guild_id"])
            if not g or not self.bot.is_family(g):
                continue
            author = g.get_member(r["author_id"] or 0)
            try:
                c = await self.post_announce(g, author, r["title"], r["text"], bool(r["ping_all"]), bool(r["voice"]))
            except Exception:
                log.exception("Отложенное объявление №%s", r["id"])
                c = None
            if not c:
                db.q("UPDATE scheduled SET status='fail' WHERE id=?", r["id"])
                await self.bot.log(g, f"❌ Отложенное объявление №{r['id']} не вышло — нет канала объявлений или прав.")

    @sched_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- ретранслятор ----------
    @commands.Cog.listener()
    async def on_message(self, message):
        g = message.guild
        if not g or message.author.id == self.bot.user.id or not self.bot.is_family(g):
            return
        src = self.bot.db.gget(g, "relay:src")
        if not src or str(message.channel.id) != str(src) or not self.on(g, "relay"):
            return
        text = message.content or ""
        for e in message.embeds:
            text += f"\n{e.title or ''}\n{e.description or ''}"
        text = text.strip()
        if len(text) < 40:
            return
        words = [w.strip().lower() for w in (self.bot.db.gget(g, "relay:filter") or "").split(",") if w.strip()]
        if words and not any(w in text.lower() for w in words):
            return
        db = self.bot.db
        if db.one("SELECT 1 FROM relay_posts WHERE guild_id=? AND message_id=?", g.id, message.id):
            return
        db.q("INSERT INTO relay_posts(guild_id, message_id, status, ts) VALUES(?,?,'new',?)", g.id, message.id,
             int(time.time()))
        ai = self.bot.ai_for(g)
        if not ai.on:
            return
        system = self.bot.brand(g, f"Ты редактор новостей RP-семьи «{config.FAMILY}». Тебе дают внешний анонс "
                                   "(обновление игры, чейнджлог, новости проекта). Вытащи только полезное для игроков: "
                                   "что изменилось, важные даты/цифры, что делать. Без воды и рекламы, 3–8 пунктов, "
                                   "Discord-разметка. Если пользы нет (реклама, флуд, мелочь) — skip=true. "
                                   'Формат: {"skip":false,"title":"до 80 символов","text":"до 1500 символов"}')
        try:
            data = await ai.ask_json(system, text[:6000], max_tokens=900)
        except Exception as e:
            log.warning("Ретранслятор: %s", e)
            return
        if not isinstance(data, dict) or data.get("skip") or not data.get("text"):
            db.q("UPDATE relay_posts SET status='skip' WHERE guild_id=? AND message_id=?", g.id, message.id)
            return
        title_, body = str(data.get("title") or "Новости")[:200], str(data["text"])[:3500]
        db.q("UPDATE relay_posts SET title=?, text=? WHERE guild_id=? AND message_id=?", title_, body, g.id, message.id)
        if (db.gget(g, "relay:mode") or "draft") == "auto":
            await self.relay_publish(g, message.id)
            return
        c = self.bot.ch(g, "botcmd")
        if c:
            v = discord.ui.View(timeout=None)
            v.add_item(RelayButton("pub", message.id))
            v.add_item(RelayButton("skip", message.id))
            e = self.bot.embed(f"📰 Черновик новости · {title_}", body, config.COLOR, g)
            e.set_footer(text=f"Из канала #{message.channel.name} · опубликовать в новости?")
            try:
                await c.send(embed=e, view=v)
            except discord.HTTPException:
                pass

    async def relay_publish(self, g, mid):
        r = self.bot.db.one("SELECT * FROM relay_posts WHERE guild_id=? AND message_id=?", g.id, mid)
        dst = self.bot.db.gget(g, "relay:dst")
        c = g.get_channel(int(dst)) if dst else self.bot.ch(g, "news")
        if not r or not r["text"] or not c:
            return None
        e = self.bot.embed(f"📰 {r['title']}"[:256], r["text"], config.GOLD, g)
        e.set_footer(text="Коротко из официальных анонсов · " + self.bot.brand(g, config.FOOTER))
        await c.send(embed=e)
        self.bot.db.q("UPDATE relay_posts SET status='posted' WHERE guild_id=? AND message_id=?", g.id, mid)
        return c

    async def relay_decide(self, inter, a, mid):
        if not inter.guild or not self.bot.has(inter.user, POSTERS):
            return await inter.response.send_message("⛔ Решает руководство.", ephemeral=True)
        r = self.bot.db.one("SELECT status FROM relay_posts WHERE guild_id=? AND message_id=?", inter.guild.id, mid)
        if not r or r["status"] in ("posted", "skip"):
            return await inter.response.send_message("Уже решено.", ephemeral=True)
        if a == "skip":
            self.bot.db.q("UPDATE relay_posts SET status='skip' WHERE guild_id=? AND message_id=?", inter.guild.id, mid)
            return await inter.response.edit_message(content=f"✖️ Пропущено ({inter.user.mention}).", view=None)
        c = await self.relay_publish(inter.guild, mid)
        await inter.response.edit_message(content=f"📰 Опубликовано в {c.mention} ({inter.user.mention})." if c else
                                          "❌ Нет канала для новостей.", view=None)

    @relay.command(name="настроить", description="Откуда брать анонсы и куда публиковать выжимку")
    @app_commands.rename(src="источник", dst="куда", mode="режим", words="фильтр")
    @app_commands.describe(src="Канал, куда приходят чужие анонсы (подпишитесь на них кнопкой «Подписаться» в Discord)",
                           dst="Куда публиковать (по умолчанию — объявления)",
                           mode="Сразу публиковать или сначала черновик руководству",
                           words="Только анонсы с этими словами, через запятую (необязательно)")
    @app_commands.choices(mode=[app_commands.Choice(name="черновик на подтверждение", value="draft"),
                                app_commands.Choice(name="публиковать сразу", value="auto")])
    @staff_only(config.LEAD)
    async def relay_set(self, inter: discord.Interaction, src: discord.TextChannel, dst: discord.TextChannel = None,
                        mode: app_commands.Choice[str] = None, words: str = None):
        db, g = self.bot.db, inter.guild
        db.gset(g, "relay:src", src.id)
        if dst:
            db.gset(g, "relay:dst", dst.id)
        db.gset(g, "relay:mode", mode.value if mode else "draft")
        if words is not None:
            db.gset(g, "relay:filter", words[:300])
        db.gset(g, "mod:auto", "1")
        db.gset(g, "mod:auto:relay", "1")
        c = dst or self.bot.ch(g, "news")
        await inter.response.send_message(
            f"📰 Ретранслятор включён: {src.mention} → {c.mention if c else '?'}, "
            f"{'сразу в новости' if (mode and mode.value == 'auto') else 'сначала черновик в бот-команды'}.\n"
            "Как подписаться на чужие анонсы: в том сервере открой канал анонсов → «Подписаться» (Follow) → выбери "
            f"этот сервер и канал {src.mention}. Новые анонсы будут приходить туда, а я — пересказывать их коротко.",
            ephemeral=True)

    @relay.command(name="выключить", description="Выключить ретранслятор")
    @staff_only(config.LEAD)
    async def relay_off(self, inter: discord.Interaction):
        self.bot.db.gset(inter.guild, "mod:auto:relay", "0")
        await inter.response.send_message("📰 Ретранслятор выключен.", ephemeral=True)

    # ---------- команды ----------
    @app_commands.command(name="пропустил", description="Что я пропустил? ИИ-сводка семьи за последние часы")
    @app_commands.rename(hours="часов")
    @app_commands.describe(hours="За сколько часов (1–72, по умолчанию 12)")
    @app_commands.guild_only()
    async def c_missed(self, inter: discord.Interaction, hours: app_commands.Range[int, 1, 72] = 12):
        if not self.bot.in_family(inter.user) and not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("Это для семьи.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        await inter.followup.send((await self.missed(inter.user, hours))[:1990], ephemeral=True)

    @app_commands.command(name="поиск", description="Найти в интернете и коротко ответить")
    @app_commands.rename(query="запрос")
    @app_commands.guild_only()
    async def c_search(self, inter: discord.Interaction, query: str):
        if not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("Это для семьи.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        await inter.followup.send((await self.search_answer(inter.guild, inter.user.id, query))[:1990], ephemeral=True)

    @app_commands.command(name="черновик", description="ИИ напишет объявление, анонс ивента, пиар-пост или поздравление")
    @app_commands.rename(kind="что", topic="тема")
    @app_commands.choices(kind=[app_commands.Choice(name=v.split(" (")[0], value=k) for k, v in DRAFT_KINDS.items()])
    @app_commands.default_permissions(manage_messages=True)
    @staff_only(config.LEAD + ["events", "hr_head", "recruiter", "diplomat"])
    async def c_draft(self, inter: discord.Interaction, kind: app_commands.Choice[str], topic: str):
        if not await self.gate(inter, "drafts"):
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        title_, text = await self.draft(inter.guild, kind.value, topic)
        if not text:
            return await inter.followup.send(title_, ephemeral=True)
        await inter.followup.send(embed=self.draft_embed(inter.guild, title_, text),
                                  view=DraftView(self, inter.user.id, kind.value, topic, title_, text), ephemeral=True)

    @app_commands.command(name="запланировать", description="Отложенное объявление: бот опубликует сам в нужное время")
    @app_commands.rename(when="когда", ping_all="пинг_всех", voice="зачитать_в_войсах")
    @app_commands.describe(when="По Москве: 20:00 · завтра 18:30 · через 2 часа · 25.11 19:00")
    @app_commands.default_permissions(manage_messages=True)
    @staff_only(POSTERS)
    async def c_schedule(self, inter: discord.Interaction, when: str, ping_all: bool = False, voice: bool = False):
        if not await self.gate(inter, "schedule"):
            return
        if not parse_when(when):
            return await inter.response.send_message(
                "❌ Не понял время. Примеры (по Москве): `20:00`, `завтра 18:30`, `через 2 часа`, `25.11 19:00`.",
                ephemeral=True)
        await inter.response.send_modal(AnnounceLaterModal(self, when, ping_all, voice))

    @app_commands.command(name="запланированные", description="Очередь отложенных объявлений (и отмена)")
    @app_commands.default_permissions(manage_messages=True)
    @staff_only(POSTERS)
    async def c_sched_list(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT * FROM scheduled WHERE guild_id=? AND status='wait' ORDER BY run_at",
                               inter.guild.id)
        if not rows:
            return await inter.response.send_message("⏰ Отложенных объявлений нет. Новое: `/запланировать`.",
                                                     ephemeral=True)
        lines = [f"№{r['id']} · <t:{r['run_at']}:f> · «{r['title'][:60]}» — <@{r['author_id']}>" for r in rows[:25]]
        v = discord.ui.View(timeout=300)
        v.add_item(CancelSelect(self, rows))
        await inter.response.send_message("⏰ **В очереди:**\n" + "\n".join(lines), view=v, ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    bot.add_dynamic_items(RelayButton)
    await bot.add_cog(Automation(bot))
