# -*- coding: utf-8 -*-
"""Квиз недели по законам и правилам (модуль lawyer:quiz, по умолчанию выключен).
• Раз в неделю (суббота, 18:00 МСК) бесплатный ИИ составляет 5 вопросов из базы юриста (законы, правила проекта,
  устав) — как генератор экзамена. Пост в общий чат с кнопкой «Пройти квиз».
• Отвечают кнопками в личном (эфемерном) окне; один заход на человека; вопросы и варианты перемешаны.
• Через 3 дня — итоги в объявлениях: сколько участвовало, лучшие. Вручную — /база квиз (руководство).
Таблицы quizzes и quiz_answers — по guild_id."""
import datetime as dt
import json
import logging
import random
import time

import discord
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded

log = logging.getLogger("family-bot.quiz")
SCHEMA = """
CREATE TABLE IF NOT EXISTS quizzes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, created INTEGER NOT NULL, closes INTEGER NOT NULL,
    questions TEXT NOT NULL, channel_id INTEGER, message_id INTEGER, status TEXT DEFAULT 'open');
CREATE INDEX IF NOT EXISTS ix_quizzes_g ON quizzes(guild_id, status);
CREATE TABLE IF NOT EXISTS quiz_answers(
    quiz_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, score INTEGER, total INTEGER,
    ts INTEGER, UNIQUE(quiz_id, user_id));
"""
QUESTIONS = 5
OPEN_DAYS = 3
WEEKDAY, HOUR_UTC = 5, 15  # суббота, 18:00 по Москве
CATS = ["laws", "ooc", "family"]
PROMPT = ("Ты составляешь короткий развлекательный квиз для RP-семьи (игра «Россия Онлайн») по тексту законов и правил "
          "ниже. Составь {n} вопросов с 4 вариантами ответа, ровно один верный. Вопросы однозначные, полезные в игре, "
          "на русском; вопрос до 200 символов, варианты до 80. Не выдумывай того, чего нет в тексте. "
          'Формат: [{{"q":"...","options":["верный","неверный","неверный","неверный"],"why":"короткое пояснение до 120 '
          'знаков, со ссылкой на статью, если есть"}}] — верный всегда первый.')


def clean_questions(data, n=QUESTIONS):
    """Ответ ИИ → [{'q','options','why'}] (верный — первый), только корректные."""
    out = []
    for q in data if isinstance(data, list) else []:
        opts = q.get("options") if isinstance(q, dict) else None
        if not isinstance(opts, list) or len(opts) != 4 or len({str(o).strip() for o in opts}) != 4:
            continue
        text = str(q.get("q") or "").strip()
        if not text:
            continue
        out.append({"q": text[:200], "options": [str(o).strip()[:80] for o in opts],
                    "why": str(q.get("why") or "").strip()[:150]})
    return out[:n]


def score_line(rows, guild):
    best = [r for r in rows if r["score"]]
    if not rows:
        return "Никто не участвовал. В следующий раз — смелее!"
    medals = ["🥇", "🥈", "🥉"]
    top = [f"{medals[i] if i < 3 else '▫️'} <@{r['user_id']}> — {r['score']}/{r['total']}" for i, r in enumerate(best[:10])]
    return f"Участвовали: **{len(rows)}**.\n" + ("\n".join(top) if top else "Ни одного верного ответа — законы ждут вас.")


class QuizGo(discord.ui.DynamicItem[discord.ui.Button], template=r"quiz:go:(?P<qid>\d+)"):
    def __init__(self, qid):
        super().__init__(discord.ui.Button(label="Пройти квиз", emoji="🧠", style=discord.ButtonStyle.primary,
                                           custom_id=f"quiz:go:{qid}"))
        self.qid = int(qid)

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["qid"])

    async def callback(self, inter: discord.Interaction):
        cog = inter.client.get_cog("Quiz")
        if not cog:
            return await inter.response.send_message("Квиз сейчас недоступен.", ephemeral=True)
        await cog.begin(inter, self.qid)


class QuestionView(discord.ui.View):
    """Один вопрос: 4 кнопки (перемешаны). Ответ → следующий вопрос в том же окне."""

    def __init__(self, cog, run):
        super().__init__(timeout=600)
        self.cog, self.run = cog, run
        q = run["qs"][run["i"]]
        for pos, idx in enumerate(run["orders"][run["i"]]):
            b = discord.ui.Button(label=q["options"][idx][:80], style=discord.ButtonStyle.secondary, row=pos)
            b.callback = self.pick(idx)
            self.add_item(b)

    def pick(self, idx):
        async def cb(inter):
            await self.cog.answer(inter, self.run, idx)
        return cb


class Quiz(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ---------- создание ----------
    async def make(self, guild, by=None):
        """Составить и опубликовать квиз → (True, текст) или (False, причина)."""
        ai = self.bot.ai_for(guild)
        if not ai.on:
            return False, "ИИ не подключён — квизу нужен бесплатный ключ (/ии ключ)."
        text = self.bot.kb.sample(guild.id, CATS, max_chars=18000)
        if len(text) < 300:
            return False, "База юриста почти пустая — загрузите законы и правила: /база."
        try:
            data = await ai.ask_json(self.bot.brand(guild, PROMPT.format(n=QUESTIONS + 2)), text, max_tokens=2500,
                                     light=True)
        except Exception as e:  # noqa: BLE001
            return False, f"ИИ не ответил: {str(e)[:120]}"
        qs = clean_questions(data)
        if len(qs) < 3:
            return False, "ИИ вернул неподходящие вопросы — попробуйте ещё раз."
        ch = self.bot.ch(guild, "chat") or self.bot.ch(guild, "news")
        if not ch:
            return False, "Нет канала общего чата."
        now_ts = int(time.time())
        closes = now_ts + OPEN_DAYS * 86400
        cur = self.bot.db.q("INSERT INTO quizzes(guild_id, created, closes, questions, channel_id) VALUES(?,?,?,?,?)",
                            guild.id, now_ts, closes, json.dumps(qs, ensure_ascii=False), ch.id)
        qid = cur.lastrowid
        v = discord.ui.View(timeout=None)
        v.add_item(QuizGo(qid))
        e = self.bot.embed("🧠 Квиз недели: законы и правила",
                           f"**{len(qs)} вопросов** по законам и правилам проекта. Отвечаешь кнопками — видишь только "
                           f"ты, один заход. Итоги и лучшие — <t:{closes}:R> в объявлениях.", config.COLOR, guild)
        try:
            msg = await ch.send(embed=e, view=v)
        except discord.HTTPException as ex:
            self.bot.db.q("DELETE FROM quizzes WHERE id=?", qid)
            return False, f"Не смог написать в {ch.mention}: {ex}"
        self.bot.db.q("UPDATE quizzes SET message_id=? WHERE id=?", msg.id, qid)
        log.info("Квиз №%s на «%s»%s", qid, guild.name, f" (запустил {by})" if by else "")
        return True, f"🧠 Квиз опубликован в {ch.mention}: {len(qs)} вопросов, итоги <t:{closes}:R>."

    # ---------- прохождение ----------
    async def begin(self, inter, qid):
        db = self.bot.db
        row = db.one("SELECT * FROM quizzes WHERE id=? AND guild_id=?", qid, inter.guild.id) if inter.guild else None
        if not row:
            return await inter.response.send_message("Квиз не найден.", ephemeral=True)
        mine = db.one("SELECT score, total FROM quiz_answers WHERE quiz_id=? AND user_id=?", qid, inter.user.id)
        if mine:
            return await inter.response.send_message(f"Ты уже прошёл: **{mine['score']}/{mine['total']}**. Итоги — "
                                                     f"<t:{row['closes']}:R>.", ephemeral=True)
        if row["status"] != "open" or time.time() > row["closes"]:
            return await inter.response.send_message("Квиз закрыт — итоги уже в объявлениях.", ephemeral=True)
        qs = json.loads(row["questions"])
        random.shuffle(qs)
        run = {"qid": qid, "qs": qs, "i": 0, "score": 0, "wrong": [],
               "orders": [random.sample(range(4), 4) for _ in qs], "user": inter.user.id}
        await inter.response.send_message(self.render(run), view=QuestionView(self, run), ephemeral=True)

    def render(self, run):
        q = run["qs"][run["i"]]
        return f"**Вопрос {run['i'] + 1}/{len(run['qs'])}**\n{q['q']}"

    async def answer(self, inter, run, idx):
        if inter.user.id != run["user"]:
            return await inter.response.send_message("Это не твой квиз.", ephemeral=True)
        q = run["qs"][run["i"]]
        before = (run["score"], len(run["wrong"]))
        if idx == 0:
            run["score"] += 1
        else:
            run["wrong"].append(f"❌ {q['q'][:120]}\n✅ {q['options'][0]}" + (f" — {q['why']}" if q.get("why") else ""))
        run["i"] += 1
        if run["i"] < len(run["qs"]):
            try:
                return await inter.response.edit_message(content=("✅ Верно!\n\n" if idx == 0 else "❌ Мимо.\n\n")
                                                         + self.render(run), view=QuestionView(self, run))
            except discord.NotFound:  # 10062: экран не сменился — откат, человек нажмёт ещё раз
                run["i"] -= 1
                run["score"] = before[0]
                del run["wrong"][before[1]:]
                return
        total = len(run["qs"])
        try:
            self.bot.db.q("INSERT INTO quiz_answers(quiz_id, guild_id, user_id, score, total, ts) VALUES(?,?,?,?,?,?)",
                          run["qid"], inter.guild.id, inter.user.id, run["score"], total, int(time.time()))
        except Exception:  # noqa: BLE001 — второй заход из старого окна
            return await inter.response.edit_message(content="Ты уже прошёл этот квиз.", view=None)
        text = f"🏁 Готово: **{run['score']}/{total}**." + (" Идеально! 🏆" if run["score"] == total else "")
        if run["wrong"]:
            text += "\n\n**Разбор ошибок:**\n" + "\n\n".join(run["wrong"])
        await inter.response.edit_message(content=text[:1900], view=None)

    # ---------- расписание и итоги ----------
    @tasks.loop(minutes=10)
    @guarded
    async def loop(self):
        t = dt.datetime.now(dt.timezone.utc)
        for g in self.bot.home_guilds:
            if not self.bot.feature(g, "lawyer:quiz"):
                continue
            await self.close_due(g)
            week = f"{t.isocalendar()[0]}-{t.isocalendar()[1]}"
            if t.weekday() == WEEKDAY and t.hour >= HOUR_UTC and self.bot.db.gget(g, "quiz:week") != week:
                self.bot.db.gset(g, "quiz:week", week)
                ok, why = await self.make(g)
                if not ok:
                    log.info("Квиз недели на «%s» не вышел: %s", g.name, why)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def close_due(self, g):
        db = self.bot.db
        for q in db.all("SELECT * FROM quizzes WHERE guild_id=? AND status='open' AND closes<=?", g.id, int(time.time())):
            db.q("UPDATE quizzes SET status='closed' WHERE id=?", q["id"])
            rows = db.all("SELECT user_id, score, total FROM quiz_answers WHERE quiz_id=? ORDER BY score DESC, ts",
                          q["id"])
            e = self.bot.embed("🏆 Итоги квиза недели", score_line(rows, g), config.GOLD, g)
            ch = self.bot.ch(g, "news") or g.get_channel(q["channel_id"] or 0)
            if ch:
                try:
                    await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            src = g.get_channel(q["channel_id"] or 0)
            if src and q["message_id"]:
                try:
                    await src.get_partial_message(q["message_id"]).edit(view=None)
                except discord.HTTPException:
                    pass


async def setup(bot):
    bot.add_dynamic_items(QuizGo)
    await bot.add_cog(Quiz(bot))
