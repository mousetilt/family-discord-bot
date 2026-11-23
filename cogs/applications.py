# -*- coding: utf-8 -*-
"""Заявки в семью: анкета → тест по правилам → ситуационные вопросы → карточка в отдел кадров (+ ИИ-оценка)."""
import asyncio
import datetime as dt
import json
import logging
import math
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config, images
from core.botcore import guarded, now, now_iso, parse_dt, safe_dm, staff_only

log = logging.getLogger("family-bot.apps")
LETTERS = ["🇦", "🇧", "🇨", "🇩"]
STATUS = {
    "exam": ("⏳ Проходит тест", 0x9E9E9E), "exam_failed": ("📕 Тест не сдан", 0x757575),
    "new": ("🟡 Новая", 0xFDD835), "taken": ("🔵 На рассмотрении", 0x1E88E5),
    "interview": ("🟣 Собеседование", 0x8E24AA), "accepted": ("🟢 Принята", 0x43A047),
    "denied": ("🔴 Отклонена", 0xE53935), "blacklisted": ("⛔ Чёрный список", 0x212121),
    "abandoned": ("⚪ Брошена", 0x9E9E9E),
}
OPEN = ("new", "taken", "interview")
TOPIC_BY_CAT = {"ooc": "проект", "laws": "законы", "family": "семья"}


# ─────────────────────────────── вопросы ───────────────────────────────
def seed_questions(db, gid):
    """Базовые вопросы из data/exam_questions.json — каждой семье свой набор (его можно править)."""
    if db.one("SELECT COUNT(*) n FROM exam_questions WHERE guild_id=?", gid)["n"]:
        return
    try:
        data = json.loads((config.DATA / "exam_questions.json").read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не удалось загрузить data/exam_questions.json")
        return
    for q in data:
        db.q("INSERT INTO exam_questions(guild_id, question, options, answer, topic, source, created_at) "
             "VALUES(?,?,?,?,?,?,?)", gid, q["q"], json.dumps(q["options"], ensure_ascii=False), 0, q.get("topic", "проект"), "builtin", now_iso())


def pick_questions(db, gid, n):
    seed_questions(db, gid)
    rows = [dict(r) for r in db.all("SELECT * FROM exam_questions WHERE guild_id=? AND enabled=1", gid)]
    if not rows:
        return []
    fam = [r for r in rows if r["topic"] == "семья"]
    other = [r for r in rows if r["topic"] != "семья"]
    k_f = min(len(fam), max(1, round(n * 0.4))) if fam else 0
    chosen = random.sample(fam, k_f) + random.sample(other, min(len(other), n - k_f))
    if len(chosen) < n:
        rest = [r for r in rows if r not in chosen]
        chosen += random.sample(rest, min(len(rest), n - len(chosen)))
    random.shuffle(chosen)
    out = []
    for r in chosen:
        opts = json.loads(r["options"])
        correct = opts[r["answer"]]
        random.shuffle(opts)
        out.append({"id": r["id"], "q": r["question"], "options": opts, "correct": opts.index(correct),
                    "topic": r["topic"]})
    return out


class ExamSession:
    def __init__(self, user_id, app_id, questions, attempt, training=False, conf=None):
        """conf — числа экзамена семьи (Applications.conf): вопросов, нужно верных, минут, пересдача."""
        self.user_id, self.app_id, self.questions = user_id, app_id, questions
        self.attempt, self.training = attempt, training
        self.conf = conf or {"n": config.EXAM_QUESTIONS, "pass": config.EXAM_PASS, "time": config.EXAM_TIME_MIN,
                             "retry": config.EXAM_RETRY_MIN}
        self.idx, self.answers, self.started = 0, [], now()

    @property
    def time_min(self):
        return self.conf["time"]

    @property
    def expired(self):
        return (now() - self.started).total_seconds() > self.time_min * 60

    def need(self):
        total, n, p = len(self.questions), self.conf["n"], min(self.conf["pass"], self.conf["n"])
        if total >= n:
            return min(p, total)
        return math.ceil(p * total / max(n, 1))


# ─────────────────────────────── карточка заявки ───────────────────────────────
def app_embed(bot, a, user, guild):
    label, color = STATUS.get(a["status"], ("?", 0x9E9E9E))
    e = discord.Embed(title=f"Заявка №{a['id']} — {label}", color=color, timestamp=parse_dt(a["created_at"]))
    e.add_field(name="Discord", value=f"<@{a['user_id']}>", inline=True)
    e.add_field(name="Ник", value=a["nick"] or "—", inline=True)
    e.add_field(name="Static ID", value=a["static_id"] or "—", inline=True)
    e.add_field(name="Возраст / стаж", value=(a["age_exp"] or "—")[:1024], inline=False)
    e.add_field(name="Почему мы", value=(a["reason"] or "—")[:700], inline=False)
    e.add_field(name="Онлайн", value=(a["online"] or "—")[:1024], inline=True)
    if user is not None and getattr(user, "created_at", None):
        days = (now() - user.created_at).days
        e.add_field(name="Аккаунт Discord", value=f"{days} дн.{' ⚠️ свежий' if days < 30 else ''}", inline=True)
    if a["exam_total"]:
        wrong = json.loads(a["exam_wrong"] or "[]")
        txt = f"**{a['exam_score']}/{a['exam_total']}** · попытка {a['exam_attempt'] or 1}"
        if wrong:
            txt += "\nОшибки: " + "; ".join(w[:60] for w in wrong[:3]) + ("…" if len(wrong) > 3 else "")
        e.add_field(name="🧠 Тест по правилам", value=txt[:1024], inline=False)
    for item in json.loads(a["situational"] or "[]"):
        e.add_field(name=f"💬 {item['q'][:250]}", value=(item["a"] or "—")[:350], inline=False)
    if a["manual"]:
        e.add_field(name="🛂 Принят вручную", value="минуя экзамен — решение руководства", inline=False)
    if a["ai_review"]:
        e.add_field(name="🤖 ИИ-оценка", value=a["ai_review"][:1024], inline=False)
    e.add_field(name="⛔ Чёрный список", value="чисто", inline=True)
    if a["reviewer_id"]:
        e.add_field(name="Рассматривает", value=f"<@{a['reviewer_id']}>", inline=True)
    if a["deny_reason"]:
        e.add_field(name="Причина отказа", value=a["deny_reason"][:1024], inline=False)
    if user is not None:
        e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text=bot.brand(guild, config.FOOTER))
    return e


ACTIONS = {
    "take": ("Взять", "✋", discord.ButtonStyle.secondary),
    "interview": ("Собеседование", "🎤", discord.ButtonStyle.primary),
    "accept": ("Принять", "✅", discord.ButtonStyle.success),
    "deny": ("Отклонить", "❌", discord.ButtonStyle.danger),
}
# кнопки не на карточке заявки: «Принять вручную» — под записью «не сдал тест» в кадровом журнале
EXTRA_ACTIONS = {"manual": ("Принять вручную", "🛂", discord.ButtonStyle.success)}


def app_view(app_id, closed=False):
    v = discord.ui.View(timeout=None)
    if not closed:
        for a in ACTIONS:
            v.add_item(AppAction(a, app_id))
    return v


# ─────────────────────────────── UI ───────────────────────────────
class ApplyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Подать заявку", emoji="📨", style=discord.ButtonStyle.success, custom_id="apply:open")
    async def open(self, inter: discord.Interaction, _):
        await inter.client.get_cog("Applications").start_apply(inter)

    @discord.ui.button(label="Где моя заявка?", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="apply:status")
    async def status(self, inter: discord.Interaction, _):
        await inter.response.send_message(inter.client.get_cog("Applications").status_text(inter.guild, inter.user),
                                          ephemeral=True)


class ApplicationModal(discord.ui.Modal, title="Шаг 1 из 3 · Анкета"):
    nick = discord.ui.TextInput(label="Ник в игре (Имя_Фамилия)", placeholder="Ivan_Ivanov", max_length=32)
    static = discord.ui.TextInput(label="Static ID", placeholder="123456", max_length=10)
    age_exp = discord.ui.TextInput(label="Возраст (реальный) и сколько играешь", placeholder="19 лет, играю 4 месяца",
                                   max_length=100)
    reason = discord.ui.TextInput(label="Почему именно мы? Где состоял раньше?", style=discord.TextStyle.paragraph,
                                  max_length=800)
    online = discord.ui.TextInput(label="Онлайн в день и в какое время", placeholder="3-4 часа, вечером",
                                  max_length=100)

    async def on_submit(self, inter: discord.Interaction):
        await inter.client.get_cog("Applications").anketa_submitted(inter, self)


class StartExamView(discord.ui.View):
    def __init__(self, cog, s):
        super().__init__(timeout=s.time_min * 60)
        self.cog, self.s = cog, s

    @discord.ui.button(label="Начать тест", emoji="▶️", style=discord.ButtonStyle.success)
    async def go(self, inter: discord.Interaction, _):
        self.s.started = now()
        await inter.response.edit_message(embed=self.cog.question_embed(self.s, inter.guild),
                                          view=QuestionView(self.cog, self.s))


class QuestionView(discord.ui.View):
    def __init__(self, cog, s):
        super().__init__(timeout=s.time_min * 60)
        self.cog, self.s = cog, s
        self.idx = s.idx
        for i, _ in enumerate(s.questions[s.idx]["options"]):
            b = discord.ui.Button(emoji=LETTERS[i], style=discord.ButtonStyle.secondary)
            b.callback = self._cb(i)
            self.add_item(b)

    def _cb(self, i):
        async def cb(inter: discord.Interaction):
            if self.s.idx != self.idx:  # двойной клик: на этот вопрос ответ уже принят
                return await inter.response.defer()
            await self.cog.answer(inter, self.s, i)
        return cb


class SituationalStartView(discord.ui.View):
    def __init__(self, cog, app_id):
        super().__init__(timeout=1800)
        self.cog, self.app_id = cog, app_id

    @discord.ui.button(label="Ответить на вопросы", emoji="✍️", style=discord.ButtonStyle.primary)
    async def go(self, inter: discord.Interaction, _):
        await inter.response.send_modal(SituationalModal(self.cog, self.app_id, inter.client.situational(inter.guild)))


class SituationalModal(discord.ui.Modal):
    def __init__(self, cog, app_id, pool=None):
        super().__init__(title="Шаг 3 из 3 · Вопросы о тебе", timeout=1200)
        self.cog, self.app_id = cog, app_id
        self.qs = random.sample(pool or config.SITUATIONAL, 2)  # вопросы семьи — /настройки → «Тексты»
        self.inputs = []
        for label, ph in self.qs:
            ti = discord.ui.TextInput(label=label[:45], placeholder=ph[:100], style=discord.TextStyle.paragraph,
                                      min_length=10, max_length=700)
            self.add_item(ti)
            self.inputs.append(ti)

    async def on_submit(self, inter: discord.Interaction):
        answers = [{"q": f"{l}. {p}", "a": ti.value} for (l, p), ti in zip(self.qs, self.inputs)]
        await self.cog.submit(inter, self.app_id, answers)


class AppAction(discord.ui.DynamicItem[discord.ui.Button], template=r"app:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, action, app_id):
        label, emoji, style = ACTIONS.get(action) or EXTRA_ACTIONS.get(action) or ("?", None,
                                                                                discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"app:{action}:{app_id}"))
        self.action, self.app_id = action, app_id

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, inter: discord.Interaction):
        await inter.client.get_cog("Applications").act(inter, self.action, self.app_id)


class DenyModal(discord.ui.Modal, title="Отклонить заявку"):
    reason = discord.ui.TextInput(label="Причина отказа", style=discord.TextStyle.paragraph, max_length=400)

    def __init__(self, cog, app_id):
        super().__init__()
        self.cog, self.app_id = cog, app_id

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.deny(inter, self.app_id, self.reason.value)


class AddQuestionModal(discord.ui.Modal, title="Новый вопрос экзамена"):
    q = discord.ui.TextInput(label="Вопрос", style=discord.TextStyle.paragraph, max_length=300)
    right = discord.ui.TextInput(label="Правильный ответ", max_length=90)
    w1 = discord.ui.TextInput(label="Неправильный ответ 1", max_length=90)
    w2 = discord.ui.TextInput(label="Неправильный ответ 2", max_length=90)
    w3 = discord.ui.TextInput(label="Неправильный ответ 3", max_length=90)

    def __init__(self, topic):
        super().__init__()
        self.topic = topic

    async def on_submit(self, inter: discord.Interaction):
        opts = [self.right.value, self.w1.value, self.w2.value, self.w3.value]
        cur = inter.client.db.q("INSERT INTO exam_questions(guild_id, question, options, answer, topic, source, "
                                "created_at) VALUES(?,?,?,?,?,?,?)", inter.guild.id, self.q.value, json.dumps(opts, ensure_ascii=False), 0,
                                self.topic, f"user:{inter.user.id}", now_iso())
        await inter.response.send_message(f"✅ Вопрос №{cur.lastrowid} добавлен.", ephemeral=True)


# ─────────────────────────────── COG ───────────────────────────────
class Applications(commands.Cog):
    exam = app_commands.Group(name="экзамен", description="Экзамен по правилам для вступления в семью",
                              default_permissions=discord.Permissions(manage_messages=True))  # участникам — панель

    def __init__(self, bot):
        self.bot = bot
        self.sessions = {}
        self.watch_loop.start()

    def cog_unload(self):
        self.watch_loop.cancel()

    # ---------- «где моя заявка?» ----------
    def status_text(self, g, user):
        """Ответ кандидату: что с его последней заявкой и что делать дальше."""
        a = self.bot.db.one("SELECT * FROM applications WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                            g.id, user.id)
        if not a:
            return "📄 Заявок от тебя нет. Жми «📨 Подать заявку» — это займёт минут 10."

        def when(iso):
            try:
                return discord.utils.format_dt(parse_dt(iso), "R") if iso else "недавно"
            except (TypeError, ValueError):
                return "недавно"
        st, rev = a["status"], f"<@{a['reviewer_id']}>" if a["reviewer_id"] else None
        head = f"📄 Заявка №{a['id']} · {STATUS.get(st, (st,))[0]}\n"
        if st == "exam":
            return head + "Ты начал анкету, но тест не закончил. Жми «📨 Подать заявку» и пройди заново."
        if st == "exam_failed":
            retry = self.conf(g)["retry"]
            return head + (f"Тест не сдан {when(a['created_at'])}. Пересдать можно через {retry} мин после попытки — "
                           "снова «📨 Подать заявку». Потренируйся: `/экзамен тренировка`.")
        if st == "new":
            return head + (f"Подана {when(a['created_at'])} и ждёт рекрутёра. Обычно берут за пару часов. "
                           "Личку от участников сервера не закрывай — ответ придёт туда.")
        if st == "taken":
            return head + f"Её рассматривает {rev or 'рекрутёр'}. Жди сообщения в ЛС или вызова на собеседование."
        if st == "interview":
            w = self.bot.ch(g, "waiting")
            return head + (f"Тебя вызвали на собеседование. Зайди в голосовой {w.mention if w else '«Ожидание собеседования»'}"
                           f" — тебя заберёт {rev or 'рекрутёр'}.")
        if st == "accepted":
            return head + f"Принята {when(a['decided_at'])}. Ты уже в семье — добро пожаловать!"
        if st == "denied":
            return head + (f"Отклонена {when(a['decided_at'])}." + (f" Причина: {a['deny_reason']}" if a["deny_reason"] else "")
                           + " Подать снова можно позже — кнопка «📨 Подать заявку» сама скажет, когда.")
        if st == "blacklisted":
            return head + "Заявку принять нельзя."
        return head + "Эта заявка закрыта. Можно подать новую — «📨 Подать заявку»."

    # ---------- присмотр за заявками ----------
    @tasks.loop(minutes=10)
    @guarded
    async def watch_loop(self):
        """Невзятая заявка дольше app_ping_hours — пинг отделу кадров (один раз); брошенный тест — напоминание
        кандидату через сутки, через 3 дня — заявка «брошена»."""
        db = self.bot.db
        for g in list(self.bot.home_guilds):
            hours = self.bot.num(g, "app_ping_hours")
            if hours:
                border = (now() - dt.timedelta(hours=hours)).isoformat()
                for a in db.all("SELECT * FROM applications WHERE guild_id=? AND status='new' AND created_at<?",
                                g.id, border):
                    if db.gget(g, f"app:pinged:{a['id']}"):
                        continue
                    db.gset(g, f"app:pinged:{a['id']}", 1)
                    await self.ping_hr(g, a, hours)
            day = (now() - dt.timedelta(days=1)).isoformat()
            old = (now() - dt.timedelta(days=3)).isoformat()
            for a in db.all("SELECT * FROM applications WHERE guild_id=? AND status='exam' AND created_at<?", g.id, day):
                if (g.id, a["user_id"]) in self.sessions:
                    continue  # прямо сейчас проходит тест
                if a["created_at"] < old:
                    db.q("UPDATE applications SET status='abandoned' WHERE id=?", a["id"])
                    db.gdel(g, f"app:nudged:{a['id']}")
                elif not db.gget(g, f"app:nudged:{a['id']}"):
                    db.gset(g, f"app:nudged:{a['id']}", 1)
                    m = g.get_member(a["user_id"])
                    if m:
                        await safe_dm(m, f"📨 Ты начал заявку в семью {config.FAMILY}, но не прошёл тест до конца. "
                                         "Вернись на сервер и нажми «📨 Подать заявку» — начнёшь заново, это быстро.")

    async def ping_hr(self, g, a, hours):
        c = self.bot.ch(g, "hr_chat") or self.bot.ch(g, "review")
        if not c:
            return
        review = self.bot.ch(g, "review")
        link = f"https://discord.com/channels/{g.id}/{review.id}/{a['message_id']}" if review and a["message_id"] else None
        roles = [r for r in (self.bot.role(g, "recruiter"), self.bot.role(g, "hr_head")) if r]
        try:
            await c.send(content=" ".join(r.mention for r in roles) or None,
                         embed=self.bot.embed("⏰ Заявку никто не взял",
                                              f"Заявка №{a['id']} от <@{a['user_id']}> ({a['nick'] or '—'}) ждёт больше "
                                              f"{hours} ч." + (f"\n[Открыть карточку]({link}) и нажать «✋ Взять»." if link
                                                               else ""), 0xFB8C00, g),
                         allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            pass

    @watch_loop.before_loop
    async def _watch_before(self):
        await self.bot.wait_until_ready()

    def conf(self, guild):
        """Числа экзамена семьи (/настройки → «🔢 Числа»)."""
        n = self.bot.num
        return {"n": n(guild, "exam_questions"), "pass": n(guild, "exam_pass"), "time": n(guild, "exam_time"),
                "retry": n(guild, "exam_retry")}  # (guild_id, user_id) -> ExamSession: заявки в разные семьи не мешают друг другу

    async def post_panels(self, guild, repost):
        bot = self.bot

        async def make():
            c = self.conf(guild)
            e = bot.embed("📨 Заявка в семью",
                          f"Хочешь стать частью семьи **{config.FAMILY}**?\n\n"
                          f"**Шаг 1.** Анкета: ник, Static ID, возраст, опыт\n"
                          f"**Шаг 2.** Тест по правилам семьи и проекта: {c['n']} вопросов, "
                          f"нужно {min(c['pass'], c['n'])} верных, {c['time']} минут\n"
                          f"**Шаг 3.** Два вопроса о тебе\n\n"
                          "Потренироваться можно командой `/экзамен тренировка`.\n"
                          "Ответ придёт в ЛС, так что **не закрывай личку от участников сервера**.",
                          config.COLOR, guild)
            e.set_image(url="attachment://banner.png")
            return {"embed": e, "view": ApplyPanel(),
                    "file": discord.File(config.ASSETS / "banner.png", filename="banner.png")}
        await bot.panel_once(guild, "apply", "apply", repost, make, version=4)  # 4: кнопка «Где моя заявка?»

    # ---------- шаги заявки ----------
    async def start_apply(self, inter: discord.Interaction):
        bot, db, u = self.bot, self.bot.db, inter.user
        if bot.in_family(u):
            return await inter.response.send_message("Ты уже в семье 🙂", ephemeral=True)
        bl = bot.blacklist_find(inter.guild, discord_id=u.id)
        if bl:
            await bot.log(inter.guild, f"⛔ {u.mention} из чёрного списка (№{bl['id']}) пытался подать заявку", "hrlog")
            return await inter.response.send_message("К сожалению, мы не можем принять твою заявку.", ephemeral=True)
        s = self.sessions.get((inter.guild.id, u.id))
        if s and not s.expired and not s.training:
            return await inter.response.send_message("⏳ У тебя уже идёт тест — продолжи в открытом окне.",
                                                     ephemeral=True)
        gid = inter.guild.id
        pending = db.one(f"SELECT id FROM applications WHERE guild_id=? AND user_id=? AND status IN {OPEN}", gid, u.id)
        if pending:
            return await inter.response.send_message(f"⏳ Твоя заявка №{pending['id']} уже на рассмотрении.",
                                                     ephemeral=True)
        last_fail = db.one("SELECT created_at FROM exam_attempts WHERE guild_id=? AND user_id=? AND passed=0 AND "
                           "training=0 ORDER BY id DESC LIMIT 1", gid, u.id)
        if last_fail:
            left = parse_dt(last_fail["created_at"]) + dt.timedelta(minutes=self.conf(gid)["retry"]) - now()
            if left.total_seconds() > 0:
                return await inter.response.send_message(
                    f"📕 Повторно пройти тест можно через **{int(left.total_seconds() // 60) + 1} мин**. "
                    "Пока потренируйся: `/экзамен тренировка`", ephemeral=True)
        last_deny = db.one("SELECT decided_at, created_at FROM applications WHERE guild_id=? AND user_id=? AND "
                           "status='denied' ORDER BY id DESC LIMIT 1", gid, u.id)
        if last_deny and now() - parse_dt(last_deny["decided_at"] or last_deny["created_at"]) < dt.timedelta(hours=24):
            return await inter.response.send_message("⏳ После отказа повторно подать заявку можно через 24 часа.",
                                                     ephemeral=True)
        await inter.response.send_modal(ApplicationModal())

    async def anketa_submitted(self, inter: discord.Interaction, m: ApplicationModal):
        bot, db, u = self.bot, self.bot.db, inter.user
        static = m.static.value.strip()
        if not static.isdigit():
            return await inter.response.send_message("❌ Static ID должен состоять только из цифр.", ephemeral=True)
        gid = inter.guild.id
        bl = bot.blacklist_find(inter.guild, discord_id=u.id, static=static, nick=m.nick.value)
        db.q("UPDATE applications SET status='abandoned' WHERE guild_id=? AND user_id=? AND status='exam'", gid, u.id)
        cur = db.q("INSERT INTO applications(guild_id,user_id,nick,static_id,age_exp,reason,online,status,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", gid, u.id, m.nick.value.strip(), static, m.age_exp.value, m.reason.value,
                   m.online.value, "blacklisted" if bl else "exam", now_iso())
        app_id = cur.lastrowid
        if bl:
            await bot.log(inter.guild, f"⛔ Заявка №{app_id} от {u.mention} ({m.nick.value}, ID {static}) совпала с "
                                       f"чёрным списком №{bl['id']}: {bl['reason']}", "hrlog", config.RED)
            return await inter.response.send_message("К сожалению, мы не можем принять твою заявку.", ephemeral=True)
        attempt = db.one("SELECT COUNT(*) n FROM exam_attempts WHERE guild_id=? AND user_id=? AND training=0",
                         gid, u.id)["n"] + 1
        # экзамен можно выключить в /настройки → Кадры: тогда анкета сразу идёт к вопросам о себе
        conf = self.conf(gid)
        questions = pick_questions(db, gid, conf["n"]) if bot.feature(inter.guild, "hr:exam") else []
        if not questions:
            return await inter.response.send_message(
                embed=bot.embed("✅ Анкета сохранена", "Последний шаг — ответь на 2 вопроса о себе.", config.GREEN),
                view=SituationalStartView(self, app_id), ephemeral=True)
        s = ExamSession(u.id, app_id, questions, attempt, conf=conf)
        self.sessions[(inter.guild.id if inter.guild else 0, u.id)] = s
        e = bot.embed("✅ Анкета сохранена · Шаг 2 из 3 — тест",
                      f"Сейчас будет **{len(questions)} вопросов** по правилам семьи и проекта «Россия Онлайн».\n"
                      f"Нужно ответить правильно минимум на **{s.need()}**. На всё — **{s.time_min} минут**.\n\n"
                      "Не закрывай это окно. Готов?", config.COLOR, inter.guild)
        await inter.response.send_message(embed=e, view=StartExamView(self, s), ephemeral=True)

    def question_embed(self, s, guild):
        q = s.questions[s.idx]
        total = len(s.questions)
        bar = "▰" * s.idx + "▱" * (total - s.idx)
        left = max(0, int(s.time_min * 60 - (now() - s.started).total_seconds()) // 60)
        desc = f"`{bar}`\n\n**{q['q']}**\n\n" + "\n".join(f"{LETTERS[i]}  {o}" for i, o in enumerate(q["options"]))
        e = self.bot.embed(f"🧠 {'Тренировка' if s.training else 'Тест'} · вопрос {s.idx + 1} из {total}", desc,
                           config.COLOR, guild)
        e.set_footer(text=f"Тема: {q['topic']} · осталось ~{left} мин · нужно {s.need()} из {total}")
        return e

    async def answer(self, inter: discord.Interaction, s: ExamSession, i: int):
        if inter.user.id != s.user_id:
            return await inter.response.send_message("Это не твой тест.", ephemeral=True)
        if s.expired:
            key = (inter.guild.id if inter.guild else 0, s.user_id)
            if self.sessions.get(key) is s:
                self.sessions.pop(key, None)
            if s.app_id:
                self.bot.db.q("UPDATE applications SET status='abandoned' WHERE id=?", s.app_id)
            return await inter.response.edit_message(
                embed=self.bot.embed("⌛ Время вышло", "Тест не завершён вовремя. Подай заявку заново.", config.RED),
                view=None)
        s.answers.append(i)
        s.idx += 1
        if s.idx < len(s.questions):
            return await inter.response.edit_message(embed=self.question_embed(s, inter.guild),
                                                     view=QuestionView(self, s))
        await self.finish(inter, s)

    async def finish(self, inter: discord.Interaction, s: ExamSession):
        bot, db = self.bot, self.bot.db
        key = (inter.guild.id if inter.guild else 0, s.user_id)
        if self.sessions.get(key) is s:
            self.sessions.pop(key, None)
        total = len(s.questions)
        wrong = [q for q, a in zip(s.questions, s.answers) if q["correct"] != a]
        score = total - len(wrong)
        need = s.need()
        passed = score >= need
        db.q("INSERT INTO exam_attempts(guild_id, user_id, score, total, passed, training, created_at) "
             "VALUES(?,?,?,?,?,?,?)", inter.guild.id, s.user_id, score, total, int(passed), int(s.training), now_iso())
        wrong_text = "\n".join(f"• {q['q']}\n  ✅ {q['options'][q['correct']]}" for q in wrong[:8])
        if s.training:
            e = bot.embed("🎓 Тренировка завершена",
                          f"Результат: **{score}/{total}** — {'✅ сдал бы' if passed else '❌ не сдал бы'} "
                          f"(нужно {need}).\n\n" + (f"**Ошибки:**\n{wrong_text}" if wrong else "Без единой ошибки! 🔥"),
                          config.GREEN if passed else config.RED, inter.guild)
            return await inter.response.edit_message(embed=e, view=None)
        if not s.app_id:  # новичок досдаёт экзамен уже в семье (его приняли вручную)
            await bot.log(inter.guild, f"{'📗' if passed else '📕'} Новичок <@{s.user_id}> "
                                       f"{'сдал' if passed else 'не сдал'} экзамен: {score}/{total}", "hrlog",
                          config.GREEN if passed else config.RED)
            e = bot.embed("✅ Экзамен сдан!" if passed else "📕 Экзамен не сдан",
                          f"Результат: **{score}/{total}**, нужно {need}.\n\n"
                          + ("Теперь после испытательного срока тебя можно переводить в 👥 Члены семьи."
                             if passed else f"**Где ошибся:**\n{wrong_text}\n\nПопробуй снова через "
                                            f"{s.conf['retry']} мин: `/экзамен сдать`"),
                          config.GREEN if passed else config.RED, inter.guild)
            return await inter.response.edit_message(embed=e, view=None)
        db.q("UPDATE applications SET exam_score=?, exam_total=?, exam_attempt=?, exam_wrong=? WHERE id=?",
             score, total, s.attempt, json.dumps([q["q"] for q in wrong], ensure_ascii=False), s.app_id)
        if not passed:
            db.q("UPDATE applications SET status='exam_failed' WHERE id=?", s.app_id)
            hrlog = bot.ch(inter.guild, "hrlog")
            if hrlog:  # руководство может принять и без экзамена — кнопкой прямо отсюда
                v = discord.ui.View(timeout=None)
                v.add_item(AppAction("manual", s.app_id))
                try:
                    await hrlog.send(embed=discord.Embed(
                        description=f"📕 <@{s.user_id}> не сдал тест: {score}/{total} (заявка №{s.app_id}, "
                                    f"попытка {s.attempt}). Принять всё равно — кнопкой ниже.",
                        color=0x607D8B, timestamp=now()), view=v)
                except discord.HTTPException:
                    pass
            e = bot.embed("📕 Тест не сдан",
                          f"Результат: **{score}/{total}**, нужно минимум **{need}**.\n\n**Где ошибся:**\n{wrong_text}\n\n"
                          f"Перечитай #📜・правила. Повторить можно через **{s.conf['retry']} мин**.",
                          config.RED, inter.guild)
            return await inter.response.edit_message(embed=e, view=None)
        e = bot.embed("✅ Тест сдан!",
                      f"Результат: **{score}/{total}**.\n\n**Шаг 3 из 3.** Ответь на 2 вопроса о себе — честно и "
                      "развёрнуто, ответы прочитает отдел кадров.", config.GREEN, inter.guild)
        await inter.response.edit_message(embed=e, view=SituationalStartView(self, s.app_id))

    async def submit(self, inter: discord.Interaction, app_id, answers):
        bot, db = self.bot, self.bot.db
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        if not a or a["status"] not in ("exam",):
            return await inter.response.send_message("Эта заявка уже отправлена или устарела.", ephemeral=True)
        review = bot.ch(inter.guild, "review")
        if not review:
            return await inter.response.send_message("Канал заявок не настроен — сообщи руководству.", ephemeral=True)
        db.q("UPDATE applications SET situational=?, status='new' WHERE id=?",
             json.dumps(answers, ensure_ascii=False), app_id)
        await inter.response.send_message(f"✅ Заявка №{app_id} отправлена! Ответ придёт в личные сообщения.",
                                          ephemeral=True)
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        ping = bot.role(inter.guild, "recruiter")
        msg = await review.send(content=ping.mention if ping else None, embed=app_embed(bot, a, inter.user, inter.guild),
                                view=app_view(app_id), allowed_mentions=discord.AllowedMentions(roles=True))
        db.q("UPDATE applications SET message_id=? WHERE id=?", msg.id, app_id)
        self.refresh_panel(inter.guild)
        if bot.ai_for(inter.guild).on:
            asyncio.create_task(self.ai_review(inter.guild, app_id, msg, inter.user))

    def history_facts(self, guild, a, user):
        """Проверенные факты о кандидате из НАШЕЙ базы (только этот сервер): прошлые заявки, отказы и причины, был ли
        в семье и когда ушёл, тот же Static у другого Discord-аккаунта, возраст аккаунта."""
        db, out, gid = self.bot.db, [], guild.id
        prev = db.all("SELECT status, deny_reason, created_at FROM applications WHERE guild_id=? AND user_id=? AND id<>? "
                      "ORDER BY id DESC LIMIT 5", gid, a["user_id"], a["id"])
        denied = [p for p in prev if p["status"] == "denied"]
        if denied:
            out.append(f"раньше отказывали {len(denied)} раз" + (f" (последний: {str(denied[0]['deny_reason'])[:80]})"
                                                                  if denied[0]["deny_reason"] else ""))
        if any(p["status"] == "accepted" for p in prev):
            out.append("уже был принят в семью раньше")
        try:
            left = db.one("SELECT text, ts FROM member_log WHERE guild_id=? AND user_id=? AND kind='members' AND "
                          "text LIKE '%покинул сервер%' ORDER BY id DESC LIMIT 1", gid, a["user_id"])
        except Exception:  # noqa: BLE001 — журнала нет
            left = None
        if left:
            out.append(f"уходил с сервера <t:{left['ts']}:R>")
        if a["static_id"]:
            other = db.one("SELECT user_id FROM applications WHERE guild_id=? AND static_id=? AND user_id<>? LIMIT 1",
                           gid, a["static_id"], a["user_id"])
            if other:
                out.append(f"этот Static ID уже указывал другой аккаунт (<@{other['user_id']}>)")
        created = getattr(user, "created_at", None)
        if created and (now() - created).days < 30:
            out.append(f"аккаунту Discord всего {(now() - created).days} дн.")
        return out

    async def ai_review(self, guild, app_id, msg, user):
        bot = self.bot
        a = bot.db.one("SELECT * FROM applications WHERE id=?", app_id)
        system = bot.brand(guild, f"Ты помощник отдела кадров RP-семьи «{config.FAMILY}» (игра «Россия Онлайн», сервер "
                  f"{config.GAME_SERVER}). Оцени кандидата: анкету, тест по правилам и ответы на ситуационные "
                  "вопросы. Будь объективен и краток. Мат и сленг в ответах — не повод для отказа, а грубость, "
                  "желание обманывать, готовность нарушать правила или сливать информацию — повод.\n"
                  f"Правила семьи:\n{bot.rules_text(guild)}\n\n"
                  "«история_по_нашей_базе» — проверенные факты бота (прошлые заявки, отказы, уходы, совпадения Static). "
                  "Учитывай их; ничего сверх них о прошлом кандидата не придумывай.\n"
                  'Формат: {"answers":[{"score":0-10,"comment":"кратко"}], '
                  '"verdict":"принять"|"собеседование"|"отказать", "summary":"2-3 предложения", "red_flags":["..."]}')
        history = self.history_facts(guild, a, user)
        prompt = json.dumps({
            "история_по_нашей_базе": history,
            "анкета": {"ник": a["nick"], "static": a["static_id"], "возраст_и_стаж": a["age_exp"],
                       "почему_мы": a["reason"], "онлайн": a["online"]},
            "тест": f"{a['exam_score']}/{a['exam_total']}, попытка {a['exam_attempt']}",
            "ошибки_в_тесте": json.loads(a["exam_wrong"] or "[]"),
            "ситуационные_вопросы": json.loads(a["situational"] or "[]"),
        }, ensure_ascii=False)
        try:
            data = await bot.ai_for(guild).ask_json(system, prompt, max_tokens=700)
        except Exception as e:
            log.warning("ИИ-оценка заявки: %s", e)
            return
        if not isinstance(data, dict):
            return
        verdict = str(data.get("verdict", "собеседование")).lower()
        icon = {"принять": "🟢", "собеседование": "🟣", "отказать": "🔴"}.get(verdict, "⚪")
        text = f"{icon} **{verdict.capitalize()}** — {data.get('summary', '')}"
        scores = [str(x.get("score", "?")) for x in data.get("answers", []) if isinstance(x, dict)]
        if scores:
            text += f"\nОтветы: {' · '.join(s + '/10' for s in scores)}"
        flags = [str(f) for f in data.get("red_flags", []) if f]
        if flags:
            text += "\n⚠️ " + "; ".join(flags[:4])
        if history:  # факты бота — рекрутёру как есть, отдельно от мнения ИИ
            text += "\n📂 " + "; ".join(history[:4])
        bot.db.q("UPDATE applications SET ai_review=? WHERE id=?", text[:1000], app_id)
        a = bot.db.one("SELECT * FROM applications WHERE id=?", app_id)
        try:
            await msg.edit(embed=app_embed(bot, a, user, guild))
        except discord.HTTPException:
            pass

    # ---------- решения отдела кадров ----------
    async def act(self, inter: discord.Interaction, action, app_id):
        bot, db = self.bot, self.bot.db
        if not bot.has(inter.user, config.HR):
            return await inter.response.send_message("⛔ Только отдел кадров.", ephemeral=True)
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        if action == "manual":
            return await self.manual_button(inter, a)
        if not a or a["status"] not in OPEN:
            return await inter.response.send_message("Заявка уже закрыта.", ephemeral=True)
        member = inter.guild.get_member(a["user_id"])
        if action == "deny":
            return await inter.response.send_modal(DenyModal(self, app_id))
        if action == "take":
            db.q("UPDATE applications SET status='taken', reviewer_id=? WHERE id=?", inter.user.id, app_id)
        elif action == "interview":
            db.q("UPDATE applications SET status='interview', reviewer_id=? WHERE id=?", inter.user.id, app_id)
            self.stat(inter.guild, app_id, inter.user.id, "called")  # обзвон засчитает перенос в комнату собеседования
        elif action == "accept":
            if not member:
                return await inter.response.send_message("Игрок уже покинул сервер.", ephemeral=True)
            # статус — ДО первого await: второй рекрутёр, нажавший одновременно, увидит «закрыта»
            db.q("UPDATE applications SET status='accepted', reviewer_id=?, decided_at=? WHERE id=?",
                 inter.user.id, now_iso(), app_id)
            await inter.response.defer()
            self.stat(inter.guild, app_id, inter.user.id, "accepted")
            await self.accept(inter.guild, inter.user, a, member)
            a = db.one("SELECT * FROM applications WHERE id=?", app_id)
            return await inter.edit_original_response(embed=app_embed(bot, a, member, inter.guild),
                                                      view=app_view(app_id, True))
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        try:  # сначала ответить на нажатие (у Discord на это 3 секунды), ЛС — потом
            await inter.response.edit_message(embed=app_embed(bot, a, member, inter.guild), view=app_view(app_id))
        except discord.NotFound:
            pass  # кнопку нажали со старого сообщения — взаимодействие уже протухло
        if action == "interview" and member:
            waiting = bot.ch(inter.guild, "waiting")
            await safe_dm(member, f"🎤 Тебя вызывают на собеседование в семью **{config.FAMILY}**!\n"
                                  f"Зайди в голосовой канал **{waiting.name if waiting else 'Ожидание собеседования'}**"
                                  f" — тебя заберёт {inter.user.display_name}.")
        self.refresh_panel(inter.guild)

    async def accept(self, g, by, a, member):
        bot = self.bot
        try:
            await bot.set_rank(member, "newbie", by, reason=f"Заявка №{a['id']}", silent=True)
        except discord.HTTPException:
            pass
        if a["nick"]:
            # своя форма ника у семьи (/ник шаблон) — ник собираем по ней, чтобы новичку не прилетело «поправь ник»
            nick = None
            if bot.db.gget(g, "nick:template"):
                from cogs.nickcheck import build_nick
                nick = build_nick(bot.db.gget(g, "nick:template"), a["nick"], a["static_id"])
            if not nick and a["static_id"]:
                nick = f"{a['nick']} [{a['static_id']}]"
            if nick:
                try:
                    await member.edit(nick=nick[:32])
                except discord.HTTPException:
                    pass
        await safe_dm(member, f"🎉 Поздравляем! Ты принят в семью **{config.FAMILY}**!\n"
                              "Сейчас у тебя ранг 🆕 Новичок — испытательный срок. Скоро за тобой закрепят наставника.")
        chat = bot.ch(g, "chat")
        if chat:
            try:
                avatar = await member.display_avatar.replace(size=256, static_format="png").read()
            except discord.HTTPException:
                avatar = None
            card = await asyncio.to_thread(images.welcome_card, avatar, "Новый член семьи",
                                           a["nick"] or member.display_name,
                                           f"Добро пожаловать в семью {bot.family(g)['gen']}!")
            await chat.send(content=f"🎉 Встречайте нового члена семьи — {member.mention}! Принял: {by.mention}",
                            file=discord.File(card, filename="new_member.png"))
        manual = " **вручную, минуя экзамен**" if a["manual"] else ""
        await bot.log(g, f"✅ {member.mention} принят в семью{manual} (заявка №{a['id']}) — {by.mention}", "hrlog",
                      config.GREEN)
        bot.emit(g, "accepted", member=member, by=by, nick=a["nick"], app_id=a["id"])
        self.refresh_panel(g)

    def stat(self, g, app_id, reviewer_id, outcome):
        """Решение по заявке → статистика рекрутёров (cogs/recruit.py)."""
        rec = self.bot.get_cog("Recruit")
        if rec:
            rec.record(g, app_id, reviewer_id, outcome)

    def refresh_panel(self, g):
        """Счётчики заявок на закреплённой панели канала заявок."""
        hub = self.bot.get_cog("Hub")
        if hub:
            asyncio.create_task(hub.refresh_channel(g, "review"))

    # ---------- ручной приём (минуя экзамен) ----------
    async def manual_accept(self, g, by, member, nick=None, static=None):
        """Принять в семью по решению руководства: без экзамена и собеседования. Возвращает (ok, текст)."""
        bot, db = self.bot, self.bot.db
        if member.bot:
            return False, "Ботов в семью не принимаем."
        if bot.in_family(member):
            return False, f"{member.display_name} уже в семье."
        static = "".join(ch for ch in str(static or "") if ch.isdigit())[:10] or None
        nick = str(nick or "").strip()[:32] or None
        bl = bot.blacklist_find(g, discord_id=member.id, static=static, nick=nick)
        if bl:
            return False, (f"⛔ {member.display_name} в чёрном списке (№{bl['id']}: {bl['reason']}). "
                           "Сначала убери из ЧС.")
        # незакрытую заявку (в т.ч. «тест не сдан») закрываем приёмом, иначе заводим новую запись
        a = db.one("SELECT * FROM applications WHERE guild_id=? AND user_id=? AND status IN "
                   "('exam','exam_failed','new','taken','interview') ORDER BY id DESC LIMIT 1", g.id, member.id)
        if a:
            db.q("UPDATE applications SET status='accepted', manual=1, reviewer_id=?, decided_at=?, "
                 "nick=COALESCE(?, nick), static_id=COALESCE(?, static_id) WHERE id=?",
                 by.id, now_iso(), nick, static, a["id"])
            app_id = a["id"]
        else:
            app_id = db.q("INSERT INTO applications(guild_id,user_id,nick,static_id,status,manual,reviewer_id,"
                          "created_at,decided_at) VALUES(?,?,?,?,'accepted',1,?,?,?)", g.id, member.id, nick, static,
                          by.id, now_iso(), now_iso()).lastrowid
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        self.stat(g, app_id, by.id, "accepted")
        await self.accept(g, by, a, member)
        review = bot.ch(g, "review")
        if a["message_id"] and review:  # карточка в канале заявок — закрыть кнопки
            try:
                await review.get_partial_message(a["message_id"]).edit(embed=app_embed(bot, a, member, g),
                                                                       view=app_view(app_id, True))
            except discord.HTTPException:
                pass
        note = "" if self.exam_passed(g, member.id) or not bot.feature(g, "hr:exam") else \
            " Экзамен не сдавал — пусть досдаст за испытательный срок: `/экзамен сдать`."
        return True, f"✅ {member.display_name} принят в семью вручную (заявка №{app_id}).{note}"

    def exam_passed(self, g, user_id):
        return bool(self.bot.db.one("SELECT 1 FROM exam_attempts WHERE guild_id=? AND user_id=? AND passed=1 AND "
                                    "training=0 LIMIT 1", getattr(g, "id", g), user_id))

    async def manual_button(self, inter, a):
        if not self.bot.has(inter.user, config.HR):
            return await inter.response.send_message("⛔ Только отдел кадров и руководство.", ephemeral=True)
        member = inter.guild.get_member(a["user_id"]) if a else None
        if not member:
            return await inter.response.send_message("Игрок уже покинул сервер.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok, text = await self.manual_accept(inter.guild, inter.user, member)
        if ok:
            try:
                await inter.message.edit(view=None)
            except discord.HTTPException:
                pass
        await inter.followup.send(text, ephemeral=True)

    async def deny(self, inter: discord.Interaction, app_id, reason):
        bot, db = self.bot, self.bot.db
        cur = db.one("SELECT status FROM applications WHERE id=?", app_id)
        if not cur or cur["status"] not in OPEN:  # пока писали причину, заявку уже принял/отклонил другой
            return await inter.response.send_message("Заявка уже закрыта.", ephemeral=True)
        try:  # ЛС и журнал дольше 3 с — сначала отвечаем Discord, иначе 10062 Unknown interaction
            await inter.response.defer()
        except discord.HTTPException:
            pass
        db.q("UPDATE applications SET status='denied', reviewer_id=?, deny_reason=?, decided_at=? WHERE id=?",
             inter.user.id, reason, now_iso(), app_id)
        self.stat(inter.guild, app_id, inter.user.id, "denied")
        a = db.one("SELECT * FROM applications WHERE id=?", app_id)
        member = inter.guild.get_member(a["user_id"])
        if member:
            await safe_dm(member, f"😔 Твоя заявка в семью **{config.FAMILY}** отклонена.\nПричина: {reason}\n"
                                  "Подать новую можно через 24 часа.")
        self.refresh_panel(inter.guild)
        await bot.log(inter.guild, f"❌ Заявка №{app_id} (<@{a['user_id']}>) отклонена — {inter.user.mention}: {reason}",
                      "hrlog", config.RED)
        try:
            if inter.message:
                await inter.message.edit(embed=app_embed(bot, a, member, inter.guild), view=app_view(app_id, True))
            else:
                await inter.edit_original_response(embed=app_embed(bot, a, member, inter.guild),
                                                   view=app_view(app_id, True))
        except discord.HTTPException:
            pass

    # ---------- команды ----------
    @app_commands.default_permissions(move_members=True)
    @app_commands.command(name="заявки", description="Открытые заявки и статистика набора")
    @staff_only(config.HR)
    async def apps_cmd(self, inter: discord.Interaction):
        db = self.bot.db
        gid = inter.guild.id
        rows = db.all(f"SELECT * FROM applications WHERE guild_id=? AND status IN {OPEN} ORDER BY id", gid)
        week = (now() - dt.timedelta(days=7)).isoformat()
        acc = db.one("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status='accepted' AND created_at>?",
                     gid, week)["n"]
        den = db.one("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status='denied' AND created_at>?",
                     gid, week)["n"]
        att = db.one("SELECT COUNT(*) n, SUM(passed) p FROM exam_attempts WHERE guild_id=? AND training=0 AND "
                     "created_at>?", gid, week)
        top = db.all("""SELECT reviewer_id, COUNT(*) n FROM applications WHERE guild_id=? AND
                        status IN ('accepted','denied') AND created_at>? AND reviewer_id IS NOT NULL
                        GROUP BY reviewer_id ORDER BY n DESC LIMIT 5""", gid, week)
        e = self.bot.embed("📥 Набор в семью", None, config.COLOR, inter.guild)
        e.add_field(name=f"Открытые ({len(rows)})",
                    value="\n".join(f"№{r['id']} <@{r['user_id']}> — {STATUS[r['status']][0]}"
                                    for r in rows)[:1024] or "Нет", inline=False)
        e.add_field(name="За неделю", value=f"✅ принято: {acc}\n❌ отклонено: {den}\n🧠 тестов: {att['n']} "
                                            f"(сдали {att['p'] or 0})", inline=True)
        e.add_field(name="Лучшие рекрутеры", value="\n".join(f"<@{r['reviewer_id']}> — {r['n']}" for r in top) or "—",
                    inline=True)
        await inter.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="принять", description="Принять в семью вручную — без экзамена и собеседования")
    @app_commands.rename(member="участник", nick="ник", static="static_id")
    @app_commands.describe(nick="Ник в игре (Имя_Фамилия) — необязательно", static="Static ID — необязательно")
    @app_commands.default_permissions(move_members=True)
    @staff_only(config.HR)
    async def manual_cmd(self, inter: discord.Interaction, member: discord.Member, nick: str = None,
                         static: str = None):
        await inter.response.defer(ephemeral=True)
        ok, text = await self.manual_accept(inter.guild, inter.user, member, nick, static)
        await inter.followup.send(text, ephemeral=True)

    @exam.command(name="сдать", description="Сдать экзамен по правилам (для новичков, принятых без экзамена)")
    async def exam_pass(self, inter: discord.Interaction):
        bot, db, u, g = self.bot, self.bot.db, inter.user, inter.guild
        newbie = bot.role(g, "newbie")
        if not newbie or newbie not in u.roles:
            return await inter.response.send_message(
                "Сдавать экзамен здесь — для новичков на испытательном сроке. Чтобы вступить — подай заявку.",
                ephemeral=True)
        if self.exam_passed(g, u.id):
            return await inter.response.send_message("✅ Экзамен у тебя уже сдан.", ephemeral=True)
        last_fail = db.one("SELECT created_at FROM exam_attempts WHERE guild_id=? AND user_id=? AND passed=0 AND "
                           "training=0 ORDER BY id DESC LIMIT 1", g.id, u.id)
        if last_fail:
            left = parse_dt(last_fail["created_at"]) + dt.timedelta(minutes=self.conf(g)["retry"]) - now()
            if left.total_seconds() > 0:
                return await inter.response.send_message(
                    f"📕 Повторить можно через **{int(left.total_seconds() // 60) + 1} мин**.", ephemeral=True)
        conf = self.conf(g)
        qs = pick_questions(db, g.id, conf["n"])
        if not qs:
            return await inter.response.send_message("Вопросов экзамена пока нет.", ephemeral=True)
        attempt = db.one("SELECT COUNT(*) n FROM exam_attempts WHERE guild_id=? AND user_id=? AND training=0",
                         g.id, u.id)["n"] + 1
        s = ExamSession(u.id, None, qs, attempt, conf=conf)
        self.sessions[(inter.guild.id if inter.guild else 0, u.id)] = s
        await inter.response.send_message(embed=self.question_embed(s, g), view=QuestionView(self, s), ephemeral=True)

    @exam.command(name="тренировка", description="Пройти пробный тест по правилам (ни на что не влияет)")
    async def exam_train(self, inter: discord.Interaction):
        conf = self.conf(inter.guild)
        qs = pick_questions(self.bot.db, inter.guild.id, conf["n"])
        if not qs:
            return await inter.response.send_message("Вопросов пока нет.", ephemeral=True)
        s = ExamSession(inter.user.id, None, qs, 0, training=True, conf=conf)
        await inter.response.send_message(embed=self.question_embed(s, inter.guild), view=QuestionView(self, s),
                                          ephemeral=True)

    @exam.command(name="вопросы", description="Список вопросов экзамена")
    @staff_only(config.LEAD + ["hr_head"])
    async def exam_list(self, inter: discord.Interaction):
        db = self.bot.db
        gid = inter.guild.id
        seed_questions(db, gid)
        stats = db.all("SELECT topic, source, COUNT(*) n FROM exam_questions WHERE guild_id=? AND enabled=1 "
                       "GROUP BY topic, source", gid)
        rows = db.all("SELECT id, topic, question FROM exam_questions WHERE guild_id=? AND enabled=1 "
                      "ORDER BY id DESC LIMIT 25", gid)
        txt = "\n".join(f"`{r['id']}` [{r['topic']}] {r['question'][:80]}" for r in rows)
        st = ", ".join(f"{r['topic']}/{r['source'].split(':')[0]}: {r['n']}" for r in stats)
        await inter.response.send_message(embed=self.bot.embed("🧠 Вопросы экзамена", f"**Всего:** {st}\n\n{txt}"[:4000],
                                                               config.COLOR, inter.guild), ephemeral=True)

    @exam.command(name="добавить", description="Добавить свой вопрос в экзамен")
    @app_commands.rename(topic="тема")
    @app_commands.choices(topic=[app_commands.Choice(name=n, value=n) for n in ("семья", "проект", "законы")])
    @staff_only(config.LEAD + ["hr_head"])
    async def exam_add(self, inter: discord.Interaction, topic: app_commands.Choice[str]):
        await inter.response.send_modal(AddQuestionModal(topic.value))

    @exam.command(name="удалить", description="Убрать вопрос из экзамена")
    @app_commands.rename(qid="номер")
    @staff_only(config.LEAD + ["hr_head"])
    async def exam_del(self, inter: discord.Interaction, qid: int):
        self.bot.db.q("UPDATE exam_questions SET enabled=0 WHERE guild_id=? AND id=?", inter.guild.id, qid)
        await inter.response.send_message(f"🗑 Вопрос №{qid} убран.", ephemeral=True)

    @exam.command(name="сгенерировать", description="ИИ составит новые вопросы по базе знаний (законы/правила)")
    @app_commands.rename(cat="раздел", count="количество")
    @app_commands.choices(cat=[app_commands.Choice(name=v, value=k) for k, v in config.KB_CATEGORIES.items()
                               if k != "other"])
    @staff_only(config.LEAD)
    async def exam_gen(self, inter: discord.Interaction, cat: app_commands.Choice[str],
                       count: app_commands.Range[int, 3, 15] = 8):
        bot = self.bot
        ai = bot.ai_for(inter.guild)
        if not ai.on:
            return await inter.response.send_message("ИИ не подключён — бесплатный ключ: `/ии ключ`.", ephemeral=True)
        text = bot.kb.sample(inter.guild.id, [cat.value], max_chars=22000)
        if len(text) < 300:
            return await inter.response.send_message("В этом разделе базы знаний пока пусто. Добавь документы: "
                                                     "`/база ссылка` или `/база правила-проекта`.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        system = ("Ты составляешь экзамен для вступления в RP-семью на проекте «Россия Онлайн». По тексту ниже составь "
                  f"{count} вопросов с 4 вариантами ответа, ровно один верный. Вопросы однозначные, проверяют знание "
                  "правил, на русском; вопрос до 200 символов, варианты до 90. Не выдумывай того, чего нет в тексте. "
                  'Формат: [{"q":"...","options":["верный","неверный","неверный","неверный"]}] — верный всегда первый.')
        try:
            data = await ai.ask_json(system, text, max_tokens=3000)
        except Exception as e:
            return await inter.followup.send(f"❌ ИИ не ответил: {e}", ephemeral=True)
        added = []
        for q in data if isinstance(data, list) else []:
            opts = q.get("options") if isinstance(q, dict) else None
            if (not isinstance(opts, list) or len(opts) != 4 or len(set(map(str, opts))) != 4
                    or not str(q.get("q", "")).strip()):
                continue
            cur = bot.db.q("INSERT INTO exam_questions(guild_id, question, options, answer, topic, source, created_at) "
                           "VALUES(?,?,?,?,?,?,?)", inter.guild.id, str(q["q"])[:300],
                           json.dumps([str(o)[:90] for o in opts], ensure_ascii=False), 0,
                           TOPIC_BY_CAT.get(cat.value, "проект"), "ai", now_iso())
            added.append(f"`{cur.lastrowid}` {str(q['q'])[:90]}")
        await inter.followup.send(embed=bot.embed(f"🤖 Добавлено вопросов: {len(added)}",
                                                  ("\n".join(added) or "ИИ вернул неподходящий формат, попробуй ещё раз.")
                                                  + "\n\nНеудачный вопрос можно убрать: `/экзамен удалить номер`",
                                                  config.GREEN, inter.guild), ephemeral=True)


async def setup(bot):
    bot.add_view(ApplyPanel())
    bot.add_dynamic_items(AppAction)
    await bot.add_cog(Applications(bot))
