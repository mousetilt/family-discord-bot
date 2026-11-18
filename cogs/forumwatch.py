# -*- coding: utf-8 -*-
"""Форум-монитор: жалобы, иски и обращения против семьи на форуме «Россия Онлайн». Модуль «forum» (по умолчанию выкл).
• Аккаунт форума семьи (/форум вход, модалка) — бот входит им и ТОЛЬКО читает. Пароль — секрет семьи: не
  показывается, не логируется, вырезается из копий базы. Неверный пароль/двухфакторка → стоп и сообщение в бот-команды.
• Раз в N минут (/форум интервал, по умолч. 12): 1-я страница каждого раздела (core.forum.SECTIONS), новые/обновлённые
  темы — читаем (не больше MAX_READS за цикл, паузы 2 с, одна сессия). Кэш тем — forum_threads(thread_id, last_ts).
• Фамилия семьи во всех формах + Глава по имени/статику/должности (core.forum.analyze), роль — ИИ (ask_json),
  без ИИ — эвристика. Ответчик/обвиняемый → карточка в «⚖・суды-и-жалобы» + пинг; остальное — тихий лог (forum_cases).
• Дело: новые ответы — ветка под карточкой, переезд в «рассмотренные/отклонённые» или замок — статус в карточке."""
import asyncio
import json
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.family import set_module
from core.forum import (BASE, HEAD_TITLES, KIND_ICON, LAW_SECTIONS, ROLE_RANK, SECTIONS, STATUS, ForumClient,
                        LoginError, analyze, case_number, find_person, norm, status_from, surname_rx, surname_stem,
                        thread_id, thread_url, titles_rx)
from core.kb import FetchBlocked, LoginRequired
from core.secret import PREFIX, decrypt, encrypt
from core.legal import SECTION_TEMPLATE, diff_line

log = logging.getLogger("family-bot.forumwatch")
MANAGERS = ["leader", "deputy"]
MAX_READS = 30          # тем за цикл (остальное — в следующий)
FIRST_DAYS = 30         # при первом проходе старше этого не читаем
CASE_POLL_H = 6         # активное дело, которое не видно в списках, проверяем напрямую раз в столько часов
DEFAULT_INTERVAL = 12
COLORS = {"pending": 0xF9A825, "reviewed": 0x43A047, "rejected": 0x616161, "closed": 0x455A64}
ROLE_RU = {"defendant": "ответчик / обвиняемый", "plaintiff": "заявитель", "witness": "свидетель", "mention": "упоминание"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS forum_threads(
    guild_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, forum_id INTEGER, title TEXT, author TEXT,
    last_ts INTEGER DEFAULT 0, seen_ts INTEGER DEFAULT 0, harvested INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, thread_id));
CREATE TABLE IF NOT EXISTS forum_cases(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, forum_id INTEGER,
    title TEXT, url TEXT, kind TEXT, role TEXT, defendant TEXT, member_id INTEGER, head INTEGER DEFAULT 0,
    applicant TEXT, summary TEXT, case_no TEXT, status TEXT DEFAULT 'pending', body TEXT, decision TEXT,
    channel_id INTEGER, message_id INTEGER, dthread_id INTEGER, last_post_ts INTEGER DEFAULT 0,
    checked_ts INTEGER DEFAULT 0, created_ts INTEGER, updated_ts INTEGER, UNIQUE(guild_id, thread_id));
CREATE INDEX IF NOT EXISTS ix_forum_cases_g ON forum_cases(guild_id, role, status);
CREATE TABLE IF NOT EXISTS forum_people(
    guild_id INTEGER NOT NULL, name TEXT NOT NULL, static_id TEXT, user_id INTEGER, head INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, name));
"""

# известные дела семьи Ульяновых на момент запуска (домашний сервер, один раз)
KNOWN_PEOPLE = [("Юлиан Ульянов", "104589", 1), ("Ваня Ульянов", "64524", 0), ("Феликс Ульянов", None, 0)]
KNOWN_CASES = [
    {"thread_id": 47492, "forum_id": 1491, "kind": "иск", "case_no": "58", "status": "reviewed",
     "title": "Иск №58 в Верховный суд", "defendant": "Ваня Ульянов (64524), заместитель начальника ФСО",
     "applicant": "Иса Мавроди", "summary": "Иск Исы Мавроди против Вани Ульянова в Верховный суд"},
    {"thread_id": 47339, "forum_id": 1497, "kind": "иск", "case_no": "99", "status": "reviewed",
     "title": "Иск №99 в Районный суд", "defendant": "Ваня Ульянов (64524), заместитель начальника ФСО",
     "applicant": "Иса Мавроди", "summary": "Иск Исы Мавроди против Вани Ульянова в Районный суд"},
    {"thread_id": 47770, "forum_id": 1487, "kind": "прокуратура", "case_no": "220", "status": "reviewed",
     "title": "Обращение в Генпрокуратуру №220", "defendant": "Феликс Ульянов", "applicant": "Леонид Вишневский",
     "summary": "Незаконная выдача 5 звёзд розыска"},
    {"thread_id": 46522, "forum_id": None, "kind": "жалоба", "case_no": "", "status": "pending", "head": 1,
     "title": "Жалоба на лидера правительства", "defendant": "Лидер Правительства (Глава семьи)", "applicant": "",
     "summary": "Прокуратура ведёт расследование, материалы готовят к передаче в суд"},
]

CLASSIFY = (
    "Ты разбираешь темы форума RP-проекта «Россия Онлайн»: жалобы, иски в суд, обращения в прокуратуру. "
    "Нужно понять, в какой роли там член семьи {family} (фамилия {stem} в любой форме и латиницей) или Глава семьи "
    "({head}; должности: {titles}). Известные члены семьи: {people}.\n"
    "Верни JSON: {{\"role\":\"defendant|plaintiff|witness|mention\",\"who\":\"член семьи, как он назван\","
    "\"head\":true|false,\"applicant\":\"кто подал (ник)\",\"summary\":\"суть одной строкой, до 150 знаков\","
    "\"case_no\":\"номер иска/жалобы/обращения или пусто\"}}.\n"
    "defendant — на него жалуются, он ответчик/обвиняемый/нарушитель, обжалуют его действия; plaintiff — он сам подал; "
    "witness — свидетель; mention — просто упомянут. head=true, если дело против Главы (по имени или должности). "
    "Ничего не выдумывай.")


def is_manager(bot, inter):
    return bool(inter.guild) and (inter.user.id == inter.guild.owner_id or bot.has(inter.user, MANAGERS))


class AccountModal(discord.ui.Modal, title="Общий аккаунт форума (только чтение)"):
    login = discord.ui.TextInput(label="Логин или почта общего аккаунта семьи", max_length=120,
                                 placeholder="«-» — забыть аккаунт")
    password = discord.ui.TextInput(label="Пароль (хранится зашифрованным)", max_length=200, required=False)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter: discord.Interaction):
        cog, g, db = self.cog, inter.guild, self.cog.bot.db
        if self.login.value.strip() == "-":
            for k in ("forum:login", "forum:pass", "forum:cookies", "forum:auth_fail"):
                db.gdel(g, k)
            await cog.drop_client(g.id)
            return await inter.response.send_message("🔌 Аккаунт форума отключён, слежка остановлена.", ephemeral=True)
        if not self.password.value:
            return await inter.response.send_message("Нужен пароль.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        client = ForumClient(self.login.value.strip(), self.password.value)
        try:
            await client.sign_in()
            await client.forum_page(next(iter(SECTIONS)))  # видит ли аккаунт закрытые разделы
        except LoginError as e:
            return await inter.followup.send(f"❌ Не вошёл: {e}. Ничего не сохранил.", ephemeral=True)
        except LoginRequired:
            return await inter.followup.send("❌ Вошёл, но аккаунт не видит разделы жалоб и судов — у него мало прав "
                                             "на форуме. Ничего не сохранил.", ephemeral=True)
        except Exception as e:  # noqa: BLE001 — сеть, анти-флуд
            return await inter.followup.send(f"❌ Форум не ответил: {str(e)[:150]}. Попробуй позже.", ephemeral=True)
        finally:
            await client.close()
        try:
            db.gset(g, "forum:login", self.login.value.strip())
            db.gset(g, "forum:pass", encrypt(self.password.value))
            db.gset(g, "forum:cookies", encrypt(json.dumps(client.cookies)))  # дальше ходим кукой, пароль — запасной
        except RuntimeError as e:
            return await inter.followup.send(f"❌ Не могу зашифровать пароль: {e}. Ничего не сохранил.", ephemeral=True)
        for k in ("forum:auth_fail", "forum:nologin_told"):
            db.gdel(g, k)
        set_module(db, g, "forum", True)
        await cog.drop_client(g.id)
        ch = await cog.ensure_channel(g)
        cog.seed(g)
        await inter.followup.send(
            "✅ Вошёл на форум, разделы жалоб и судов видны. Модуль «⚖️ Суды и жалобы» включён.\n"
            f"Канал: {ch.mention if ch else '— не смог создать, укажи: `/форум канал`'}\n"
            "Первая проверка уже идёт (первый раз — дольше: смотрю последние 30 дней).\n"
            "Пароль и сессия хранятся зашифрованными (ключ — в окружении хоста, не в базе), бот только читает форум.",
            ephemeral=True)
        asyncio.create_task(cog.check_safe(g, manual=True))


class ForumWatch(commands.Cog):
    group = app_commands.Group(name="форум", description="Суды и жалобы против семьи на форуме", guild_only=True,
                               default_permissions=discord.Permissions(manage_messages=True))
    people_group = app_commands.Group(name="участники", description="Кого из семьи искать на форуме", parent=group)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()
        try:  # практика для юриста: тема уже взята в базу (столбец добавлен после первого выпуска)
            bot.db.c.execute("ALTER TABLE forum_threads ADD COLUMN harvested INTEGER DEFAULT 0")
            bot.db.c.commit()
        except Exception:  # noqa: BLE001 — уже есть
            pass
        self.clients = {}           # guild_id -> (логин, пароль, ForumClient)
        self.net_lock = asyncio.Lock()  # к форуму ходим строго по очереди, даже если семей с аккаунтом несколько
        self.backoff = {}           # guild_id -> до какого времени не ходить (анти-флуд)
        self.watch_loop.start()

    async def cog_unload(self):
        self.watch_loop.cancel()
        for _, _, c in self.clients.values():
            await c.close()

    # ---------- настройки ----------
    def creds(self, g):
        """(логин, пароль) общего аккаунта или None. Пароль в базе — зашифрован (core.secret)."""
        db = self.bot.db
        lg, raw = db.gget(g, "forum:login"), db.gget(g, "forum:pass")
        if not (lg and raw):
            return None
        pw = decrypt(raw)
        if pw is None:  # сменили DISCORD_TOKEN/FORUM_SECRET — старый шифр не читается
            if not db.gget(g, "forum:auth_fail"):
                db.gset(g, "forum:auth_fail", "не могу расшифровать пароль (сменился ключ на хосте) — /форум вход заново")
            return None
        if not str(raw).startswith(PREFIX):
            try:
                db.gset(g, "forum:pass", encrypt(pw))  # пароль из прошлой версии лежал открыто — шифруем
            except RuntimeError:
                pass
        return lg, pw

    def client(self, g, creds):
        """Клиент общего аккаунта (или гость при creds=None). Куки сессии — из базы, зашифрованные."""
        key = creds or (None, None)
        cur = self.clients.get(g.id)
        if cur and (cur[0], cur[1]) == key:
            return cur[2]
        db = self.bot.db
        try:
            cookies = json.loads(decrypt(db.gget(g, "forum:cookies")) or "{}") if creds else {}
        except (TypeError, ValueError):
            cookies = {}

        def save(ck, gid=g.id):
            try:
                db.gset(gid, "forum:cookies", encrypt(json.dumps(ck)))
            except RuntimeError:
                pass
        c = ForumClient(key[0], key[1], cookies=cookies, on_cookies=save)
        self.clients[g.id] = (key[0], key[1], c)
        return c

    async def drop_client(self, gid):
        cur = self.clients.pop(gid, None)
        if cur:
            await cur[2].close()

    def interval(self, g):
        try:
            return max(5, min(120, int(self.bot.db.gget(g, "forum:interval") or DEFAULT_INTERVAL)))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL

    def stem(self, g):
        return self.bot.db.gget(g, "forum:surname") or surname_stem(self.bot.family(g).get("name")
                                                                   if hasattr(self.bot, "family") else config.FAMILY)

    def matchers(self, g):
        db = self.bot.db
        rx = surname_rx(self.stem(g))
        titles = db.gget(g, "forum:titles")
        hrx = titles_rx(titles) if titles else None
        ignore = [w.strip() for w in (db.gget(g, "forum:ignore") or "").split(",") if w.strip()]
        return rx, hrx, self.people(g, rx), ignore

    def people(self, g, rx=None):
        """Члены семьи для сверки: ручной список (/форум участники) + ростер бота (ник/анкета с фамилией семьи)."""
        db = self.bot.db
        rx = rx or surname_rx(self.stem(g))
        out = {}
        for r in db.all("SELECT * FROM forum_people WHERE guild_id=?", g.id):
            out[norm(r["name"]).lower()] = {"name": r["name"], "static": r["static_id"], "user_id": r["user_id"],
                                            "head": bool(r["head"])}
        leader = self.bot.role(g, "leader")
        for m in self.bot.family_members(g):
            app = db.one("SELECT nick, static_id FROM applications WHERE guild_id=? AND user_id=? "
                         "AND static_id IS NOT NULL ORDER BY id DESC LIMIT 1", g.id, m.id)
            name = norm((app["nick"] if app and app["nick"] else m.display_name).split("[")[0])
            if not name or not (rx and rx.search(name)):
                continue
            st = app["static_id"] if app else (re.findall(r"\d{4,7}", m.display_name) or [None])[0]
            same = out.get(name.lower()) or next((p for p in out.values() if st and p["static"] == str(st)), None)
            if same:
                same["user_id"] = same["user_id"] or m.id
                same["static"] = same["static"] or st
            else:
                out[name.lower()] = {"name": name, "static": st, "user_id": m.id,
                                     "head": bool(leader and leader in m.roles)}
        if not any(p["head"] for p in out.values()) and leader:
            for p in out.values():
                mem = g.get_member(p["user_id"] or 0)
                if mem and leader in mem.roles:
                    p["head"] = True
        return list(out.values())

    def head_members(self, g):
        ids = [p["user_id"] for p in self.people(g) if p["head"] and p["user_id"]]
        ms = [m for m in (g.get_member(i) for i in ids) if m]
        if not ms:
            leader = self.bot.role(g, "leader")
            ms = list(leader.members) if leader else []
        return ms[:2]

    def seed(self, g):
        """Известные дела и люди из ТЗ — один раз, только домашнему серверу Ульяновых."""
        db = self.bot.db
        if db.gget(g, "forum:seeded") or not self.bot.is_home(g) or self.stem(g).lower() != "ульянов":
            return
        for name, st, head in KNOWN_PEOPLE:
            db.q("INSERT OR IGNORE INTO forum_people(guild_id, name, static_id, head) VALUES(?,?,?,?)", g.id, name, st,
                 head)
        db.gset(g, "forum:titles", db.gget(g, "forum:titles") or HEAD_TITLES)
        db.gset(g, "forum:ignore", db.gget(g, "forum:ignore") or "Сакрадов, АДМ-41, АДМ-46")
        now = int(time.time())
        for c in KNOWN_CASES:
            db.q("INSERT OR IGNORE INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, "
                 "head, applicant, summary, case_no, status, created_ts, updated_ts) "
                 "VALUES(?,?,?,?,?,?,'defendant',?,?,?,?,?,?,?,?)", g.id, c["thread_id"], c["forum_id"], c["title"],
                 thread_url(c["thread_id"]), c["kind"], c["defendant"], c.get("head", 0), c["applicant"], c["summary"],
                 c["case_no"], c["status"], now, now)
        db.gset(g, "forum:seeded", "1")

    # ---------- канал и карточки ----------
    async def ensure_channel(self, g):
        ch = self.bot.ch(g, "forumwatch")
        if ch:
            return ch
        self.bot.db.gdel(g, "auto:forumwatch")
        try:
            from cogs.server_setup import ensure_new_channels
            await ensure_new_channels(self.bot, g)
        except Exception:  # noqa: BLE001 — сервер не собран и т.п.: ниже — запасной вариант
            log.warning("forumwatch: автосоздание канала не вышло", exc_info=True)
        ch = self.bot.ch(g, "forumwatch")
        if ch:
            return ch
        try:
            near = self.bot.ch(g, "lawyer")
            ch = await g.create_text_channel("⚖・суды-и-жалобы", category=near.category if near else None,
                                             topic="Жалобы, иски и обращения против семьи на форуме — следит бот",
                                             reason="Форум-монитор")
            self.bot.db.gset(g, "ch:forumwatch", ch.id)
            return ch
        except discord.HTTPException:
            return None

    def who_line(self, g, c):
        who = c["defendant"] or "—"
        mem = g.get_member(c["member_id"] or 0)
        if mem:
            who += f" · {mem.mention}"
        return ("👑 Глава семьи · " if c["head"] else "") + who

    def card(self, g, c):
        sec = SECTIONS.get(c["forum_id"], ("раздел форума", "", ""))[0]
        kind = c["kind"] or "дело"
        e = discord.Embed(title=f"{KIND_ICON.get(kind, '⚖️')} {kind.capitalize()}: {c['title']}"[:256], url=c["url"],
                          color=COLORS.get(c["status"], config.COLOR))
        e.add_field(name="Против кого", value=self.who_line(g, c)[:1024], inline=True)
        e.add_field(name="Кто подал", value=(c["applicant"] or "—")[:1024], inline=True)
        e.add_field(name="Статус", value=STATUS.get(c["status"], c["status"]), inline=True)
        e.add_field(name="Раздел", value=sec, inline=True)
        if c["case_no"]:
            e.add_field(name="Номер", value=f"№{c['case_no']}", inline=True)
        e.add_field(name="Суть", value=(c["summary"] or "—")[:1024], inline=False)
        if c["decision"] and c["status"] in ("reviewed", "rejected", "closed"):
            e.add_field(name="Решение / последний ответ", value=c["decision"][:700], inline=False)
        e.set_footer(text=f"Форум · тема {c['thread_id']} · обновления — в ветке под карточкой · "
                          f"{self.bot.brand(g, config.FOOTER)}")
        return e

    async def post_card(self, g, case_id, ping=False):
        db = self.bot.db
        c = db.one("SELECT * FROM forum_cases WHERE guild_id=? AND id=?", g.id, case_id)
        ch = self.bot.ch(g, "forumwatch")
        if not c or not ch or c["message_id"]:
            return
        content = None
        if ping and self.bot.feature(g, "forum:ping"):
            targets = self.head_members(g) if c["head"] else [m for m in [g.get_member(c["member_id"] or 0)] if m]
            if targets:
                content = " ".join(m.mention for m in targets) + " — на форуме дело против тебя, глянь."
        try:
            msg = await ch.send(content, embed=self.card(g, c),
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException as e:
            log.warning("forumwatch: карточка не ушла: %s", e)
            return
        db.q("UPDATE forum_cases SET channel_id=?, message_id=? WHERE id=?", ch.id, msg.id, c["id"])

    async def post_missing_cards(self, g):
        for c in self.bot.db.all("SELECT id FROM forum_cases WHERE guild_id=? AND role='defendant' AND message_id IS NULL "
                                 "ORDER BY id", g.id):
            await self.post_card(g, c["id"])

    async def card_message(self, g, c):
        ch = g.get_channel(c["channel_id"] or 0)
        if not ch or not c["message_id"]:
            return None
        try:
            return await ch.fetch_message(c["message_id"])
        except discord.HTTPException:
            return None

    async def dthread(self, g, c):
        """Ветка обновлений под карточкой (создаётся при первом обновлении)."""
        t = g.get_thread(c["dthread_id"] or 0) or g.get_channel(c["dthread_id"] or 0)
        if t:
            return t
        msg = await self.card_message(g, c)
        if not msg:
            return None
        try:
            t = await msg.create_thread(name=f"Дело: {c['title']}"[:95], auto_archive_duration=10080)
        except discord.HTTPException:
            return None
        self.bot.db.q("UPDATE forum_cases SET dthread_id=? WHERE id=?", t.id, c["id"])
        return t

    async def refresh_card(self, g, case_id, note=None):
        c = self.bot.db.one("SELECT * FROM forum_cases WHERE guild_id=? AND id=?", g.id, case_id)
        if not c or c["role"] != "defendant":
            return
        msg = await self.card_message(g, c)
        if msg:
            try:
                await msg.edit(embed=self.card(g, c))
            except discord.HTTPException:
                pass
        if note:
            t = await self.dthread(g, c)
            if t:
                try:
                    await t.send(note[:1900])
                except discord.HTTPException:
                    pass

    # ---------- проверка ----------
    async def check_safe(self, g, manual=False):
        try:
            return await self.check(g, manual)
        except Exception:  # noqa: BLE001
            log.exception("forumwatch: проверка упала")
            return "❌ Проверка упала — ошибка ушла разработчику."

    async def check(self, g, manual=False):
        db = self.bot.db
        creds = self.creds(g)  # None — гость: смотрим то, что видно без входа
        if db.gget(g, "forum:auth_fail") and not manual:
            return f"Вход на форум не работает: {db.gget(g, 'forum:auth_fail')}"
        self.seed(g)
        rx, hrx, people, ignore = self.matchers(g)
        if not rx and not hrx:
            return "Не знаю, какую фамилию искать."
        client = self.client(g, creds)
        now = int(time.time())
        db.gset(g, "forum:last", now)
        first = not db.one("SELECT 1 FROM forum_threads WHERE guild_id=? LIMIT 1", g.id)
        st = {"lists": 0, "read": 0, "new": 0, "upd": 0}
        err = None
        async with self.net_lock:
            try:
                changes, sticky, hidden = [], {}, []
                for fid in SECTIONS:
                    for page in range(1, 3 if first else 2):
                        try:
                            items = await client.forum_page(fid, page)
                        except LoginRequired:
                            hidden.append(fid)  # этот раздел аккаунту не виден — остальные смотрим
                            break
                        st["lists"] += 1
                        sticky.setdefault(fid, []).extend(it for it in items if it.get("sticky"))
                        for it in items:
                            if not it.get("sticky"):
                                changes += self.remember(g, fid, it, now - FIRST_DAYS * 86400)
                        if len(items) < 10:
                            break
                if len(hidden) == len(SECTIONS):
                    raise LoginRequired("ни один раздел не виден")
                if hidden:
                    err = "аккаунту не видны разделы: " + ", ".join(SECTIONS[f][0] for f in hidden[:5])
                for case_id, status, fid in changes:
                    db.q("UPDATE forum_cases SET status=?, forum_id=?, updated_ts=? WHERE id=?", status, fid, now,
                         case_id)
                    await self.refresh_card(g, case_id, f"📌 Статус дела: **{STATUS.get(status, status)}** "
                                                         f"({SECTIONS.get(fid, ('раздел форума',))[0]})")
                todo = [("thread", r) for r in db.all(
                    "SELECT t.* FROM forum_threads t LEFT JOIN forum_cases c ON c.guild_id=t.guild_id AND "
                    "c.thread_id=t.thread_id WHERE t.guild_id=? AND t.last_ts>t.seen_ts AND t.forum_id NOT IN "
                    f"({','.join(str(f) for f in LAW_SECTIONS)}) "
                    "ORDER BY (c.id IS NOT NULL) DESC, t.last_ts DESC LIMIT ?", g.id, MAX_READS)]
                seen = {r["thread_id"] for _, r in todo}
                for c in db.all("SELECT * FROM forum_cases WHERE guild_id=? AND status='pending' AND checked_ts<? "
                                "ORDER BY checked_ts LIMIT 5", g.id, now - CASE_POLL_H * 3600):
                    if c["thread_id"] not in seen:
                        todo.append(("case", c))
                for kind, r in todo[:MAX_READS + 5]:
                    try:
                        await self.read(g, client, r["thread_id"], r if kind == "thread" else None,
                                        rx, hrx, people, ignore, st)
                    except (LoginError, FetchBlocked):
                        raise
                    except LoginRequired:
                        db.q("UPDATE forum_threads SET seen_ts=last_ts WHERE guild_id=? AND thread_id=?", g.id,
                             r["thread_id"])  # тема закрыта даже для аккаунта — не долбим
                    except Exception:  # noqa: BLE001 — одна кривая тема не должна ронять цикл
                        log.warning("forumwatch: тема %s не разобрана", r["thread_id"], exc_info=True)
                        db.q("UPDATE forum_threads SET seen_ts=last_ts WHERE guild_id=? AND thread_id=?", g.id,
                             r["thread_id"])
                if self.bot.feature(g, "lawyer"):
                    await self.harvest(g, client, sticky, st)
            except LoginError as e:
                err = f"не вхожу на форум: {e}"
                db.gset(g, "forum:auth_fail", str(e)[:200])
                await self.drop_client(g.id)
                await self.alert(g, f"⚖️ Форум-монитор остановлен: {e}.\nПоправь данные: `/форум вход`.")
            except FetchBlocked:
                err = "форум попросил сбавить обороты (анти-флуд) — продолжу через 30 минут"
                self.backoff[g.id] = time.time() + 1800
            except LoginRequired:
                if creds:
                    err = "аккаунт не видит разделы жалоб и судов"
                    await self.alert(g, "⚖️ Форум-монитор: аккаунт форума вошёл, но не видит разделы жалоб и судов.")
                else:
                    err = "без входа разделы жалоб и судов не видны — владелец сервера: `/форум вход`"
                    self.backoff[g.id] = time.time() + 6 * 3600  # гостем раз в 6 часов, не чаще
                    await self.tell_owner(g)
        db.gset(g, "forum:last_err", err or "")
        await self.post_missing_cards(g)
        res = (f"Просмотрено разделов: {st['lists']}, тем прочитано: {st['read']}, новых дел: {st['new']}, "
               f"обновлений: {st['upd']}." + (f" Для юриста: {st['kb']}." if st.get("kb") else ""))
        return res + (f"\n⚠️ {err}" if err else "")

    # ---------- для юриста: судебная практика, формы документов, законы ----------
    def kb_doc_by_thread(self, g, tid):
        for r in self.bot.db.all("SELECT id, guild_id, url, title, content FROM kb_docs WHERE guild_id IN (0, ?) "
                                 "AND url LIKE ?", g.id, f"%{tid}/%"):
            if thread_id(r["url"]) == tid:
                return r
        return None

    def kb_put(self, g, tid, title, category, content):
        """Тема форума → база юриста. Документ уже есть (свой или общий) — обновляем на месте.
        → старый текст (None — документа не было)."""
        old = self.kb_doc_by_thread(g, tid)
        if old:
            if (old["content"] or "") != content:
                self.bot.db.q("UPDATE kb_docs SET title=?, category=?, content=?, updated_at=? WHERE id=?", title[:200],
                              category, content, discord.utils.utcnow().isoformat(), old["id"])
                self.bot.kb.invalidate()
            return old["content"] or ""
        self.bot.kb.add(g.id, title[:200], thread_url(tid), category, content, 0)
        return None

    @staticmethod
    def practice_text(sec, status, url, posts):
        first = posts[0]
        ans = [p for p in posts[1:] if p["author"] != first["author"] and p["text"]]
        out = (f"Раздел: {sec}. Итог: {STATUS.get(status, status)}. Тема: {url}\n"
               f"Заявление ({first['author'] or 'заявитель'}):\n{first['text'][:2500]}")
        if ans:
            out += f"\n\nРешение / ответ ({ans[-1]['author'] or 'ответ'}):\n{ans[-1]['text'][:2500]}"
        return out

    async def harvest(self, g, client, sticky, st):
        db, now = self.bot.db, int(time.time())
        got = {"practice": 0, "forms": 0, "laws": 0}
        # 1) судебная практика: рассмотренные/отклонённые темы — по 3 за цикл, каждую один раз
        closed = [f for f, s in SECTIONS.items() if s[2] != "pending"]
        for r in db.all(f"SELECT * FROM forum_threads WHERE guild_id=? AND harvested=0 AND forum_id IN "
                        f"({','.join('?' * len(closed))}) ORDER BY last_ts DESC LIMIT 3", g.id, *closed):
            db.q("UPDATE forum_threads SET harvested=1 WHERE guild_id=? AND thread_id=?", g.id, r["thread_id"])
            try:
                meta, posts = await client.thread(r["thread_id"])
                if meta["pages"] > 1:
                    posts = posts[:1] + (await client.thread(r["thread_id"], latest=True))[1]
            except (LoginError, FetchBlocked):
                raise
            except Exception:  # noqa: BLE001
                continue
            if posts and posts[0]["text"]:
                fid = meta["forum_id"] or r["forum_id"]
                self.kb_put(g, r["thread_id"], f"Практика: {meta['title'] or r['title']}", "practice",
                            self.practice_text(SECTIONS.get(fid, ("",))[0], status_from(fid, meta["prefix"],
                                               meta["locked"]), thread_url(r["thread_id"]), posts))
                got["practice"] += 1
        # дела семьи с решением — тоже практика (без сети)
        for c in db.all("SELECT * FROM forum_cases WHERE guild_id=? AND status<>'pending' AND decision<>''", g.id):
            body = (f"Раздел: {SECTIONS.get(c['forum_id'], ('',))[0]}. Итог: {STATUS.get(c['status'])}. "
                    f"Тема: {c['url']}\nДело против: {c['defendant']}. Подал: {c['applicant']}. Суть: {c['summary']}\n"
                    f"Заявление:\n{(c['body'] or '')[:2000]}\n\nРешение / ответ:\n{c['decision']}")
            self.kb_put(g, c["thread_id"], f"Практика: {c['title']}", "practice", body)
        # 2) формы документов: закреплённые темы судов/прокуратуры — раз в неделю
        if now - int(db.gget(g, "forum:forms_ts") or 0) > 7 * 86400:
            for fid in SECTION_TEMPLATE:
                for it in sticky.get(fid, [])[:3]:
                    if not re.search(r"форм|образ|шаблон|подач|порядок|правила", it["title"], re.I):
                        continue
                    try:
                        title, content = await client.document(it["id"], max_pages=1)
                    except (LoginError, FetchBlocked):
                        raise
                    except Exception:  # noqa: BLE001
                        continue
                    if len(content) > 40:
                        self.kb_put(g, it["id"], f"Форма: {SECTIONS[fid][0]} — {title}", "forms", content)
                        got["forms"] += 1
            db.gset(g, "forum:forms_ts", now)
        # 3) законы (1471) и указы (1474): список — раз в 3 дня, новые/изменённые темы — до 8 за цикл
        if now - int(db.gget(g, "forum:laws_ts") or 0) > 3 * 86400:
            for fid in LAW_SECTIONS:
                for page in (1, 2):
                    try:
                        items = await client.forum_page(fid, page)
                    except LoginRequired:
                        break
                    for it in items:
                        self.remember(g, fid, it, 0)
                    if len(items) < 10:
                        break
            db.gset(g, "forum:laws_ts", now)
        init = not db.gget(g, "forum:laws_init")  # первая загрузка — без отчёта «что изменилось»
        changed, added = [], []
        rows = db.all(f"SELECT * FROM forum_threads WHERE guild_id=? AND forum_id IN "
                      f"({','.join(str(f) for f in LAW_SECTIONS)}) AND last_ts>seen_ts ORDER BY last_ts DESC LIMIT 8",
                      g.id)
        for r in rows:
            db.q("UPDATE forum_threads SET seen_ts=last_ts WHERE guild_id=? AND thread_id=?", g.id, r["thread_id"])
            try:
                title, content = await client.document(r["thread_id"])
            except (LoginError, FetchBlocked):
                raise
            except Exception:  # noqa: BLE001
                continue
            if len(content) < 50:
                continue
            prefix = LAW_SECTIONS.get(r["forum_id"], "")
            title = title if title.startswith(prefix) else prefix + title
            old = self.kb_put(g, r["thread_id"], title, "laws", content[:300000])
            got["laws"] += 1
            if old is None:
                added.append(title)
            elif old != content[:300000]:
                changed.append(diff_line(title, old, content))
        if init and not db.one(f"SELECT 1 FROM forum_threads WHERE guild_id=? AND forum_id IN "
                               f"({','.join(str(f) for f in LAW_SECTIONS)}) AND last_ts>seen_ts LIMIT 1", g.id) \
                and db.gget(g, "forum:laws_ts"):
            db.gset(g, "forum:laws_init", "1")
        elif not init and (changed or added):
            text = "📜 **Обновилась законодательная база** (юрист уже знает):\n" + "\n".join(
                [f"• {x}" for x in changed] + [f"• новый документ: «{x}»" for x in added])
            for key in ("botcmd", "lawyer"):
                ch = self.bot.ch(g, key)
                if ch:
                    try:
                        await ch.send(text[:1900])
                    except discord.HTTPException:
                        pass
        parts = [f"{k} +{v}" for k, v in (("практика", got["practice"]), ("формы", got["forms"]),
                                          ("законы", got["laws"])) if v]
        if parts:
            st["kb"] = ", ".join(parts)

    def cases_context(self, g, question):
        """Дела против семьи для юриста. → (текст для ИИ, просят ли защиту по конкретному делу)."""
        db = self.bot.db
        rows = db.all("SELECT * FROM forum_cases WHERE guild_id=? AND role='defendant' ORDER BY (status='pending') "
                      "DESC, updated_ts DESC LIMIT 12", g.id)
        if not rows:
            return "", False
        q = norm(question)
        target = None
        for c in rows:
            if c["case_no"] and re.search(rf"(?<!\d){re.escape(c['case_no'])}(?!\d)", q):
                target = c
                break
        if target is None:
            p = find_person(self.people(g), q)
            if p:
                mine = [c for c in rows if (p["user_id"] and c["member_id"] == p["user_id"]) or
                        norm(p["name"]).lower() in norm(c["defendant"]).lower() or (p["head"] and c["head"])]
                target = mine[0] if mine else None
        if target is None and re.search(r"глав|премьер|лидер\w* правительств", q, re.I):
            target = next((c for c in rows if c["head"]), None)
        active = [c for c in rows if c["status"] == "pending"] + [c for c in rows if c["status"] != "pending"][:3]
        lines = [f"• «{c['title']}» — против {c['defendant']}, подал {c['applicant'] or '?'}, "
                 f"{STATUS.get(c['status'], c['status'])}, {c['url']}" for c in active]
        text = "ДЕЛА ПРОТИВ СЕМЬИ НА ФОРУМЕ (знай о них; упоминай, только если вопрос о них):\n" + "\n".join(lines)
        if target:
            text += (f"\n\nПОДРОБНО ПО ДЕЛУ «{target['title']}» ({target['url']}):\nПротив: {target['defendant']}\n"
                     f"Подал: {target['applicant']}\nСуть: {target['summary']}\n"
                     f"Статус: {STATUS.get(target['status'], target['status'])}\n"
                     f"Текст заявления:\n{(target['body'] or '— не загружен')[:2500]}\n"
                     f"Последний ответ / решение:\n{(target['decision'] or '—')[:1200]}")
        wants = bool(target) and bool(re.search(r"дел|защит|что делать|иск|жалоб|суд|прокурат|позици|ответ",
                                                q, re.I))
        return text, wants

    async def fetch_doc(self, g, url):
        """Закрытая тема форума аккаунтом семьи (для /база). → (название, текст, ссылка) или None (нет аккаунта)."""
        creds, tid = self.creds(g), thread_id(url)
        if not creds or not tid:
            return None
        async with self.net_lock:
            title, content = await self.client(g, creds).document(tid)
        if len(content) < 50:
            raise ValueError("В теме почти нет текста.")
        return title, content, thread_url(tid)

    # ---------- проверка человека: «проверь меня / Петю по форуму» ----------
    def identity(self, g, member, game_name=None, static=None):
        """Кто человек по игре — молча из того, что бот уже знает: анкета (ник + статик), ручной список форума,
        Discord-ник, прозвища. → (полные имена «Имя Фамилия», статики, как назвать в ответе)."""
        db = self.bot.db
        names, statics = [(game_name or "", False)], [re.sub(r"\D", "", str(static or ""))]  # (имя, строго?)
        if member is not None:
            for r in db.all("SELECT nick, static_id FROM applications WHERE guild_id=? AND user_id=? "
                            "ORDER BY id DESC LIMIT 5", g.id, member.id):
                names.append((r["nick"] or "", False))
                statics.append(str(r["static_id"] or ""))
            for r in db.all("SELECT name, static_id FROM forum_people WHERE guild_id=? AND user_id=?", g.id,
                            member.id):
                names.append((r["name"], False))
                statics.append(str(r["static_id"] or ""))
            # Discord-ник и прозвища — игровое имя, только если похоже на «Имя Фамилия» (с заглавных), а не «cool_guy»
            names.append((member.display_name.split("[")[0], True))
            statics += re.findall(r"(?<!\d)\d{4,7}(?!\d)", member.display_name)
            names += [(x, True) for x in (db.gget(g, f"alias:{member.id}") or "").split(",")]
        full, seen = [], set()
        for n, strict in names:
            n = " ".join(re.sub(r"[^A-Za-zА-Яа-яЁё\- ]", " ", norm(n).replace("\n", " ")).split())
            words = n.split()
            if len(words) < 2 or n.lower() in seen:  # искать можно только «Имя Фамилия»
                continue
            if strict and not all(w[0].isupper() for w in words[:2]):
                continue
            seen.add(n.lower())
            full.append(n)
        st = list(dict.fromkeys(x for x in statics if x and len(x) >= 3 and x.isdigit()))
        label = member.display_name if member is not None else (full[0] if full else (game_name or "?"))
        return full, st, label

    @staticmethod
    def _names_in(text, name):
        """«Имя Фамилия» (в любом падеже, латиницей) в тексте: фамилия + имя рядом с ней."""
        last = name.split()[-1].lower()
        rx = surname_rx(re.sub(r"(а|ой|ая|ий)$", "", last) if len(last) > 5 else last)
        for m in (rx.finditer(text) if rx else []):
            zone = text[max(0, m.start() - 40):m.end() + 15]
            if find_person([{"name": name, "static": None}], zone, zone):
                return True
        return False

    def person_hits(self, g, member, names, statics):
        """Дела форума, где фигурирует человек. → [(дело, роль)]: роль относительно ЭТОГО человека."""
        db = self.bot.db
        heads = {p["user_id"] for p in self.people(g) if p["head"] and p["user_id"]}
        out = []
        for c in db.all("SELECT * FROM forum_cases WHERE guild_id=? ORDER BY (status='pending') DESC, updated_ts DESC",
                        g.id):
            dfd = norm(c["defendant"] or "")
            text = norm("\n".join([c["title"] or "", c["body"] or ""]))
            role = None
            if member is not None and (c["member_id"] == member.id or (c["head"] and member.id in heads)):
                role = c["role"]
            elif any(re.search(rf"(?<!\d){re.escape(s)}(?!\d)", dfd) for s in statics) or \
                    any(self._names_in(dfd, n) for n in names):
                role = c["role"]  # он и есть тот, на кого дело
            else:
                for s in statics:
                    if re.search(rf"(?<!\d){re.escape(s)}(?!\d)", text):
                        res = analyze(c["title"] or "", c["body"] or "", c["applicant"] or "", None,
                                      [{"name": names[0] if names else "", "static": s}])
                        role = res["role"] if res else "mention"
                        break
                if role is None:
                    for n in names:
                        if self._names_in(text, n) or self._names_in(norm(c["applicant"] or ""), n):
                            last = n.split()[-1].lower()
                            rx = surname_rx(re.sub(r"(а|ой|ая|ий)$", "", last) if len(last) > 5 else last)
                            res = analyze(c["title"] or "", c["body"] or "", c["applicant"] or "", rx,
                                          [{"name": n, "static": None}])
                            role = res["role"] if res else "mention"
                            break
            if role:
                out.append((c, role))
        return out

    def person_report(self, g, member, game_name=None, static=None, self_check=True):
        """Ответ на «проверь меня по форуму»: дела с ролью, статусом и ссылкой — или «чисто». Только реальные данные."""
        db = self.bot.db
        names, statics, label = self.identity(g, member, game_name, static)
        you = "тебя" if self_check else label
        if not names and not statics:
            dn = member.display_name if member is not None else (game_name or "?")
            return (f"По нику «{dn}» не понять, кто {'ты' if self_check else 'это'} в игре, а анкеты с статиком нет. "
                    f"{'Кто ты' if self_check else 'Кто это'} по игре — Имя Фамилия и статик?")
        hits = self.person_hits(g, member, names, statics)
        last = int(db.gget(g, "forum:last") or 0)
        ident = ", ".join(names[:2]) + (f", static {statics[0]}" if statics else "")
        head = f"🔎 Форум по **{label}**" + (f" ({ident})" if ident else "") + ":"
        lines = []
        for c, role in hits[:8]:
            sec = SECTIONS.get(c["forum_id"], ("раздел форума",))[0]
            lines.append(f"{'🔴' if role == 'defendant' else '⚪'} {KIND_ICON.get(c['kind'], '⚖️')} "
                         f"{(c['kind'] or 'дело').capitalize()} [{c['title'][:70]}]({c['url']}) — "
                         f"{ROLE_RU.get(role, role)} · {sec} · подал {c['applicant'] or '?'} · "
                         f"{STATUS.get(c['status'], c['status'])}")
        titles = []
        known = {c["thread_id"] for c, _ in hits}
        for t in db.all("SELECT thread_id, title FROM forum_threads WHERE guild_id=? ORDER BY last_ts DESC LIMIT 2000",
                        g.id):
            tt = norm(t["title"] or "")
            if t["thread_id"] not in known and (any(re.search(rf"(?<!\d){re.escape(s)}(?!\d)", tt) for s in statics)
                                                or any(self._names_in(tt, n) for n in names)):
                titles.append(f"· [{t['title'][:70]}]({thread_url(t['thread_id'])})")
            if len(titles) >= 5:
                break
        if not last:
            return (head + "\n" + ("\n".join(lines) + "\n" if lines else "") +
                    "Форум ещё ни разу не проверялся — вход не настроен (владелец сервера: `/форум вход`), "
                    "так что «чисто» сказать не могу.")
        body = "\n".join(lines) if lines else f"Чисто: жалоб, исков и обращений против {you} не вижу."
        if titles:
            body += "\nУпоминается в заголовках тем:\n" + "\n".join(titles)
        rx = surname_rx(self.stem(g))
        note = f"\n-# Жалобы, суды и прокуратура, что бот видел на форуме · последняя проверка <t:{last}:R>"
        if not any(rx and rx.search(n) for n in names) and not any(
                s == str(p["static"]) for p in self.people(g) for s in statics):
            note += " · не из семьи: вижу только темы, где упомянута семья"
        return head + "\n" + body + note

    def fresh_later(self, g, channel, member, game_name=None, static=None, self_check=True):
        """«Посвежее»: проверка форума в фоне, потом — новый ответ в тот же канал."""
        if self.net_lock.locked():
            return "⏳ Форум как раз проверяется — если что-то всплывёт, придёт в канал дел."
        last = int(self.bot.db.gget(g, "forum:last") or 0)
        if time.time() - last < 180:
            return "-# Форум проверял пару минут назад — данные свежие."

        async def run():
            await self.check_safe(g, manual=True)
            try:
                await channel.send(("🔄 Свежая проверка:\n" + self.person_report(g, member, game_name, static,
                                                                                self_check))[:1900])
            except discord.HTTPException:
                pass
        asyncio.create_task(run())
        return "🔄 Запустил свежую проверку форума — допишу сюда через пару минут."

    def remember(self, g, fid, it, cutoff):
        """Кэш темы из списка раздела. → [(case_id, новый статус, раздел)] для дел, у которых статус поменялся."""
        db = self.bot.db
        last = it["last_ts"] or int(time.time())
        row = db.one("SELECT * FROM forum_threads WHERE guild_id=? AND thread_id=?", g.id, it["id"])
        if row is None:
            db.q("INSERT INTO forum_threads(guild_id, thread_id, forum_id, title, author, last_ts, seen_ts) "
                 "VALUES(?,?,?,?,?,?,?)", g.id, it["id"], fid, it["title"], it["author"], last,
                 last if last < cutoff else 0)  # старьё при первом проходе не читаем
        else:
            db.q("UPDATE forum_threads SET forum_id=?, title=?, last_ts=? WHERE guild_id=? AND thread_id=?", fid,
                 it["title"], max(last, row["last_ts"] or 0), g.id, it["id"])
        c = db.one("SELECT id, status, forum_id FROM forum_cases WHERE guild_id=? AND thread_id=?", g.id, it["id"])
        if c:
            status = status_from(fid, it["prefix"], it["locked"])
            if status != c["status"] or fid != c["forum_id"]:
                return [(c["id"], status, fid)]
        return []

    async def read(self, g, client, tid, trow, rx, hrx, people, ignore, st):
        db = self.bot.db
        now = int(time.time())
        c = db.one("SELECT * FROM forum_cases WHERE guild_id=? AND thread_id=?", g.id, tid)
        if c:
            meta, posts = await client.thread(tid, latest=True)
            st["read"] += 1
            last = c["last_post_ts"] or 0
            new = [p for p in posts if p["ts"] > last] if last else []  # дело только что заведено/импорт — без спама
            status = status_from(meta["forum_id"] or c["forum_id"], meta["prefix"], meta["locked"])
            decision = c["decision"]
            if status != "pending" and posts:
                ans = [p for p in posts if p["author"] != (c["applicant"] or "")] or posts
                decision = ans[-1]["text"][:1500]
            top = max([p["ts"] for p in posts] + [last])
            db.q("UPDATE forum_cases SET last_post_ts=?, status=?, forum_id=COALESCE(?, forum_id), decision=?, "
                 "checked_ts=?, updated_ts=? WHERE id=?", top, status, meta["forum_id"], decision, now, now, c["id"])
            db.q("UPDATE forum_threads SET seen_ts=last_ts WHERE guild_id=? AND thread_id=?", g.id, tid)
            if c["role"] != "defendant":
                return
            if not c["message_id"]:
                await self.post_card(g, c["id"])
                c = db.one("SELECT * FROM forum_cases WHERE id=?", c["id"])
            notes = []
            for p in new[-3:]:
                mark = "⚖️" if re.search(r"решени|постанов|приговор|удовлетвор|отказ|ходатайств|прокурор", p["text"],
                                         re.I) else "💬"
                notes.append(f"{mark} **{p['author'] or 'кто-то'}** · <t:{p['ts']}:f>\n{p['text'][:500]}\n"
                             f"<{BASE}/posts/{p['id']}/>")
            if len(new) > 3:
                notes.insert(0, f"…и ещё {len(new) - 3} сообщ. раньше — всё в теме: <{c['url']}>")
            if status != c["status"]:
                notes.append(f"📌 Статус дела: **{STATUS.get(status, status)}**")
            if notes:
                st["upd"] += 1
                await self.refresh_card(g, c["id"], "\n\n".join(notes))
            return
        again = bool(trow and trow["seen_ts"])
        meta, posts = await client.thread(tid, latest=again)
        st["read"] += 1
        if again:
            posts = [p for p in posts if p["ts"] > trow["seen_ts"]]
        db.q("UPDATE forum_threads SET seen_ts=last_ts WHERE guild_id=? AND thread_id=?", g.id, tid)
        title = meta["title"] or (trow["title"] if trow else "")
        author = trow["author"] if trow else (posts[0]["author"] if posts else "")
        text = "\n\n".join(p["text"] for p in posts[:10])
        res = analyze(title, text, author, rx, people, hrx, ignore)
        if not res:
            return
        fid = meta["forum_id"] or (trow["forum_id"] if trow else None)
        sec, kind = SECTIONS.get(fid, ("раздел форума", "жалоба", ""))[:2]
        info = await self.classify(g, title, text, author, sec, res, people)
        who = res["who"] or find_person(people, info.get("who") or "")
        head = bool(res["head"] or info.get("head") or (who and who.get("head")))
        defendant = (who["name"] if who else info.get("who") or res["name"]) or "—"
        if head and not who:
            defendant = f"{defendant} (Глава семьи)"
        cur = db.q("INSERT OR IGNORE INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, "
                   "member_id, head, applicant, summary, case_no, status, body, last_post_ts, checked_ts, created_ts, "
                   "updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   g.id, tid, fid, title[:200], thread_url(tid), kind, info["role"], defendant[:150],
                   who.get("user_id") if who else None, int(head), (info.get("applicant") or author)[:100],
                   (info.get("summary") or title)[:300], info.get("case_no") or case_number(title),
                   status_from(fid, meta["prefix"], meta["locked"]), text[:4000],
                   max([p["ts"] for p in posts] or [now]), now, now, now)
        if info["role"] == "defendant" and cur.lastrowid:
            st["new"] += 1
            await self.post_card(g, cur.lastrowid, ping=True)

    async def classify(self, g, title, text, author, section, res, people):
        """Роль члена семьи в теме: ИИ (строгий JSON), без ИИ или при ошибке — эвристика analyze()."""
        base = {"role": res["role"], "who": res["name"], "head": res["head"], "applicant": author,
                "summary": title[:150], "case_no": case_number(title)}
        ai = self.bot.ai_for(g)
        if not getattr(ai, "on", False):
            return base
        heads = [p["name"] for p in people if p.get("head")]
        system = CLASSIFY.format(family=self.bot.family(g).get("name") if hasattr(self.bot, "family") else config.FAMILY,
                                 stem=self.stem(g), head=", ".join(heads) or "неизвестно",
                                 titles=self.bot.db.gget(g, "forum:titles") or "—",
                                 people=", ".join(f"{p['name']} ({p['static'] or 'статик ?'})" for p in people)[:600]
                                 or "—")
        try:
            data = await ai.ask_json(system, f"Раздел: {section}\nЗаголовок: {title}\nАвтор темы: {author}\n\n"
                                             f"Текст:\n{text[:4000]}", max_tokens=350)
        except Exception as e:  # noqa: BLE001 — лимит/сеть: эвристика
            log.info("forumwatch: ИИ не разобрал тему (%s) — эвристика", type(e).__name__)
            return base
        if not isinstance(data, dict):
            return base
        out = dict(base)
        if data.get("role") in ROLE_RANK:
            out["role"] = data["role"]
        for k in ("who", "applicant", "summary", "case_no"):
            if isinstance(data.get(k), str) and data[k].strip():
                out[k] = data[k].strip()[:300]
        out["head"] = bool(data.get("head")) or base["head"]
        return out

    async def tell_owner(self, g):
        """Один раз: владельцу сервера в ЛС — без входа форум закрыт, нужен /форум вход."""
        db = self.bot.db
        if db.gget(g, "forum:nologin_told"):
            return
        db.gset(g, "forum:nologin_told", "1")
        text = ("⚖️ Форум-монитор: разделы жалоб и судов на forum.russia.online видны только после входа. "
                f"Введи общий аккаунт семьи на сервере «{g.name}»: `/форум вход` (только владелец сервера; бот только "
                "читает, пароль хранится зашифрованным).")
        owner = g.owner
        if owner is None and g.owner_id:
            try:
                owner = await self.bot.fetch_user(g.owner_id)
            except discord.HTTPException:
                owner = None
        if owner:
            try:
                await owner.send(text)
                return
            except discord.HTTPException:
                pass
        await self.alert(g, text)

    async def alert(self, g, text):
        ch = self.bot.ch(g, "botcmd") or self.bot.ch(g, "council_chat")
        if ch:
            try:
                await ch.send(text)
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=1)
    async def watch_loop(self):
        db = self.bot.db
        for g in list(self.bot.home_guilds):
            if not self.bot.feature(g, "forum:watch") or db.gget(g, "forum:auth_fail"):
                continue
            if time.time() < self.backoff.get(g.id, 0):
                continue
            if time.time() - int(db.gget(g, "forum:last") or 0) < self.interval(g) * 60:
                continue
            await self.check_safe(g)

    @watch_loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    async def deny(self, inter):
        await inter.response.send_message("⛔ Только Глава, заместитель или владелец сервера.", ephemeral=True)

    @group.command(name="вход", description="Общий аккаунт семьи на форуме — только владелец сервера (бот только читает)")
    async def account(self, inter: discord.Interaction):
        if inter.user.id != inter.guild.owner_id:
            return await inter.response.send_message("⛔ Вход на форум вводит только владелец сервера.", ephemeral=True)
        await inter.response.send_modal(AccountModal(self))

    @group.command(name="канал", description="Канал для карточек дел (без канала — создам «⚖・суды-и-жалобы»)")
    @app_commands.rename(channel="канал", lead_only="только_руководство")
    async def set_channel(self, inter: discord.Interaction, channel: discord.TextChannel = None,
                          lead_only: bool = False):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        await inter.response.defer(ephemeral=True, thinking=True)
        g, db = inter.guild, self.bot.db
        if channel:
            db.gset(g, "ch:forumwatch", channel.id)
            db.gset(g, "chmap:forumwatch", "1")  # своё — сборка сервера не трогает
            ch = channel
        else:
            ch = await self.ensure_channel(g)
        if not ch:
            return await inter.followup.send("❌ Не смог создать канал — у бота нет прав. Укажи готовый канал.",
                                             ephemeral=True)
        if lead_only:
            try:
                for key in config.FAMILY_PLUS:
                    if key in config.LEAD:
                        continue
                    r = self.bot.role(g, key)
                    if r:
                        await ch.set_permissions(r, view_channel=False, reason="Суды и жалобы — только руководство")
                for key in config.LEAD:
                    r = self.bot.role(g, key)
                    if r:
                        await ch.set_permissions(r, view_channel=True, reason="Суды и жалобы — руководство")
                db.gset(g, "chmap:forumwatch", "1")
            except discord.HTTPException:
                return await inter.followup.send(f"⚠️ Канал {ch.mention} задан, но права поменять не смог.",
                                                 ephemeral=True)
        self.seed(g)
        await self.post_missing_cards(g)
        await inter.followup.send(f"✅ Карточки дел — в {ch.mention}" + (" (видит только руководство)." if lead_only
                                                                         else "."), ephemeral=True)

    @group.command(name="интервал", description="Как часто проверять форум (минут, 5–120)")
    @app_commands.rename(minutes="минут")
    async def set_interval(self, inter: discord.Interaction, minutes: app_commands.Range[int, 5, 120]):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        self.bot.db.gset(inter.guild, "forum:interval", minutes)
        await inter.response.send_message(f"⏱ Проверяю форум раз в {minutes} мин.", ephemeral=True)

    @group.command(name="проверить", description="Проверить форум прямо сейчас")
    async def check_now(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.LEAD) and inter.user.id != inter.guild.owner_id:
            return await self.deny(inter)
        if not self.creds(inter.guild):
            return await inter.response.send_message("Аккаунт форума не подключён: `/форум вход`.", ephemeral=True)
        if self.net_lock.locked():
            return await inter.response.send_message("⏳ Проверка уже идёт — результат будет в канале дел.",
                                                      ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        res = await self.check_safe(inter.guild, manual=True)
        await inter.followup.send(f"🔎 {res}", ephemeral=True)

    @group.command(name="дела", description="Активные дела против семьи (и тихий лог упоминаний)")
    @app_commands.rename(everything="с_упоминаниями")
    async def cases(self, inter: discord.Interaction, everything: bool = False):
        if not self.bot.has(inter.user, config.CORE):
            return await inter.response.send_message("⛔ Только для семьи.", ephemeral=True)
        g, db = inter.guild, self.bot.db
        self.seed(g)
        rows = db.all("SELECT * FROM forum_cases WHERE guild_id=? AND role='defendant' ORDER BY "
                      "(status='pending') DESC, updated_ts DESC LIMIT 20", g.id)
        lines = []
        for c in rows:
            link = f"[{c['title'][:70]}]({c['url']})"
            lines.append(f"{STATUS.get(c['status'], c['status'])[:2]} {KIND_ICON.get(c['kind'], '⚖️')} {link} — "
                         f"{('👑 ' if c['head'] else '') + (c['defendant'] or '?')[:50]}")
        if everything:
            quiet = db.all("SELECT * FROM forum_cases WHERE guild_id=? AND role<>'defendant' ORDER BY id DESC LIMIT 10",
                           g.id)
            if quiet:
                lines += ["", "**Тихий лог (не ответчик):**"] + [
                    f"· [{c['title'][:60]}]({c['url']}) — {c['defendant'] or '?'}: {ROLE_RU.get(c['role'], c['role'])}"
                    for c in quiet]
        last = int(db.gget(g, "forum:last") or 0)
        state = ("🔴 вход не работает: " + db.gget(g, "forum:auth_fail")) if db.gget(g, "forum:auth_fail") else \
            ("🟢 слежу" if self.creds(g) and self.bot.feature(g, "forum:watch") else "⚪ выключено (`/форум вход`)")
        head = f"{state} · раз в {self.interval(g)} мин" + (f" · последняя проверка <t:{last}:R>" if last else "")
        if db.gget(g, "forum:last_err"):
            head += f"\n⚠️ {db.gget(g, 'forum:last_err')}"
        await inter.response.send_message(embed=self.bot.embed("⚖️ Дела на форуме", head + "\n\n" + (
            "\n".join(lines) or "Дел против семьи не найдено."), config.COLOR, g), ephemeral=True)

    @group.command(name="исключения", description="Кого/что не отслеживать (через запятую; «-» — очистить)")
    @app_commands.rename(words="слова")
    async def ignore(self, inter: discord.Interaction, words: str = None):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        db = self.bot.db
        if words:
            db.gset(inter.guild, "forum:ignore", "" if words.strip() == "-" else words.strip()[:500])
        cur = db.gget(inter.guild, "forum:ignore") or "—"
        await inter.response.send_message(f"🚫 Не отслеживаю темы со словами: {cur}", ephemeral=True)

    @group.command(name="должности", description="Должности Главы, жалобы на которые тоже ловить (через запятую)")
    @app_commands.rename(titles="должности")
    async def head_titles(self, inter: discord.Interaction, titles: str = None):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        db = self.bot.db
        if titles:
            db.gset(inter.guild, "forum:titles", "" if titles.strip() == "-" else titles.strip()[:300])
        await inter.response.send_message(f"👑 Должности Главы: {db.gget(inter.guild, 'forum:titles') or '—'}",
                                          ephemeral=True)

    @people_group.command(name="список", description="Кого из семьи ищу на форуме")
    async def people_list(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.CORE):
            return await inter.response.send_message("⛔ Только для семьи.", ephemeral=True)
        ps = self.people(inter.guild)
        lines = [f"{'👑 ' if p['head'] else '· '}{p['name']} · `{p['static'] or '—'}`"
                 + (f" · <@{p['user_id']}>" if p["user_id"] else "") for p in ps]
        await inter.response.send_message(embed=self.bot.embed(
            f"👥 Ищу на форуме: фамилия «{self.stem(inter.guild)}» во всех формах",
            ("\n".join(lines) or "Список пуст — ищу просто по фамилии.") +
            "\n\n-# Ростер берётся из ников и анкет семьи; добавить вручную — `/форум участники добавить`.",
            config.COLOR, inter.guild), ephemeral=True)

    @people_group.command(name="добавить", description="Добавить члена семьи для поиска на форуме")
    @app_commands.rename(name="имя_фамилия", static="статик", member="участник", head="глава")
    async def people_add(self, inter: discord.Interaction, name: str, static: str = None,
                         member: discord.Member = None, head: bool = False):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        name = norm(name).replace("\n", " ")[:60]
        st = re.sub(r"\D", "", static or "")[:9] or None
        db = self.bot.db
        if head:
            db.q("UPDATE forum_people SET head=0 WHERE guild_id=?", inter.guild.id)
        db.q("INSERT OR REPLACE INTO forum_people(guild_id, name, static_id, user_id, head) VALUES(?,?,?,?,?)",
             inter.guild.id, name, st, member.id if member else None, int(head))
        await inter.response.send_message(f"✅ {'👑 ' if head else ''}{name} · `{st or '—'}`"
                                          + (f" · {member.mention}" if member else "") + " — ищу на форуме.",
                                          ephemeral=True)

    @people_group.command(name="убрать", description="Убрать из ручного списка")
    @app_commands.rename(name="имя_фамилия")
    async def people_del(self, inter: discord.Interaction, name: str):
        if not is_manager(self.bot, inter):
            return await self.deny(inter)
        n = self.bot.db.q("DELETE FROM forum_people WHERE guild_id=? AND name=?", inter.guild.id, name).rowcount
        await inter.response.send_message("🗑 Убрал." if n else "Такого в ручном списке нет.", ephemeral=True)

    @people_del.autocomplete("name")
    async def people_ac(self, inter: discord.Interaction, current: str):
        rows = self.bot.db.all("SELECT name FROM forum_people WHERE guild_id=? ORDER BY name", inter.guild.id)
        cur = (current or "").lower()
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if cur in r["name"].lower()][:25]


async def setup(bot):
    await bot.add_cog(ForumWatch(bot))
