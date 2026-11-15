# -*- coding: utf-8 -*-
"""Служебная панель владельца: статус, перезапуск, обновление кода из GitHub, горячая
перезагрузка модуля. ТОЛЬКО для Главы/заместителя (или владельца сервера).
БЕЗ выполнения произвольного кода/команд — только эти фиксированные действия.

Обновления (data/changelog.json — список версий, новые сверху: v, title, added, fixed, members):
• update_loop раз в 30 мин читает changelog.json с GitHub. Есть версия, которой у нас нет, — один раз пишет
  владельцу домашнего сервера (ЛС + канал бот-команд) «что нового» с кнопкой «Обновить бота» (upd:go).
• После перезапуска на новой версии каждой семье в канал бот-команд — «Бот обновлён: что нового»
  с кнопкой «Опубликовать в объявлениях» (upd:pub:<v>) — текст members для рядовых.
Выпуск версии: запись в НАЧАЛО data/changelog.json → commit → push."""
import asyncio
import datetime as dt
import os
import shutil
import sys
import time

import json
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, safe_dm

log = logging.getLogger("family-bot.maintenance")
REMOTE_CHANGELOG = "https://raw.githubusercontent.com/mousetilt/family-discord-bot/main/data/changelog.json"

START_TS = time.time()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # папка с bot.py


def _fmt_uptime(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    parts.append(f"{m} мин")
    return " ".join(parts)


async def _run(cmd, cwd=ROOT, timeout=120):
    """Запускает ФИКСИРОВАННУЮ команду (git/pip) и возвращает (код, вывод). Не для ввода пользователя."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "⏳ Превышено время ожидания."
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def load_changelog(text=None):
    """Список версий (новые сверху). Битый файл — пустой список, бот из-за него не падает."""
    try:
        if text is None:
            with open(os.path.join(ROOT, "data", "changelog.json"), encoding="utf-8") as f:
                text = f.read()
        data = json.loads(text)
        return [e for e in data if isinstance(e, dict) and e.get("v")] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def newer(entries, since):
    """Записи новее версии since (список новые сверху). since нет в списке — только самая свежая."""
    vs = [e["v"] for e in entries]
    if since in vs:
        return entries[:vs.index(since)]
    return entries[:1]


def entry_text(e, members=False):
    if members:
        return str(e.get("members") or "")[:3900]
    out = []
    if e.get("added"):
        out.append("**✨ Добавили**\n" + "\n".join(f"• {x}" for x in e["added"]))
    if e.get("fixed"):
        out.append("**🛠 Починили**\n" + "\n".join(f"• {x}" for x in e["fixed"]))
    return "\n\n".join(out)[:3900]


class UpdateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"upd:(?P<a>go|pub):(?P<v>[\w.\-]*)"):
    def __init__(self, a, v=""):
        label, emoji, style = {"go": ("Обновить бота", "⬇️", discord.ButtonStyle.success),
                               "pub": ("Опубликовать в объявлениях", "📢", discord.ButtonStyle.primary)}[a]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"upd:{a}:{v}"[:100]))
        self.a, self.v = a, v

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], match["v"])

    async def callback(self, inter: discord.Interaction):
        cog = inter.client.get_cog("Maintenance")
        if self.a == "go":
            await cog.do_update(inter)
        else:
            await cog.publish(inter, self.v)


def _restart_in_place(via_launcher=False):
    """Перезапускает текущий процесс бота на месте (не зависит от авто-рестарта хоста).
    via_launcher=True — через launcher.py, который перед стартом подтянет код из GitHub."""
    script = "launcher.py" if via_launcher and os.path.isfile(os.path.join(ROOT, "launcher.py")) else "bot.py"
    os.execv(sys.executable, [sys.executable, "-u", os.path.join(ROOT, script)])


class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._announced = False
        self.update_loop.start()

    def cog_unload(self):
        self.update_loop.cancel()

    def home(self):
        return next((g for g in self.bot.guilds if self.bot.is_home(g)), None)

    # ---------- уведомления об обновлениях ----------
    @tasks.loop(minutes=30)
    @guarded
    async def update_loop(self):
        """На GitHub есть версия, которой у нас нет, — один раз на каждую новую версию пишем владельцу."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": "family-bot-updater"}) as s:
                async with s.get(REMOTE_CHANGELOG) as r:
                    if r.status != 200:
                        return
                    remote = load_changelog(await r.text())
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return
        local = load_changelog()
        if not remote or remote[0]["v"] in {e["v"] for e in local}:
            return  # у нас уже эта версия
        top = remote[0]["v"]
        if self.bot.db.get("update:notified") == top:
            return
        home = self.home()
        if not home:
            return
        fresh = newer(remote, local[0]["v"] if local else None)
        e = self.bot.embed(f"⬇️ Доступно обновление бота · {fresh[0].get('title', top)}",
                           "\n\n".join(entry_text(x) for x in fresh[:3])[:3700]
                           + "\n\nНажми кнопку — бот подтянет код и перезапустится (~20 сек). Или команда `/обновить`.",
                           config.GOLD, home)
        e.set_footer(text=f"Версия {top}")
        sent = False
        c = self.bot.ch(home, "botcmd")
        if c:
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(UpdateButton("go"))
                await c.send(embed=e, view=v)
                sent = True
            except discord.HTTPException:
                pass
        if home.owner:
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(UpdateButton("go"))
                await home.owner.send(embed=e, view=v)
                sent = True
            except discord.HTTPException:
                pass
        if sent:
            self.bot.db.set("update:notified", top)
            log.warning("Доступно обновление %s — владельцу сообщено", top)

    @update_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # пусть сначала поднимется всё остальное

    @commands.Cog.listener()
    async def on_ready(self):
        """Запустились на новой версии — рассказать каждой семье, что нового."""
        if self._announced:
            return
        self._announced = True
        entries = load_changelog()
        if not entries:
            return
        top = entries[0]["v"]
        for g in list(self.bot.home_guilds):
            db = self.bot.db
            seen = db.gget(g, "changelog:seen")
            if seen == top:
                continue
            db.gset(g, "changelog:seen", top)
            c = self.bot.ch(g, "botcmd")
            if not c or not db.gget(g, "role:leader"):
                continue  # сервер ещё не собран — новостей не шлём
            fresh = newer(entries, seen)
            e = self.bot.embed(f"🎩 Бот обновлён · {fresh[0].get('title', top)}",
                               "\n\n".join(entry_text(x) for x in fresh[:3])[:3700], config.GOLD, g)
            e.set_footer(text=f"Версия {top}")
            v = discord.ui.View(timeout=None)
            if any(x.get("members") for x in fresh):
                e.description += "\n\n📢 Рассказать семье — кнопкой ниже (пост в объявления)."
                v.add_item(UpdateButton("pub", top))
            try:
                await c.send(embed=e, view=v)
            except discord.HTTPException:
                pass

    async def publish(self, inter: discord.Interaction, ver):
        """Пост «что нового» для рядовых в канал объявлений (Глава/зам/совет)."""
        g = inter.guild
        if not g or not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Публикует руководство семьи.", ephemeral=True)
        e = next((x for x in load_changelog() if x["v"] == ver), None)
        c = self.bot.ch(g, "news")
        if not e or not e.get("members") or not c:
            return await inter.response.send_message("Нечего публиковать или нет канала объявлений.", ephemeral=True)
        if self.bot.db.gget(g, f"changelog:pub:{ver}"):
            return await inter.response.send_message("✅ Уже опубликовано.", ephemeral=True)
        emb = self.bot.embed(f"🎩 Швейцар обновился · {e.get('title', ver)}", self.bot.brand(g, entry_text(e, True)),
                             config.GOLD, g)
        news = self.bot.role(g, "n_news")
        try:
            await c.send(content=news.mention if news else None, embed=emb,
                         allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            return await inter.response.send_message("❌ Не смог написать в объявления — проверь права бота.",
                                                     ephemeral=True)
        self.bot.db.gset(g, f"changelog:pub:{ver}", inter.user.id)
        await inter.response.send_message(f"📢 Опубликовано в {c.mention}.", ephemeral=True)
        try:
            await inter.message.edit(view=None)
        except discord.HTTPException:
            pass

    def _allowed(self, inter):
        # бот один на все семьи: перезапуск/обновление — только с домашнего сервера
        return bool(inter.guild) and self.bot.is_home(inter.guild) and (inter.user.id == inter.guild.owner_id
                                      or self.bot.has(inter.user, ["leader", "deputy"]))

    async def _deny(self, inter):
        await inter.response.send_message("⛔ Только для Главы и заместителя домашнего сервера бота.", ephemeral=True)

    # ---------- статус ----------
    @app_commands.command(name="статус", description="Состояние бота: ИИ, аптайм, голос (руководство)")
    @app_commands.default_permissions(administrator=True)
    async def status_cmd(self, inter: discord.Interaction):
        if not self._allowed(inter):
            return await self._deny(inter)
        ai = getattr(self.bot, "ai", None)
        lines = [f"**Состояние «{self.bot.user.display_name}»**"]
        lines.append(f"⏱ Аптайм: {_fmt_uptime(time.time() - START_TS)}")
        lines.append(f"🌐 Серверов: {len(self.bot.guilds)} · пинг: {round(self.bot.latency * 1000)} мс")
        # версия кода — чтобы видеть, дошло ли обновление (сверить с последним коммитом на GitHub)
        ver = "неизвестна (нет git)"
        if os.path.isdir(os.path.join(ROOT, ".git")) and shutil.which("git"):
            try:
                code, out = await asyncio.wait_for(_run(["git", "log", "-1", "--format=%h · %s · %cr"]), 5)
                if code == 0 and out.strip():
                    ver = f"`{out.strip()[:120]}`"
            except Exception:
                pass
        cl = load_changelog()
        lines.append(f"🧩 Версия: **{cl[0]['v']}** · код {ver}" if cl else f"🧩 Версия кода: {ver}")
        # ИИ
        if ai and getattr(ai, "on", False):
            lines.append(f"🤖 ИИ: **{ai.name}**, модель `{ai.models[0]}`"
                         + (f" (резерв — {ai.backup})" if getattr(ai, "backup", None) else ""))
            now = time.monotonic()
            cooling = [m for (p, m), until in getattr(ai, "cool", {}).items() if until > now]
            dead = [m for (p, m) in getattr(ai, "dead", set())]
            if cooling:
                lines.append(f"   ⏳ на кулдауне (лимит): {', '.join(sorted(set(cooling)))}")
            if dead:
                lines.append(f"   ❌ недоступны у провайдера: {', '.join(sorted(set(dead)))}")
            if not cooling and not dead:
                lines.append("   ✅ все модели в строю")
        else:
            lines.append("🤖 ИИ: выключен (нет ключа)")
        # голос
        vcs = list(self.bot.voice_clients)
        if vcs:
            lines.append("🔊 В голосовых: " + ", ".join(f"«{v.channel.name}»" for v in vcs if v.channel))
        else:
            lines.append("🔇 Не в голосовых каналах")
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    # ---------- перезапуск ----------
    @app_commands.command(name="перезапуск", description="Перезапустить бота (руководство)")
    @app_commands.default_permissions(administrator=True)
    async def restart_cmd(self, inter: discord.Interaction):
        if not self._allowed(inter):
            return await self._deny(inter)
        await inter.response.send_message("♻️ Перезапускаюсь… вернусь через ~15 секунд.", ephemeral=True)
        await asyncio.sleep(1.5)  # дать сообщению дойти
        try:
            await self.bot.close()
        except Exception:
            pass
        _restart_in_place()

    # ---------- обновление из GitHub ----------
    @app_commands.command(name="обновить", description="Подтянуть свежий код из GitHub и перезапуститься (руководство)")
    @app_commands.default_permissions(administrator=True)
    async def update_cmd(self, inter: discord.Interaction):
        await self.do_update(inter)

    async def do_update(self, inter: discord.Interaction):
        """/обновить и кнопка «Обновить бота» из уведомления (в т.ч. в ЛС владельца домашнего сервера)."""
        home = self.home()
        in_owner_dm = inter.guild is None and home is not None and inter.user.id == home.owner_id
        if not in_owner_dm and not self._allowed(inter):
            return await self._deny(inter)
        has_launcher = os.path.isfile(os.path.join(ROOT, "launcher.py"))
        has_git = os.path.isdir(os.path.join(ROOT, ".git")) and shutil.which("git")
        if not has_launcher and not has_git:
            return await inter.response.send_message(
                "❌ Нет ни `launcher.py`, ни git-репозитория — обновлять неоткуда.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        msg = "⬇️ Подтягиваю код из GitHub и перезапускаюсь… вернусь через ~20 секунд."
        if has_git:
            code, out = await _run(["git", "fetch", "-q", "origin", "main"])
            if code != 0:
                return await inter.followup.send(
                    f"❌ GitHub недоступен:\n```\n{out.strip()[-1500:] or '(пусто)'}\n```", ephemeral=True)
            _, changes = await _run(["git", "log", "--oneline", "--no-decorate", "HEAD..origin/main"])
            changes = changes.strip()
            if not changes:
                return await inter.followup.send("✅ Уже последняя версия.", ephemeral=True)
            msg = f"⬇️ Новое в коде:\n```\n{changes[-1500:]}\n```\nОбновляюсь и перезапускаюсь…"
            if not has_launcher:  # старая схема без загрузчика — обновляем сами
                code, out = await _run(["git", "reset", "-q", "--hard", "origin/main"])
                if code != 0:
                    return await inter.followup.send(f"❌ Не смог обновиться:\n```\n{out[-1500:]}\n```",
                                                     ephemeral=True)
        await inter.followup.send(msg, ephemeral=True)
        await asyncio.sleep(1.5)
        try:
            await self.bot.close()
        except Exception:
            pass
        _restart_in_place(via_launcher=True)

    # ---------- горячая перезагрузка модуля ----------
    @app_commands.command(name="модуль", description="Перезагрузить один модуль без полного рестарта (руководство)")
    @app_commands.describe(имя="например: assistant, persona, music, moderation")
    @app_commands.default_permissions(administrator=True)
    async def reload_cmd(self, inter: discord.Interaction, имя: str):
        if not self._allowed(inter):
            return await self._deny(inter)
        name = имя.strip().lower().replace("cogs.", "").replace(" ", "")
        ext = f"cogs.{name}"
        if ext not in config.EXTENSIONS:
            avail = ", ".join(e.split(".", 1)[1] for e in config.EXTENSIONS)
            return await inter.response.send_message(f"❌ Нет такого модуля. Доступны: {avail}", ephemeral=True)
        try:
            await self.bot.reload_extension(ext)
        except Exception as e:
            return await inter.response.send_message(f"❌ Не перезагрузился `{name}`: {str(e)[:300]}", ephemeral=True)
        await inter.response.send_message(f"🔁 Модуль `{name}` перезагружен.", ephemeral=True)


async def setup(bot):
    bot.add_dynamic_items(UpdateButton)
    await bot.add_cog(Maintenance(bot))
