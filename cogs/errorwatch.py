# -*- coding: utf-8 -*-
"""Авто-отчёты об ошибках разработчику.
• ErrorCollector — logging.Handler на корневом логгере (ERROR и выше): каждую ошибку сводит к подписи
  «тип исключения + место в нашем коде» (повторы только считаются), текст чистит от секретов (clean()).
• Раз в 10 мин новые ошибки → ЛС разработчику (dev:user, по умолчанию владелец домашнего сервера): что, где,
  сколько раз + файл с трассировками.
• Если задан токен GitHub (/разработчик github) — на каждую новую ошибку заводится issue с меткой bot-error
  в репозитории (dev:gh_repo). Claude Code в следующей сессии: `gh issue list --label bot-error` → чинит → закрывает.
• Шум сети (переподключения Discord/голоса) — не баги кода, не отчитываемся (NOISE)."""
import collections
import io
import json
import logging
import os
import re
import time
import traceback

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.botcore import guarded

log = logging.getLogger("family-bot.errors")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPO = "mousetilt/family-discord-bot"
NOISE = ("Attempting a reconnect", "Disconnected from voice", "Shard ID None", "WebSocket closed",
         "Can't keep up", "heartbeat blocked")
SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_errors(
    sig TEXT PRIMARY KEY, title TEXT, sample TEXT, count INTEGER DEFAULT 1, first_ts INTEGER, last_ts INTEGER,
    reported INTEGER DEFAULT 0, issue INTEGER, version TEXT);
"""
SECRETS = [
    (re.compile(r"[MNO][A-Za-z\d_-]{20,}\.[\w-]{5,7}\.[\w-]{20,}"), "<discord-token>"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{10,}"), "<groq-key>"),
    (re.compile(r"\bAIza[\w-]{20,}"), "<gemini-key>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}|github_pat_\w{20,}"), "<github-token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<api-key>"),
    (re.compile(r"\borg_[A-Za-z0-9]{10,}"), "<org>"),
    (re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"), "<webhook>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"(?i)(key|token|secret|password)=([^&\s\"']+)"), r"\1=<hidden>"),
    (re.compile(r"\b\d{15,21}\b"), "<id>"),
]


def clean(text):
    """Вырезать секреты и личные ID (отчёт может уйти в публичный репозиторий)."""
    s = str(text or "")
    for rx, sub in SECRETS:
        s = rx.sub(sub, s)
    return s.replace(ROOT, ".").replace("/home/container", ".")


def signature(record):
    """(подпись, заголовок, текст) — одна и та же ошибка из одного места даёт одну подпись."""
    exc = record.exc_info
    if exc and exc[0]:
        tb = traceback.extract_tb(exc[2])
        ours = [f for f in tb if f.filename.startswith(ROOT) and "site-packages" not in f.filename] or tb[-1:]
        where = ours[-1] if ours else None
        loc = f"{os.path.relpath(where.filename, ROOT)}:{where.lineno} ({where.name})" if where else record.name
        name = exc[0].__name__
        # discord.HTTPException: код ошибки Discord тоже часть подписи (10062 ≠ 50035)
        code = getattr(exc[1], "code", None)
        sig = f"{name}:{code or ''}@{loc}"
        title = f"{name}{f' {code}' if code else ''} в {loc}"
        body = "".join(traceback.format_exception(*exc))[-6000:]
        head = record.getMessage()
        return sig, title, clean(f"{head}\n\n{body}")
    msg = record.getMessage()
    tmpl = re.sub(r"\d+", "#", str(record.msg))[:120]
    return f"log:{record.name}:{tmpl}", f"{record.name}: {clean(msg)[:90]}", clean(msg)[:4000]


class ErrorCollector(logging.Handler):
    """Копит ошибки в памяти (emit может прийти из любого потока); в базу их пишет цикл кога."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.queue = collections.deque(maxlen=500)

    def emit(self, record):
        try:
            text = record.getMessage()
            if any(n in text for n in NOISE) or record.name.startswith("family-bot.errors") or \
                    ("Task was destroyed but it is pending" in text and "coro=<Event.wait()" in text):
                # utils.sane_wait_for в discord.py/disnake при таймауте подключения к войсу бросает
                # задачи Event.wait() без отмены — сборщик мусора потом ругается. Безвредно.
                return
            self.queue.append((signature(record), int(record.created)))
        except Exception:
            pass


class TokenModal(discord.ui.Modal, title="GitHub: задачи об ошибках"):
    token = discord.ui.TextInput(label="Токен GitHub (только Issues: Read & write)", max_length=200,
                                 placeholder="github_pat_… · «-» — удалить")
    repo = discord.ui.TextInput(label="Репозиторий (владелец/имя)", max_length=100, default=DEFAULT_REPO)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        db, tok, repo = self.cog.bot.db, self.token.value.strip(), self.repo.value.strip().strip("/")
        if tok == "-":
            db.set("dev:gh_token", "")
            return await inter.followup.send("🗑 Токен GitHub удалён — задачи больше не создаются.", ephemeral=True)
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            return await inter.followup.send("❌ Репозиторий в виде `владелец/имя`.", ephemeral=True)
        ok, info = await self.cog.gh_check(tok, repo)
        if not ok:
            return await inter.followup.send(f"❌ Токен не подошёл: {info}", ephemeral=True)
        db.set("dev:gh_token", tok)
        db.set("dev:gh_repo", repo)
        log.warning("Отчёты об ошибках: подключён GitHub %s", repo)
        await inter.followup.send(
            f"✅ Подключено: новые ошибки будут задачами в **{repo}** с меткой `bot-error`."
            + ("\n⚠️ Репозиторий **публичный** — задачи видны всем. Секреты, ключи и Discord ID бот вырезает, но "
               "текст ошибок (например, ники) может попасть. Надёжнее сделать репозиторий приватным." if info == "public"
               else ""), ephemeral=True)


class ErrorWatch(commands.Cog):
    dev = app_commands.Group(name="разработчик", description="Отчёты об ошибках бота разработчику",
                             default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()
        self.collector = ErrorCollector()
        logging.getLogger().addHandler(self.collector)
        self.report_loop.start()

    def cog_unload(self):
        logging.getLogger().removeHandler(self.collector)
        self.report_loop.cancel()

    def version(self):
        try:
            with open(os.path.join(ROOT, "data", "changelog.json"), encoding="utf-8") as f:
                return json.load(f)[0]["v"]
        except Exception:
            return "?"

    def developer(self):
        uid = self.bot.db.get("dev:user")
        if uid:
            return self.bot.get_user(int(uid))
        home = next((g for g in self.bot.guilds if self.bot.is_home(g)), None)
        return home.owner if home else None

    def allowed(self, inter):
        return bool(inter.guild) and self.bot.is_home(inter.guild) and (
            inter.user.id == inter.guild.owner_id or str(inter.user.id) == str(self.bot.db.get("dev:user") or ""))

    # ---------- сбор ----------
    def flush(self):
        db = self.bot.db
        while self.collector.queue:
            (sig, title, text), ts = self.collector.queue.popleft()
            if db.one("SELECT 1 FROM bot_errors WHERE sig=?", sig):
                db.q("UPDATE bot_errors SET count=count+1, last_ts=?, sample=? WHERE sig=?", ts, text, sig)
            else:
                db.q("INSERT INTO bot_errors(sig, title, sample, first_ts, last_ts, version) VALUES(?,?,?,?,?,?)",
                     sig, title[:200], text, ts, ts, self.version())

    # ---------- GitHub ----------
    async def gh(self, method, path, token, payload=None):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "DiscordFamilyBot-errors", "X-GitHub-Api-Version": "2022-11-28"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=headers) as s:
            async with s.request(method, f"https://api.github.com{path}", json=payload) as r:
                return r.status, (await r.json(content_type=None) if r.content_length != 0 else {})

    async def gh_check(self, token, repo):
        try:
            st, data = await self.gh("GET", f"/repos/{repo}", token)
        except Exception as e:
            return False, type(e).__name__
        if st != 200:
            return False, f"GitHub ответил {st} (нет доступа к {repo}?)"
        if not data.get("has_issues", True):
            return False, "в репозитории выключены задачи (Issues) — Settings → Features → Issues"
        return True, "private" if data.get("private") else "public"

    async def gh_issue(self, row):
        token, repo = self.bot.db.get("dev:gh_token"), self.bot.db.get("dev:gh_repo") or DEFAULT_REPO
        if not token:
            return None
        body = (f"Автоотчёт бота. Версия **{row['version']}**, повторов: {row['count']}, впервые "
                f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(row['first_ts']))} UTC.\n\n"
                f"Подпись: `{row['sig'][:200]}`\n\n```\n{row['sample'][:6000]}\n```\n\n"
                "_Секреты и Discord ID вырезаны ботом._")
        try:
            st, data = await self.gh("POST", f"/repos/{repo}/issues", token,
                                     {"title": f"🐞 {row['title']}"[:250], "body": body, "labels": ["bot-error"]})
        except Exception as e:
            log.warning("GitHub issue: %s", type(e).__name__)
            return None
        if st == 201:
            return data.get("number")
        log.warning("GitHub issue: ответ %s", st)
        return None

    # ---------- отчёт ----------
    @tasks.loop(minutes=10)
    @guarded
    async def report_loop(self):
        await self.report()

    async def report(self, force=False):
        self.flush()
        db = self.bot.db
        rows = db.all("SELECT * FROM bot_errors WHERE reported=0 ORDER BY first_ts")
        if not rows and not force:
            return 0
        for r in rows:
            num = await self.gh_issue(r)
            db.q("UPDATE bot_errors SET reported=1, issue=? WHERE sig=?", num, r["sig"])
        dev = self.developer()
        if not dev:
            return len(rows)
        if not rows:
            return 0
        repo = db.get("dev:gh_repo") or DEFAULT_REPO
        lines = []
        for r in rows[:15]:
            r2 = db.one("SELECT issue FROM bot_errors WHERE sig=?", r["sig"])
            link = f" · [задача #{r2['issue']}](https://github.com/{repo}/issues/{r2['issue']})" if r2["issue"] else ""
            lines.append(f"• **{r['title'][:120]}** — {r['count']}×{link}")
        e = discord.Embed(title=f"🐞 Новые ошибки бота: {len(rows)}", color=0xE53935,
                          description="\n".join(lines)[:3800])
        e.set_footer(text=f"Версия {self.version()} · подробности — в файле. Перешли его Claude, или подключи "
                          "GitHub: /разработчик github")
        blob = "\n\n".join(f"### {r['title']}\nподпись: {r['sig']}\nповторов: {r['count']}\n{r['sample']}"
                           for r in rows)
        try:
            await dev.send(embed=e, file=discord.File(io.BytesIO(blob.encode("utf-8")), filename="errors.txt"))
        except discord.HTTPException:
            log.warning("Отчёт об ошибках: не смог написать разработчику в ЛС")
        return len(rows)

    @report_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    @dev.command(name="кому", description="Кому присылать отчёты об ошибках (по умолчанию — владельцу сервера)")
    @app_commands.rename(member="разработчик")
    async def c_who(self, inter: discord.Interaction, member: discord.Member):
        if not self.allowed(inter):
            return await inter.response.send_message("⛔ Только владелец главного сервера бота.", ephemeral=True)
        self.bot.db.set("dev:user", member.id)
        await inter.response.send_message(f"✅ Отчёты об ошибках будут приходить {member.mention} в ЛС.", ephemeral=True)

    @dev.command(name="github", description="Заводить задачи на GitHub по каждой новой ошибке (токен — в окне)")
    async def c_github(self, inter: discord.Interaction):
        if not self.allowed(inter):
            return await inter.response.send_message("⛔ Только владелец главного сервера бота.", ephemeral=True)
        await inter.response.send_modal(TokenModal(self))

    @dev.command(name="ошибки", description="Сводка ошибок: сколько, какие повторяются (и прислать новые сейчас)")
    async def c_status(self, inter: discord.Interaction):
        if not self.allowed(inter):
            return await inter.response.send_message("⛔ Только владелец главного сервера бота.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        sent = await self.report(force=True)
        db = self.bot.db
        top = db.all("SELECT * FROM bot_errors ORDER BY last_ts DESC LIMIT 10")
        dev = self.developer()
        gh = db.get("dev:gh_token")
        lines = [f"🐞 Отчёты: {getattr(dev, 'mention', 'никому')} в ЛС"
                 + (f" + задачи GitHub ({db.get('dev:gh_repo') or DEFAULT_REPO})" if gh else " (GitHub не подключён)"),
                 f"Новых только что отправлено: {sent}", ""]
        lines += [f"• <t:{r['last_ts']}:R> **{r['title'][:90]}** — {r['count']}×"
                  + (f" · #{r['issue']}" if r["issue"] else "") for r in top]
        await inter.followup.send("\n".join(lines)[:1990] if top else "\n".join(lines[:2]) + "\n✅ Ошибок не было.",
                                  ephemeral=True)


async def setup(bot):
    await bot.add_cog(ErrorWatch(bot))
