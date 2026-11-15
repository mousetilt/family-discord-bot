# -*- coding: utf-8 -*-
"""Служебная панель владельца: статус, перезапуск, обновление кода из GitHub, горячая
перезагрузка модуля. ТОЛЬКО для Главы/заместителя (или владельца сервера).
БЕЗ выполнения произвольного кода/команд — только эти фиксированные действия."""
import asyncio
import datetime as dt
import os
import shutil
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config

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


def _restart_in_place(via_launcher=False):
    """Перезапускает текущий процесс бота на месте (не зависит от авто-рестарта хоста).
    via_launcher=True — через launcher.py, который перед стартом подтянет код из GitHub."""
    script = "launcher.py" if via_launcher and os.path.isfile(os.path.join(ROOT, "launcher.py")) else "bot.py"
    os.execv(sys.executable, [sys.executable, "-u", os.path.join(ROOT, script)])


class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        lines.append(f"🧩 Версия кода: {ver}")
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
        if not self._allowed(inter):
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
    await bot.add_cog(Maintenance(bot))
