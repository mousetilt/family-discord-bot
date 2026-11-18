# -*- coding: utf-8 -*-
"""Характер Швейцара: он помнит людей и по-разному к ним относится.
• Досье на каждого: заметки (прозвища, обещания, обиды, приколы) и «отношение» от −100 до 100. Заметки и оценку
  предлагает сам ИИ после каждого разговора; обиды он не прощает — отношение само не восстанавливается.
• Настроение по времени суток — подмешивается в каждый ответ.
• В войсе: иногда встречает входящих личной фразой; если человек долго сидит один — заходит проведать, спрашивает,
  как дела, и предлагает кого-нибудь позвать.
• /досье — что Швейцар о тебе помнит; /забудь_меня — стереть; /любимчики — кого он любит и кого нет."""
import asyncio
import datetime as dt
import json
import logging
import random
import time
import types

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now

log = logging.getLogger("family-bot.persona")
MAX_NOTES = 15
GREET_EVERY = 4 * 3600      # одного человека встречаем в войсе не чаще
GREET_GAP = 120             # и вообще не чаще, чем раз в 2 минуты
GREET_CHANCE = 0.7
ALONE_MIN = 20 * 60         # столько человек должен просидеть один, чтобы Швейцар зашёл проведать
VISIT_EVERY = 6 * 3600      # к одному человеку — не чаще
VISIT_GAP = 45 * 60         # и вообще не чаще
VISIT_HOURS = (8, 2)        # по Москве: с 08:00 до 02:00, ночью людей не дёргаем
VISIT_WAIT = 150            # сек ждём ответа, потом уходим
VISIT_IDLE = 5 * 60         # в гостях молчат 5 минут — уходим

VOICE_SYSTEM = f"""Ты «Швейцар» — наглый, языкастый, злопамятный, но свой в доску старый швейцар RP-семьи
«{config.FAMILY}» в Discord. Сейчас ты говоришь ГОЛОСОМ в голосовом канале. Ответь ОДНОЙ короткой живой репликой
(до 22 слов), на «ты», можно с подколом и матом, без эмодзи, без разметки, без кавычек. Не начинай со слова «Привет»
каждый раз — здоровайся по-разному. Без шуток про национальность, болезни и родных."""


def msk():
    return now() + dt.timedelta(hours=config.GATHER_UTC_OFFSET)


def mood_now():
    t = msk()
    h, wd = t.hour, t.weekday()
    if 2 <= h < 7:
        m = "сонный и ворчишь: нормальные люди в это время спят"
    elif 7 <= h < 11:
        m = "не выспался и бурчишь, пока не раскачался"
    elif 11 <= h < 17:
        m = "деловой и слегка скучаешь: народу мало"
    elif 17 <= h < 23:
        m = "бодрый и в ударе: самое время для движухи"
    else:
        m = "уставший, но ещё язвишь: день был длинный"
    if wd == 0 and h >= 7:
        m += "; к тому же понедельник — ты не в духе"
    elif wd in (4, 5) and h >= 17:
        m += "; к тому же вечер выходных — настроение гулять"
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"Сейчас {t:%H:%M} по Москве, {days[wd]}. Настроение: {m}."


def attitude(score):
    if score <= -30:
        return "терпеть его не можешь: он тебе хамил. Язви жёстко, припоминай обиды, одолжений не делай без ворчания"
    if score <= -8:
        return "недолюбливаешь: были стычки. Подкалывай чаще и злее обычного"
    if score < 8:
        return "ровно: обычный свой человек"
    if score < 30:
        return "он тебе нравится: нормальный мужик, подкалываешь по-доброму"
    return "твой любимчик: с ним теплее всех, заступаешься за него, но подколоть всё равно можешь"


def label(score):
    return ("😡 терпеть не может" if score <= -30 else "😒 недолюбливает" if score <= -8 else "😐 ровно"
            if score < 8 else "🙂 нравишься" if score < 30 else "😎 любимчик")


class Persona(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # таблица persona — в core/db.py (досье своё на каждом сервере)
        self.greeted = {}       # (guild_id, user_id) -> monotonic
        self.last_greet = {}    # guild_id -> monotonic: паузы у каждой семьи свои
        self.alone = {}         # user_id -> (channel_id, с какого момента один)
        self.visited = {}       # user_id -> monotonic
        self.last_visit = {}    # guild_id -> monotonic
        self.why = {}           # user_id -> почему не зашли (пишем в журнал один раз на причину)
        self.lonely.start()

    def cog_unload(self):
        self.lonely.cancel()

    # ---------- досье ----------
    def get(self, gid, uid):
        r = self.bot.db.one("SELECT score, notes FROM persona WHERE guild_id=? AND user_id=?", gid, uid)
        if not r:
            return 0, []
        try:
            notes = [n for n in json.loads(r["notes"] or "[]") if isinstance(n, dict) and n.get("s")]
        except ValueError:
            notes = []
        return int(r["score"] or 0), notes

    def save(self, gid, uid, score, notes):
        self.bot.db.q("INSERT INTO persona(guild_id, user_id, score, notes, updated) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(guild_id, user_id) DO UPDATE "
                      "SET score=excluded.score, notes=excluded.notes, updated=excluded.updated",
                      gid, uid, max(-100, min(100, int(score))), json.dumps(notes[-MAX_NOTES:], ensure_ascii=False),
                      now().isoformat())

    def forget(self, gid, uid):
        self.bot.db.q("DELETE FROM persona WHERE guild_id=? AND user_id=?", gid, uid)
        self.bot.db.gdel(gid, f"bday:{uid}")  # «забудь меня» — и день рождения тоже

    def apply(self, gid, uid, mood, remember):
        """Что ИИ вынес из разговора: как человек с ним обошёлся и что о нём стоит запомнить."""
        try:
            mood = max(-3, min(3, int(mood or 0)))
        except (TypeError, ValueError):
            mood = 0
        note = " ".join(str(remember or "").split())[:120]
        if not mood and not note:
            return
        if not self.bot.feature(gid, "persona:memory"):
            return  # семья выключила память Швейцара
        score, notes = self.get(gid, uid)
        if note and note.lower() not in {n["s"].lower() for n in notes}:
            notes.append({"d": msk().strftime("%d.%m"), "s": note})
        self.save(gid, uid, score + mood * 2, notes)

    def card(self, member):
        """Кусок подсказки для ИИ: время, настроение и досье на автора."""
        g = member.guild
        lines = [mood_now()]
        if self.bot.feature(g, "persona:memory"):
            score, notes = self.get(g.id, member.id)
            lines.append(f"Досье на автора ({member.display_name}): отношение {score:+d} из 100 — {attitude(score)}.")
            if notes:
                lines.append("Твои заметки о нём (дата — что было): "
                             + "; ".join(f"{n.get('d', '')} — {n['s']}" for n in notes))
            else:
                lines.append("Заметок о нём пока нет.")
        else:
            lines.append("Память о людях выключена руководством семьи: досье не ведёшь, обид не помнишь.")
        if not self.bot.feature(g, "persona:tease"):
            lines.append("ВАЖНО: подколы выключены руководством семьи. Ты вежливый и спокойный швейцар: без мата, "
                         "без язвительности и подколов, по-доброму помогаешь.")
        return "\n".join(lines)

    # ---------- реплики голосом ----------
    async def line(self, task, member, fallback):
        """Одна личная реплика для озвучки. ИИ не ответил — говорим заготовку."""
        ai = self.bot.ai_for(member.guild)
        if not ai.on:
            return fallback
        try:
            text = await asyncio.wait_for(ai.ask(self.bot.brand(member.guild, VOICE_SYSTEM),
                                                          f"{self.card(member)}\n\n{task}", max_tokens=90),
                                          timeout=8)
        except Exception:
            return fallback
        text = " ".join(str(text or "").replace("*", "").replace('"', "").replace("«", "").replace("»", "").split())
        return text[:220] if 3 < len(text) else fallback

    async def greet(self, guild, member):
        """Человек зашёл в войс, где сидит Швейцар: иногда встречаем личной фразой."""
        t = time.monotonic()
        if member.bot or not self.bot.has(member, config.FAMILY_PLUS) or not self.bot.feature(guild, "persona:greet"):
            return
        if t - self.last_greet.get(guild.id, -1e9) < GREET_GAP or t - self.greeted.get((guild.id, member.id), -1e9) < GREET_EVERY:
            return
        if random.random() > GREET_CHANCE:
            return
        jv = self.bot.get_cog("Jarvis")
        if not jv or guild.id not in jv.listeners:
            return
        self.last_greet[guild.id], self.greeted[(guild.id, member.id)] = t, t
        text = await self.line(f"В твой голосовой канал только что зашёл {member.display_name}. Встреть его вслух одной "
                               "фразой: по имени, в своём стиле, можно припомнить что-то из заметок или спросить, как дела.",
                               member, f"О, {member.display_name} пожаловал. Ну заходи, раз пришёл.")
        if jv.listeners.get(guild.id) and member.voice and member.voice.channel:
            await jv.say(guild, text)

    # ---------- проведать одинокого ----------
    @tasks.loop(minutes=2)
    @guarded
    async def lonely(self):
        t = time.monotonic()
        h = msk().hour
        awake = h >= VISIT_HOURS[0] or h < VISIT_HOURS[1]
        seen = set()
        for g in self.bot.home_guilds:
            jv, music = self.bot.get_cog("Jarvis"), self.bot.get_cog("Music")
            if not jv or not music or not self.bot.feature(g, "persona:visit"):
                continue
            afk = self.bot.ch(g, "afk")
            for ch in g.voice_channels:
                if afk and ch.id == afk.id:
                    continue
                people = [m for m in ch.members if not m.bot]
                if len(people) != 1:
                    continue
                m = people[0]
                seen.add(m.id)
                if self.alone.get(m.id, (0, 0))[0] != ch.id:
                    self.alone[m.id] = (ch.id, t)
                    continue
                if t - self.alone[m.id][1] < ALONE_MIN:
                    continue
                li = jv.listeners.get(g.id)
                if li and li.channel_id == ch.id:  # я уже сижу у него — не отмечаю как «занят в другом войсе»
                    continue
                reason = ("ночь по Москве" if not awake else
                          "я занят в другом войсе" if g.id in jv.listeners else
                          "играю музыку" if music.vc(g) and music.player(g).current else
                          "у него выключен звук (наушники)" if m.voice.self_deaf or m.voice.deaf else
                          "он не в семье и не друг семьи" if not self.bot.has(m, config.FAMILY_PLUS) else
                          "к нему уже заходил меньше 6 часов назад" if t - self.visited.get(m.id, -1e9) < VISIT_EVERY else
                          "недавно заходил к другому (пауза 45 минут)" if t - self.last_visit.get(g.id, -1e9) < VISIT_GAP else None)
                if reason:
                    if self.why.get(m.id) != reason:  # в журнал — один раз на причину, чтобы было видно, почему не зашёл
                        self.why[m.id] = reason
                        log.warning("Проведать: %s один в «%s» %d мин, не захожу — %s", m.display_name, ch.name,
                                    (t - self.alone[m.id][1]) // 60, reason)
                    continue
                self.why.pop(m.id, None)
                self.last_visit[g.id], self.visited[m.id] = t, t
                log.warning("Проведать: захожу к %s в «%s»", m.display_name, ch.name)
                asyncio.create_task(self.visit(g, m, ch))
        for uid in [u for u in self.alone if u not in seen]:
            self.alone.pop(uid, None)
            self.why.pop(uid, None)

    @lonely.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def visit(self, guild, member, channel):
        try:
            await self._visit(guild, member, channel)
        except Exception:
            log.exception("Проведать: сбой")

    async def _visit(self, guild, member, channel):
        jv, asst = self.bot.get_cog("Jarvis"), self.bot.get_cog("Assistant")
        mins = int((time.monotonic() - self.alone.get(member.id, (0, time.monotonic()))[1]) // 60)
        text = await self.line(f"{member.display_name} уже {mins} минут сидит один в голосовом канале «{channel.name}». Ты "
                               "зашёл его проведать. Скажи вслух: поздоровайся по имени, спроси, как дела или чем "
                               "занят, и предложи позвать кого-нибудь, чтобы ему не было скучно.",
                               member, f"{member.display_name}, ты чего один сидишь? Как дела, чем занят? Хочешь, позову "
                                       "кого-нибудь?")
        if not (member.voice and member.voice.channel and member.voice.channel.id == channel.id):
            return
        res = await jv.start(guild, member, hello=text)
        li = jv.listeners.get(guild.id)
        if str(res).startswith("❌") or not li:
            log.warning("Проведать: к %s зайти не вышло — %s", member.display_name, res)
            self.visited.pop(member.id, None)   # не засчитываем неудачную попытку
            self.last_visit.pop(guild.id, None)
            return
        li.chatty_until = time.monotonic() + VISIT_WAIT   # отвечать можно без «Швейцар»
        li.idle_stop = VISIT_IDLE
        if asst:  # чтобы ИИ помнил, что сам спросил и предложил позвать
            ctx = types.SimpleNamespace(channel=channel, author=member)
            asst.remember(ctx, "(ты сам зашёл к нему в войс проведать: он долго сидел один)", text, ask=True)
        mark = li.last_cmd
        await asyncio.sleep(VISIT_WAIT)
        if jv.listeners.get(guild.id) is li and li.last_cmd == mark:  # не ответил — не навязываемся
            await jv.say(guild, random.choice(["Молчишь? Ладно, не мешаю. Зови, если что.",
                                               "Ну и сиди молча. Я пошёл, свистни, если понадоблюсь.",
                                               "Понял, не до меня. Ушёл."]))
            await jv.stop(guild, "")

    # ---------- команды ----------
    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="досье", description="Что Швейцар о тебе помнит и как к тебе относится")
    async def dossier_cmd(self, inter: discord.Interaction):
        score, notes = self.get(inter.guild.id, inter.user.id)
        e = self.bot.embed("🎩 Досье швейцара", f"**Отношение:** {label(score)} ({score:+d} из 100)", config.COLOR, inter.guild)
        e.add_field(name="Что он о тебе запомнил",
                    value=("\n".join(f"`{n.get('d', '—')}` {n['s']}" for n in notes) or "Пока ничего.")[:1024], inline=False)
        e.set_footer(text="Стереть всё: /забудь_меня · Отношение портится от хамства и растёт от нормального общения")
        await inter.response.send_message(embed=e, ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="забудь_меня", description="Стереть всё, что Швейцар о тебе помнит")
    async def forget_cmd(self, inter: discord.Interaction):
        self.forget(inter.guild.id, inter.user.id)
        await inter.response.send_message("🧹 Стёр. Кто ты вообще такой? Начинаем с чистого листа.", ephemeral=True)

    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="любимчики", description="Кого Швейцар любит, а кого терпеть не может")
    async def favorites_cmd(self, inter: discord.Interaction):
        rows = self.bot.db.all("SELECT user_id, score FROM persona WHERE guild_id=? AND score<>0 ORDER BY score DESC",
                               inter.guild.id)
        rows = [r for r in rows if inter.guild.get_member(r["user_id"])]
        if not rows:
            return await inter.response.send_message("Пока все для меня на одно лицо. Поболтайте со мной.", ephemeral=True)
        top = [r for r in rows if r["score"] > 0][:5]
        low = [r for r in reversed(rows) if r["score"] < 0][:5]
        e = self.bot.embed("🎩 Любимчики и нелюбимчики швейцара", None, config.COLOR, inter.guild)
        if top:
            e.add_field(name="😎 Любит", value="\n".join(f"<@{r['user_id']}> · {r['score']:+d}" for r in top), inline=True)
        if low:
            e.add_field(name="😡 Точит зуб", value="\n".join(f"<@{r['user_id']}> · {r['score']:+d}" for r in low), inline=True)
        await inter.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Persona(bot))
