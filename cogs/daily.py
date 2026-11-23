# -*- coding: utf-8 -*-
"""Ежедневные дела бота.
• Резервная копия базы — раз в сутки владельцу сервера в ЛС (в базе есть авторы анонимных обращений, поэтому
  в каналы сервера её не кладём). Плюс полная копия на самом хосте (папка backups/, последние 7 дней).
• Восстановление — /копия восстановить_из:<дата> или файл:<zip из ЛС> (владелец домашнего сервера, с
  подтверждением): перед заменой — снимок текущей базы, ключи ИИ/форума/GitHub сохраняются, затем перезапуск.
• Сводка Главе в ЛС раз в N суток (/настройки → «Числа»: digest_days): заявки, обращения, испытательные сроки, неактив, выговоры.
• Выговоры снимаются сами через N дней без новых выговоров (warn_days)."""
import asyncio
import datetime as dt
import io
import logging
import os
import sqlite3
import tempfile
import zipfile

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, parse_dt, safe_dm

log = logging.getLogger("family-bot.daily")
BACKUP_HOUR = 2    # UTC: 05:00 по Москве
DIGEST_HOUR = 7    # UTC: 10:00 по Москве
INACTIVE_DAYS = 5


BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
KEEP_HOST = 7


def host_backup(path, folder=BACKUP_DIR, name=None, keep=KEEP_HOST):
    """Полная копия базы на хосте (вместе с ключами — она лежит там же, где сама база). Старше keep дней — удаляем."""
    os.makedirs(folder, exist_ok=True)
    name = name or f"family-{now().date().isoformat()}.db"
    dst_path = os.path.join(folder, name)
    tmp = dst_path + ".tmp"
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, dst_path)
    for prefix, n in (("family-", keep), ("prerestore-", 1)):
        old = sorted(f for f in os.listdir(folder) if f.startswith(prefix) and f.endswith(".db"))
        for f in old[:-n]:
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
    return dst_path


def host_backups(folder=BACKUP_DIR):
    """Копии на хосте, новые первыми: [(имя файла, дата)]."""
    try:
        files = sorted((f for f in os.listdir(folder) if f.startswith("family-") and f.endswith(".db")), reverse=True)
    except OSError:
        return []
    return [(f, f[len("family-"):-3]) for f in files]


def check_copy(path):
    """Файл — целая база этого бота? Иначе ValueError с понятной причиной."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        raise ValueError("файл не открывается как база") from None
    try:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("файл повреждён")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"guild_settings", "settings", "applications"} <= tables:
            raise ValueError("это не база этого бота")
    except sqlite3.DatabaseError:
        raise ValueError("файл не открывается как база") from None
    finally:
        c.close()


def restore_into(conn, path):
    """Заменить содержимое живой базы копией. Секреты (ключи ИИ, форум, токен GitHub) берём из ТЕКУЩЕЙ базы:
    в копиях из ЛС их нет, а вводить заново никто не хочет."""
    check_copy(path)
    qg = ",".join("?" * len(SECRET_GUILD_KEYS))
    keep_g = conn.execute(f"SELECT guild_id, key, value FROM guild_settings WHERE key IN ({qg})",
                          SECRET_GUILD_KEYS).fetchall()
    keep = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(SECRET_KEYS))})",
                        SECRET_KEYS).fetchall()
    conn.commit()
    src = sqlite3.connect(path)
    try:
        src.backup(conn)
    finally:
        src.close()
    for row in keep_g:
        if not conn.execute("SELECT 1 FROM guild_settings WHERE guild_id=? AND key=?", tuple(row[:2])).fetchone():
            conn.execute("INSERT INTO guild_settings(guild_id, key, value) VALUES(?,?,?)", tuple(row))
    for row in keep:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", tuple(row))
    conn.commit()


def unpack_upload(data: bytes, folder):
    """Вложение из ЛС (zip с family.db или сам .db) → путь к файлу базы во folder."""
    path = os.path.join(folder, "upload.db")
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.endswith(".db")]
            if not names:
                raise ValueError("в архиве нет файла базы")
            if z.getinfo(names[0]).file_size > 200 * 1024 * 1024:
                raise ValueError("база в архиве слишком большая")
            with open(path, "wb") as f:
                f.write(z.read(names[0]))
    else:
        with open(path, "wb") as f:
            f.write(data)
    return path


class RestoreConfirm(discord.ui.View):
    def __init__(self, cog, user_id, path, label):
        super().__init__(timeout=120)
        self.cog, self.user_id, self.path, self.label = cog, user_id, path, label

    async def interaction_check(self, inter):
        return inter.user.id == self.user_id

    @discord.ui.button(label="Да, восстановить", style=discord.ButtonStyle.danger, emoji="♻️")
    async def yes(self, inter: discord.Interaction, _):
        self.stop()
        await inter.response.edit_message(content=f"♻️ Восстанавливаю базу из «{self.label}»…", view=None)
        await self.cog.do_restore(inter, self.path, self.label)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, _):
        self.stop()
        await inter.response.edit_message(content="Отменил — база не тронута.", view=None)


SECRET_GUILD_KEYS = ("forum:pass", "forum:cookies", "ai:groq", "ai:gemini", "ai:claude", "dev:gh_token")
SECRET_KEYS = ("dev:gh_token", "forum:pass", "forum:cookies")


def make_backup(path, only_gid=None, home_gid=None):
    """Целостная копия базы (даже если бот в этот момент пишет) → zip в памяти.
    only_gid — оставить только данные этой семьи (владельцу чужого сервера — не вся база бота)."""
    tmp = os.path.join(tempfile.gettempdir(), f"family_backup_{os.getpid()}_{only_gid or 0}.db")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            # анонимные обращения: в копии автора нет (её получает владелец сервера — обычно Глава,
            # а панель обещает, что руководство не узнает, кто написал). В живой базе он остаётся — для ответа в ЛС.
            try:
                dst.execute("UPDATE appeals SET user_id=NULL WHERE anon=1")
                dst.commit()
            except sqlite3.Error:
                pass
            for t in ("msg_cache", "kb_vec"):  # суточный кэш сообщений и векторы базы знаний — не данные семьи
                try:
                    dst.execute(f"DELETE FROM {t}")
                    dst.commit()
                except sqlite3.Error:
                    pass
            # секреты не уходят ни в одну копию (копия — вложение в ЛС Discord): пароль и сессия форума,
            # ключи ИИ всех семей, токен GitHub разработчика. После восстановления их вводят заново
            for table, keys in (("guild_settings", SECRET_GUILD_KEYS), ("settings", SECRET_KEYS)):
                try:
                    dst.execute(f"DELETE FROM {table} WHERE key IN ({','.join('?' * len(keys))})", keys)
                    dst.commit()
                except sqlite3.Error:
                    pass
            if only_gid:
                strip_foreign(dst, only_gid)
        finally:
            dst.close()
    finally:
        src.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "family.db")
    os.remove(tmp)
    buf.seek(0)
    return buf


def strip_foreign(conn, gid):
    """Удаляет из копии всё, что не относится к семье gid: чужие семьи, служебное, альянс."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        if t.startswith("alliance_") or t in ("settings", "ai_usage", "bot_errors"):
            conn.execute(f"DELETE FROM {t}")
        elif "guild_id" in cols:
            conn.execute(f"DELETE FROM {t} WHERE guild_id<>?", (gid,))
    conn.commit()
    conn.execute("VACUUM")


class Daily(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hourly.start()

    def cog_unload(self):
        self.hourly.cancel()

    @tasks.loop(hours=1)
    @guarded
    async def hourly(self):
        for g in self.bot.home_guilds:
            for job in (self.expire_warns, self.backup, self.digest):
                try:
                    await job(g)
                except Exception:
                    log.exception("Ежедневная задача %s", job.__name__)

    @hourly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- резервная копия ----------
    async def owner(self, guild):
        if guild.owner:
            return guild.owner
        try:
            return await self.bot.fetch_user(guild.owner_id)
        except discord.HTTPException:
            return None

    async def send_backup(self, guild):
        owner = await self.owner(guild)
        if not owner:
            return False
        # домашнему серверу — вся база (для восстановления бота), остальным семьям — только их данные
        only = None if self.bot.is_home(guild) else guild.id
        buf = await asyncio.to_thread(make_backup, config.DB_PATH, only, None if only else guild.id)
        size = buf.getbuffer().nbytes
        if size > 9 * 1024 * 1024:
            return await safe_dm(owner, f"💾 База семьи {config.FAMILY} выросла до {size // 1024 // 1024} МБ — в ЛС уже не "
                                        "помещается. Скачай файл family.db с хостинга вручную.")
        name = f"family-{now().strftime('%Y-%m-%d')}.zip"
        return await safe_dm(owner, f"💾 Резервная копия базы семьи {config.FAMILY} за {now().strftime('%d.%m.%Y')}. "
                                    "Храни её у себя: если хостинг потеряет данные, базу можно вернуть из этого файла. "
                                    "Никому не пересылай — внутри заявки, выговоры и обращения.",
                             file=discord.File(buf, filename=name))

    async def backup(self, guild):
        t = now()
        if self.bot.is_home(guild) and t.hour >= BACKUP_HOUR and self.bot.db.get("backup:host") != str(t.date()):
            self.bot.db.set("backup:host", str(t.date()))
            try:
                await asyncio.to_thread(host_backup, config.DB_PATH)
            except (OSError, sqlite3.Error) as e:
                log.error("Копия базы на хосте не сделана: %s", e)
        key = f"backup:{t.date()}"
        if t.hour < BACKUP_HOUR or self.bot.db.gget(guild, key) or not self.bot.feature(guild, "service:backup"):
            return
        self.bot.db.gset(guild, key, 1)
        if not await self.send_backup(guild):
            await self.bot.log(guild, "💾 Не смог отправить владельцу сервера резервную копию базы — открой ЛС от "
                                      "участников сервера. Вручную: `/копия`.", color=0xFB8C00)

    @app_commands.command(name="копия", description="Копия базы в ЛС; или восстановить базу из копии (владелец)")
    @app_commands.rename(src="восстановить_из", upload="файл")
    @app_commands.describe(src="Восстановить из копии на хосте (последние 7 дней) — только домашний сервер",
                           upload="Восстановить из файла копии, который бот присылал в ЛС (zip)")
    @app_commands.default_permissions(administrator=True)
    async def backup_cmd(self, inter: discord.Interaction, src: str = None, upload: discord.Attachment = None):
        if inter.user.id != inter.guild.owner_id:
            return await inter.response.send_message("⛔ Копию базы получает только владелец сервера.", ephemeral=True)
        if src or upload:
            return await self.ask_restore(inter, src, upload)
        await inter.response.defer(ephemeral=True)
        ok = await self.send_backup(inter.guild)
        await inter.followup.send("💾 Отправил в ЛС." if ok else "❌ Не смог написать в ЛС — открой личные сообщения "
                                  "от участников сервера.", ephemeral=True)

    @backup_cmd.autocomplete("src")
    async def _src_auto(self, inter: discord.Interaction, current: str):
        return [app_commands.Choice(name=f"Копия за {d}", value=f) for f, d in host_backups()
                if current.lower() in d][:25]

    async def ask_restore(self, inter, src, upload):
        if not self.bot.is_home(inter.guild):
            return await inter.response.send_message("⛔ Восстановить базу можно только на домашнем сервере бота — "
                                                     "она общая на все семьи.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            if upload:
                if upload.size > 25 * 1024 * 1024:
                    raise ValueError("файл больше 25 МБ")
                folder = tempfile.mkdtemp(prefix="restore_")
                path = await asyncio.to_thread(unpack_upload, await upload.read(), folder)
                label = upload.filename
            else:
                name = os.path.basename(src)
                path = os.path.join(BACKUP_DIR, name)
                if not (name.startswith("family-") and os.path.isfile(path)):
                    raise ValueError("такой копии на хосте нет")
                label = f"копия за {name[len('family-'):-3]}"
            await asyncio.to_thread(check_copy, path)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            return await inter.followup.send(f"❌ Не могу восстановить: {e}.", ephemeral=True)
        await inter.followup.send(
            f"⚠️ Заменить ВСЮ базу бота копией «{label}»? Всё, что было после неё (заявки, выговоры, казна…), "
            "пропадёт. Текущую базу сохраню рядом на всякий случай, ключи ИИ и форума останутся. Потом бот "
            "перезапустится.", view=RestoreConfirm(self, inter.user.id, path, label), ephemeral=True)

    async def do_restore(self, inter, path, label):
        try:
            await asyncio.to_thread(host_backup, config.DB_PATH, BACKUP_DIR,
                                    f"prerestore-{now().strftime('%Y%m%d-%H%M%S')}.db")
            restore_into(self.bot.db.c, path)
        except (ValueError, OSError, sqlite3.Error) as e:
            log.error("Восстановление базы из «%s» не удалось: %s", label, e)
            return await inter.followup.send(f"❌ Не получилось: {e}. База не изменилась.", ephemeral=True)
        log.warning("База восстановлена из «%s» (%s)", label, inter.user)
        await inter.followup.send(f"✅ База восстановлена из «{label}». Перезапускаюсь…", ephemeral=True)
        await asyncio.sleep(1.5)
        from cogs.maintenance import _restart_in_place
        try:
            await self.bot.close()
        except Exception:  # noqa: BLE001
            pass
        _restart_in_place()

    @app_commands.command(name="ошибки", description="Последние ошибки бота из журнала (руководство)")
    @app_commands.default_permissions(administrator=True)
    async def errors_cmd(self, inter: discord.Interaction):
        if not self.bot.is_home(inter.guild) or (inter.user.id != inter.guild.owner_id
                                                  and not self.bot.has(inter.user, ["leader", "deputy"])):
            return await inter.response.send_message("⛔ Журнал ошибок общий для всех семей — только для руководства "
                                                     "домашнего сервера бота.", ephemeral=True)
        try:
            with open(config.ERR_LOG, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 60_000))
                data = f.read()
        except OSError:
            data = b""
        if not data.strip():
            return await inter.response.send_message("✅ Журнал ошибок пуст.", ephemeral=True)
        tail = data.decode("utf-8", "replace")[-1500:]
        await inter.response.send_message(f"Последние записи журнала (полный — файлом):\n```\n{tail}\n```", ephemeral=True,
                                          file=discord.File(io.BytesIO(data), filename="errors.log"))

    # ---------- авто-снятие выговоров ----------
    async def expire_warns(self, guild):
        days = self.bot.num(guild, "warn_days")
        if days <= 0:
            return
        db = self.bot.db
        border = (now() - dt.timedelta(days=days)).isoformat()
        rows = db.all("SELECT user_id, COUNT(*) n, MAX(created_at) last FROM warns WHERE guild_id=? AND active=1 "
                      "GROUP BY user_id HAVING MAX(created_at)<?", guild.id, border)
        for r in rows:
            db.q("UPDATE warns SET active=0 WHERE guild_id=? AND user_id=? AND active=1", guild.id, r["user_id"])
            await self.bot.log(guild, f"⚪ С <@{r['user_id']}> автоматически сняты выговоры ({r['n']}): "
                                      f"{days} дн. без новых нарушений.", "warns")
            m = guild.get_member(r["user_id"])
            if m:
                await safe_dm(m, f"⚪ Твои выговоры в семье {config.FAMILY} сняты: {days} дн. без новых "
                                 "нарушений. Так держать!")

    # ---------- сводка Главе ----------
    def build_digest(self, guild):
        bot, db, t = self.bot, self.bot.db, now()
        every = self.bot.num(guild, "digest_days")
        since = (t - dt.timedelta(days=max(every, 1))).isoformat()
        e = bot.embed(f"🗞 Сводка по семье {config.FAMILY}", None, config.GOLD, guild)
        fam = bot.family_members(guild)
        gid = guild.id
        joined = db.one("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status='accepted' AND decided_at>?",
                        gid, since)["n"]
        e.description = f"В семье **{len(fam)}** чел." + (f" · новых за период: **{joined}**" if joined else "")

        apps = db.all("SELECT id, user_id, status, created_at FROM applications WHERE guild_id=? AND "
                      "status IN ('new','taken','interview') ORDER BY id", gid)
        if apps:
            names = {"new": "🟡 ждёт", "taken": "🔵 взята", "interview": "🟣 собеседование"}
            e.add_field(name=f"📥 Открытые заявки ({len(apps)})",
                        value="\n".join(f"№{a['id']} <@{a['user_id']}> — {names[a['status']]} · "
                                        f"<t:{int(parse_dt(a['created_at']).timestamp())}:R>" for a in apps[:8])[:1024],
                        inline=False)
        appeals = db.all("SELECT id, topic, created_at FROM appeals WHERE guild_id=? AND status='open' ORDER BY id", gid)
        if appeals:
            e.add_field(name=f"📬 Обращения без ответа ({len(appeals)})",
                        value="\n".join(f"№{a['id']} {a['topic']} · <t:{int(parse_dt(a['created_at']).timestamp())}:R>"
                                        for a in appeals[:8])[:1024], inline=False)
        soon = (t + dt.timedelta(days=2)).isoformat()
        ms = db.all("SELECT newbie_id, mentor_id, due_at FROM mentorships WHERE guild_id=? AND status='active' "
                    "AND due_at<? ORDER BY due_at", gid, soon)
        if ms:
            e.add_field(name=f"🎓 Испытательный срок: пора решать ({len(ms)})",
                        value="\n".join(f"<@{m['newbie_id']}> (наставник <@{m['mentor_id']}>) — "
                                        f"<t:{int(parse_dt(m['due_at']).timestamp())}:R>" for m in ms[:8])[:1024],
                        inline=False)
        vac = bot.role(guild, "vacation")
        border = t - dt.timedelta(days=INACTIVE_DAYS)
        idle = []
        for m in fam:
            if (vac and vac in m.roles) or m.voice:
                continue
            a = db.one("SELECT last_seen FROM activity WHERE guild_id=? AND user_id=?", gid, m.id)
            last = parse_dt(a["last_seen"]) if a and a["last_seen"] else None
            if not last or last < border:
                idle.append(m)
        if idle:
            e.add_field(name=f"💤 Не появлялись {INACTIVE_DAYS}+ дн. ({len(idle)})",
                        value=(", ".join(m.mention for m in idle[:15]) + ("…" if len(idle) > 15 else ""))[:1024]
                        + "\nПодробно: `/неактив`", inline=False)
        from core.people import at_risk
        risky = [(m, r) for m in fam if (r := at_risk(bot, guild, m))]
        if risky:
            e.add_field(name=f"⚠️ На грани неактива ({len(risky)})",
                        value="\n".join(f"{m.mention} — {r}" for m, r in risky[:8])[:1000]
                        + "\nНаписать им: «Швейцар, кто на грани неактива» → кнопка «Написать им в ЛС»", inline=False)
        warns = db.all("SELECT user_id, COUNT(*) n FROM warns WHERE guild_id=? AND active=1 GROUP BY user_id "
                       "ORDER BY n DESC", gid)
        if warns:
            e.add_field(name=f"⚖️ С активными выговорами ({len(warns)})",
                        value=", ".join(f"<@{w['user_id']}> ×{w['n']}" for w in warns[:12])[:1024], inline=False)
        flags = db.one("SELECT COUNT(*) n FROM flags WHERE guild_id=? AND status='open'", gid)["n"]
        market = db.one("SELECT COUNT(*) n FROM market WHERE guild_id=? AND status='active'", gid)["n"]
        tr = bot.get_cog("Treasury")
        money = tr.digest_line(guild) if tr else None
        e.add_field(name="Прочее", value=f"🛡 Жалоб и флагов модерации без решения: **{flags}**\n"
                                         f"🛒 Объявлений на рынке: **{market}**\n"
                                         f"💎 Бустов сервера: **{guild.premium_subscription_count or 0}**"
                                         + (f"\n{money}" if money else ""), inline=False)
        if len(e.fields) == 1 and not flags:
            e.description += "\n\n✅ Всё разобрано: открытых заявок, обращений и просроченных сроков нет."
        rep = bot.ch(guild, "reports")
        if rep and bot.feature(guild, "reports:weekly"):  # анализ недели ИИ — там, здесь только «что требует решения»
            e.add_field(name="📊 Разбор недели", value=f"Еженедельный отчёт с советами — в {rep.mention}", inline=False)
        e.set_footer(text=f"Сводка приходит раз в {every or '—'} сут. · {bot.brand(guild, config.FOOTER)}")
        return e

    async def digest(self, guild):
        every = self.bot.num(guild, "digest_days")
        if every <= 0 or not self.bot.feature(guild, "reports:digest"):
            return
        t = now()
        last = self.bot.db.gget(guild, "digest:last")
        if t.hour < DIGEST_HOUR or (last and (t - parse_dt(last)).total_seconds() < every * 86400 - 3600):
            return
        self.bot.db.gset(guild, "digest:last", now_iso())
        role = self.bot.role(guild, "leader")
        targets = [m for m in (role.members if role else []) if not m.bot]
        if not targets and guild.owner:
            targets = [guild.owner]
        e = self.build_digest(guild)
        for m in targets:
            await safe_dm(m, embed=e)

    @app_commands.command(name="сводка", description="Сводка по семье: заявки, обращения, сроки, неактив, выговоры")
    @app_commands.default_permissions(manage_messages=True)
    async def digest_cmd(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только для руководства.", ephemeral=True)
        await inter.response.send_message(embed=self.build_digest(inter.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Daily(bot))
