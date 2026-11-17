# -*- coding: utf-8 -*-
"""Служебная панель владельца: статус, перезапуск, обновление кода из GitHub, горячая
перезагрузка модуля. ТОЛЬКО для Главы/заместителя (или владельца сервера).
БЕЗ выполнения произвольного кода/команд — только эти фиксированные действия.

Обновления (data/changelog.json — список версий, новые сверху: v, title, added, fixed, members):
• update_loop раз в 10 мин читает changelog.json с GitHub. Есть версия, которой у нас нет, — один раз пишет
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
from core.botcore import guarded

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
    """Записи новее версии since (список новые сверху). since нет в списке — до трёх последних."""
    vs = [e["v"] for e in entries]
    if since in vs:
        return entries[:vs.index(since)]
    return entries[:3]


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
        self._alerted, self._voice_down, self._offline_since, self._probe_ts = set(), None, None, 0.0
        self.update_loop.start()
        self.watchdog.start()

    def cog_unload(self):
        self.update_loop.cancel()
        self.watchdog.cancel()

    def home(self):
        return next((g for g in self.bot.guilds if self.bot.is_home(g)), None)

    # ---------- сторож (watchdog) ----------
    def problems(self):
        """[(ключ, что, с какого времени, почему)] — что сейчас не работает."""
        out, home = [], self.home()
        ai = self.bot.ai_for(home) if home else self.bot.ai
        if ai.on and getattr(ai, "fail_since", None):
            out.append(("ai", "🤖 ИИ", ai.fail_since, getattr(ai, "fail_reason", "")))
        j = self.bot.get_cog("Jarvis")
        if j and getattr(j, "whisper_fail_since", None):
            out.append(("whisper", "🎙 Распознавание речи", j.whisper_fail_since, j.whisper_fail_reason))
        music = self.bot.get_cog("Music")
        dn = getattr(music, "dn", None)
        if dn is not None and not dn.is_ready():
            self._voice_down = self._voice_down or time.time()
            out.append(("voice", "🔊 Голосовой модуль", self._voice_down, "не подключён к Discord"))
        else:
            self._voice_down = None
        return out

    def health_line(self):
        now_ = time.time()
        bad = self.problems()
        if not bad:
            return "🐕 Сторож: всё работает"
        return "🐕 Сторож: " + "; ".join(f"{t} не работает {int((now_ - s) // 60)} мин ({r[:80]})"
                                        for _, t, s, r in bad)

    async def alert(self, text):
        """Главе домашнего сервера (владельцу и роли Главы) в ЛС + в бот-команды."""
        home = self.home()
        if not home or not self.bot.feature(home, "service:watchdog"):
            return
        e = self.bot.embed("🐕 Сторож", text, config.GOLD, home)
        people = {home.owner} | set(getattr(self.bot.role(home, "leader"), "members", []))
        for m in filter(None, people):
            if not m.bot:
                try:
                    await m.send(embed=e)
                except discord.HTTPException:
                    pass
        c = self.bot.ch(home, "botcmd")
        if c:
            try:
                await c.send(embed=e)
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=2)
    @guarded
    async def watchdog(self):
        now_ = time.time()
        home = self.home()
        ai = self.bot.ai_for(home) if home else self.bot.ai
        if ai.on and getattr(ai, "fail_since", None) and now_ - self._probe_ts >= 300:
            # ИИ числится упавшим — проверим сами крошечным запросом (иначе флаг висит, пока никто не спросит)
            self._probe_ts = now_
            try:
                await ai.ask("Ответь одним словом: ок", "проверка связи", max_tokens=5, light=True)
            except Exception:
                pass
        bad = {k: (t, s, r) for k, t, s, r in self.problems()}
        for k, (t, s, r) in bad.items():
            if now_ - s >= 600 and k not in self._alerted:
                self._alerted.add(k)
                await self.alert(f"⚠️ {t} не работает уже **{int((now_ - s) // 60)} мин**.\nПричина: {r or '—'}\n"
                                 "Если не пройдёт само — `/перезапуск`, а для ИИ проверь ключ: `/ии статус`.")
        for k in list(self._alerted):
            if k not in bad:
                self._alerted.discard(k)
                await self.alert(f"✅ {dict(ai='🤖 ИИ', whisper='🎙 Распознавание речи', voice='🔊 Голосовой модуль').get(k, k)}"
                                 " снова работает.")

    @watchdog.before_loop
    async def _wd_before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_disconnect")
    async def _wd_disconnect(self):
        self._offline_since = self._offline_since or time.time()

    @commands.Cog.listener("on_resumed")
    async def _wd_resumed(self):
        await self._wd_back()

    @commands.Cog.listener("on_ready")
    async def _wd_ready(self):
        await self._wd_back()

    async def _wd_back(self):
        since, self._offline_since = self._offline_since, None
        if since and time.time() - since >= 600:
            await self.alert(f"📡 Бот был без связи с Discord **{int((time.time() - since) // 60)} мин** "
                             f"(с <t:{int(since)}:t>). Сейчас всё снова на связи.")

    # ---------- уведомления об обновлениях ----------
    @tasks.loop(minutes=10)
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
        entries = load_changelog()
        vs = [x["v"] for x in entries]
        # эта версия и более ранние, которые семье ещё не публиковали (до трёх) — одним постом
        todo = [x for x in entries[vs.index(ver):vs.index(ver) + 3] if x.get("members")
                and not self.bot.db.gget(g, f"changelog:pub:{x['v']}")] if ver in vs else []
        c = self.bot.ch(g, "news")
        if not c:
            return await inter.response.send_message("Нет канала объявлений.", ephemeral=True)
        if not todo:
            return await inter.response.send_message("✅ Уже опубликовано.", ephemeral=True)
        e = todo[-1]  # заголовок — по самой крупной (ранней) из публикуемых
        text = "\n\n".join(entry_text(x, True) for x in reversed(todo))[:3900]
        emb = self.bot.embed(f"🎩 Швейцар обновился · {e.get('title', ver)}", self.bot.brand(g, text), config.GOLD, g)
        news = self.bot.role(g, "n_news")
        try:
            await c.send(content=news.mention if news else None, embed=emb,
                         allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            return await inter.response.send_message("❌ Не смог написать в объявления — проверь права бота.",
                                                     ephemeral=True)
        for x in todo:
            self.bot.db.gset(g, f"changelog:pub:{x['v']}", inter.user.id)
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
        ai = self.bot.ai_for(inter.guild)  # на домашнем — с учётом ключей из /ии ключ
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
        music = self.bot.get_cog("Music")  # голос работает через отдельный клиент disnake, а не discord.py
        dn = getattr(music, "dn", None)
        vcs = list(dn.voice_clients) if dn is not None and dn.is_ready() else []
        if vcs:
            lines.append("🔊 В голосовых: " + ", ".join(f"«{v.channel.name}»" for v in vcs if v.channel))
        else:
            lines.append("🔇 Не в голосовых каналах")
        lines.append(self.health_line())
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
