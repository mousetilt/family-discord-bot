# -*- coding: utf-8 -*-
"""Журнал действий участников. Модуль «journal» в /настройки (по умолчанию включён целиком):
• voice — вход/выход/переходы по войсам (сколько пробыл; кто перенёс/отключил — из журнала аудита);
• mic — микрофон, звук, камера, стрим; серверный мут/заглушка (и кто поставил);
• members — ники, роли, аватары, имя аккаунта, тайм-ауты, вход/выход с сервера;
• messages — изменённые и удалённые сообщения (кто удалил чужое), массовая очистка;
• server — действия руководства из журнала аудита Discord: каналы, роли, права, приглашения, баны, кики, вебхуки…
Каналы «🎙・журнал-войса» (voice) и «📜・журнал-действий» (остальное) в «🛠 СЛУЖЕБНОЕ» досоздаются сами
(server_setup.AUTO_CHANNELS); нет канала — пишет в «🤖・логи». Строки копятся и уходят пачкой раз в 3 с (лимиты
Discord). Всё ещё и в таблицу member_log (по guild_id) — `/журнал человек:@…` показывает историю конкретного человека;
хранится set:journal_days дней (/настройки → «Числа»). Для «кто это сделал» боту нужно право «Просмотр журнала аудита»."""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, staff_only

log = logging.getLogger("family-bot.journal")
VIEWERS = config.LEAD + ["hr_head"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS member_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER, actor_id INTEGER,
    kind TEXT NOT NULL, text TEXT, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_member_log_g ON member_log(guild_id, user_id, ts);
"""
KINDS = {"voice": "🎙 Войс", "mic": "🎤 Микро и камера", "members": "👤 Профиль и роли", "messages": "💬 Сообщения",
         "server": "🛠 Сервер"}
CHANNEL = {"voice": "voice_log", "mic": "voice_log"}   # остальное — в audit
LINE_MAX, DESC_MAX = 600, 3900
AUDIT_WINDOW = 20   # секунд: запись журнала аудита свежее этого — относится к событию
SUS_WINDOW, SUS_N = 600, 5   # «подозрительно»: 5 одинаковых действий над чужими за 10 минут от не-руководства
SUS_WHAT = {"roles": "снимает роли у людей", "delete": "удаляет чужие сообщения", "timeout": "раздаёт тайм-ауты"}
A = discord.AuditLogAction
# действия руководства, которые пишем прямо из журнала аудита (участников, мут, перенос и удаление сообщений —
# пишут свои события выше, с привязкой к человеку)
SERVER_ACTIONS = {
    A.channel_create: "создал канал", A.channel_update: "изменил канал", A.channel_delete: "удалил канал",
    A.overwrite_create: "добавил права в канале", A.overwrite_update: "изменил права в канале",
    A.overwrite_delete: "убрал права в канале",
    A.role_create: "создал роль", A.role_update: "изменил роль", A.role_delete: "удалил роль",
    A.invite_create: "создал приглашение", A.invite_delete: "удалил приглашение",
    A.kick: "кикнул", A.ban: "забанил", A.unban: "разбанил", A.member_prune: "почистил неактивных с сервера",
    A.webhook_create: "создал вебхук", A.webhook_update: "изменил вебхук", A.webhook_delete: "удалил вебхук",
    A.emoji_create: "добавил эмодзи", A.emoji_update: "изменил эмодзи", A.emoji_delete: "удалил эмодзи",
    A.bot_add: "добавил бота", A.guild_update: "изменил настройки сервера",
    A.message_pin: "закрепил сообщение", A.message_unpin: "открепил сообщение",
    A.thread_create: "создал ветку", A.thread_delete: "удалил ветку",
    A.integration_create: "подключил интеграцию", A.integration_delete: "отключил интеграцию",
}
ATTRS = {"name": "название", "permissions": "права", "color": "цвет", "colour": "цвет", "topic": "тема",
         "nsfw": "18+", "slowmode_delay": "медленный режим", "user_limit": "лимит", "bitrate": "битрейт",
         "hoist": "показывать отдельно", "mentionable": "можно упоминать", "position": "позиция", "allow": "разрешено",
         "deny": "запрещено", "icon": "иконка", "max_uses": "использований", "max_age": "срок", "channel": "канал",
         "category": "категория", "overwrites": "права", "type": "тип", "rtc_region": "регион"}


def who(m):
    """Человек в строке журнала: упоминание (кликабельно, в эмбеде не пингует) + имя (если ушёл с сервера)."""
    if m is None:
        return "кто-то"
    return f"{m.mention} `{getattr(m, 'display_name', None) or getattr(m, 'name', m.id)}`"


def cut(s, n=300):
    s = str(s or "").replace("`", "'").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def mins(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    h, m = divmod(sec // 60, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


class Journal(commands.Cog):
    group = app_commands.Group(name="журнал", description="Журнал действий участников",
                               default_permissions=discord.Permissions(manage_messages=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()
        self.buf = {}       # (guild_id, ключ канала) → [строки]
        self.sus, self.sus_alerted = {}, {}  # (guild_id, actor_id, что) → [monotonic]; когда последний раз тревожили
        self.joined = {}    # (guild_id, user_id) → когда зашёл в текущий войс (monotonic)
        self.flush.start()
        self.cleanup.start()

    def cog_unload(self):
        self.flush.cancel()
        self.cleanup.cancel()

    # ---------- запись ----------
    def on(self, guild, kind):
        return guild is not None and self.bot.is_family(guild) and self.bot.feature(guild, f"journal:{kind}")

    def put(self, guild, kind, text, user=None, actor=None, icon="•"):
        """Строку — в буфер канала и в базу. text — без времени и иконки."""
        ts = int(time.time())
        self.buf.setdefault((guild.id, CHANNEL.get(kind, "audit")), []).append(cut(f"<t:{ts}:T> {icon} {text}", LINE_MAX))
        try:
            self.bot.db.q("INSERT INTO member_log(guild_id, user_id, actor_id, kind, text, ts) VALUES(?,?,?,?,?,?)",
                          guild.id, getattr(user, "id", user), getattr(actor, "id", actor), kind,
                          cut(f"{icon} {text}", LINE_MAX), ts)
        except Exception:
            log.exception("Журнал: не записал в базу")

    def target(self, guild, key):
        return self.bot.ch(guild, key) or self.bot.ch(guild, "audit") or self.bot.ch(guild, "logs")

    @tasks.loop(seconds=3)
    @guarded
    async def flush(self):
        buf, self.buf = self.buf, {}
        for (gid, key), lines in buf.items():
            guild = self.bot.get_guild(gid)
            ch = self.target(guild, key) if guild else None
            if not ch:
                continue
            chunk = ""
            for ln in lines + [None]:
                if ln is None or len(chunk) + len(ln) + 1 > DESC_MAX:
                    if chunk:
                        try:
                            await ch.send(embed=discord.Embed(description=chunk, color=0x546E7A),
                                          allowed_mentions=discord.AllowedMentions.none())
                        except discord.HTTPException:
                            pass
                    chunk = ""
                if ln is not None:
                    chunk += ("\n" if chunk else "") + ln

    @tasks.loop(hours=6)
    @guarded
    async def cleanup(self):
        for g in list(self.bot.home_guilds):
            days = self.bot.num(g, "journal_days")
            self.bot.db.q("DELETE FROM member_log WHERE guild_id=? AND ts<?", g.id, int(time.time()) - days * 86400)

    @flush.before_loop
    @cleanup.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    async def suspect(self, guild, actor, what):
        """Массовые действия над чужими от того, кто не в руководстве, — одна тревога совету в час."""
        if actor is None or getattr(actor, "bot", False):
            return
        m = guild.get_member(actor.id)
        if m is None or m.id == guild.owner_id or self.bot.has(m, config.LEAD):
            return
        key, t = (guild.id, actor.id, what), time.monotonic()
        dq = self.sus.setdefault(key, [])
        dq[:] = [x for x in dq if t - x < SUS_WINDOW] + [t]
        if len(dq) < SUS_N or t - self.sus_alerted.get(key, -1e9) < 3600:
            return
        self.sus_alerted[key] = t
        text = f"{who(m)} за 10 минут {SUS_WHAT[what]}: **{len(dq)}** раз. Проверь журнал: `/журнал человек`."
        self.put(guild, "server", "🕵️ Подозрительно: " + text, None, m, icon="🚨")
        c = self.bot.ch(guild, "council_chat")
        if c:
            try:
                await c.send(embed=discord.Embed(title="🕵️ Подозрительная активность", description=text, color=0xE53935),
                             allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    def log_channels(self, guild):
        return {c.id for c in (self.bot.ch(guild, k) for k in ("voice_log", "audit", "logs")) if c}

    # ---------- кто это сделал (журнал аудита) ----------
    async def actor(self, guild, action, target_id=None, check=None):
        """Автор действия над участником из журнала аудита — свежая запись (до AUDIT_WINDOW с). Нет прав — None."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1.5)  # запись в журнал аудита появляется чуть позже события
        try:
            async for e in guild.audit_logs(limit=6, action=action):
                if (now() - e.created_at).total_seconds() > AUDIT_WINDOW:
                    break
                if target_id is not None and getattr(e.target, "id", None) != target_id:
                    continue
                if check and not check(e):
                    continue
                return e.user or guild.get_member(e.user_id)
        except (discord.HTTPException, AttributeError):
            return None
        return None

    def by(self, actor, member):
        """« — кем», если это сделал не сам человек."""
        if actor is None or actor.id == member.id:
            return ""
        return f" — {who(actor)}" + (" (бот)" if getattr(actor, "bot", False) else "")

    # ---------- войс ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        if member.bot or not self.bot.is_family(g):
            return
        key, bc, ac = (g.id, member.id), before.channel, after.channel
        if bc != ac and self.on(g, "voice"):
            if bc is None:
                self.joined[key] = time.monotonic()
                state = [s for s, f in (("микро выкл", after.self_mute and not after.self_deaf),
                                        ("звук выкл", after.self_deaf), ("камера", after.self_video),
                                        ("стрим", after.self_stream)) if f]
                self.put(g, "voice", f"{who(member)} зашёл в {ac.mention}" + (f" ({', '.join(state)})" if state else ""),
                         member, icon="📥")
            else:
                start = self.joined.pop(key, None)
                stay = f", пробыл {mins(time.monotonic() - start)}" if start else ""
                if ac is None:
                    actor = await self.actor(g, A.member_disconnect)
                    text = f"{who(member)} вышел из {bc.mention}{stay}"
                    if actor and actor.id != member.id:
                        text = f"{who(member)} отключён от {bc.mention}{stay} — {who(actor)}"
                    self.put(g, "voice", text, member, actor, icon="📤")
                else:
                    self.joined[key] = time.monotonic()
                    actor = await self.actor(g, A.member_move,
                                             check=lambda e: getattr(getattr(e.extra, "channel", None), "id", None) == ac.id)
                    self.put(g, "voice", f"{who(member)} {bc.mention} → {ac.mention}{stay}"
                             + (f" — перенёс {who(actor)}" if actor and actor.id != member.id else ""),
                             member, actor, icon="🔀")
        if bc is None or ac is None or not self.on(g, "mic"):
            return  # флаги сравниваем только внутри войса: при входе/выходе это не «включил/выключил»
        flags = []
        if before.self_deaf != after.self_deaf:
            flags.append(("🔇" if after.self_deaf else "🔈", "выключил звук" if after.self_deaf else "включил звук"))
        elif before.self_mute != after.self_mute:  # заглушка тянет за собой микрофон — второй строкой не пишем
            flags.append(("🎙" if not after.self_mute else "🤐", "выключил микрофон" if after.self_mute else "включил микрофон"))
        if before.self_video != after.self_video:
            flags.append(("📷", "включил камеру" if after.self_video else "выключил камеру"))
        if before.self_stream != after.self_stream:
            flags.append(("🖥", "начал стрим" if after.self_stream else "закончил стрим"))
        for icon, text in flags:
            self.put(g, "mic", f"{who(member)} {text} в {ac.mention}", member, icon=icon)
        for attr, on_t, off_t in (("mute", "серверный мут микрофона", "снят серверный мут"),
                                  ("deaf", "серверная заглушка звука", "снята серверная заглушка")):
            if getattr(before, attr) != getattr(after, attr):
                actor = await self.actor(g, A.member_update, member.id)
                self.put(g, "mic", f"{who(member)}: {on_t if getattr(after, attr) else off_t}{self.by(actor, member)}",
                         member, actor, icon="🛑")

    # ---------- участники ----------
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        g = after.guild
        if not self.on(g, "members"):
            return
        if before.nick != after.nick:
            actor = await self.actor(g, A.member_update, after.id)
            self.put(g, "members", f"{who(after)}: ник «{cut(before.nick or before.name, 60)}» → "
                                   f"«{cut(after.nick or after.name, 60)}»{self.by(actor, after)}", after, actor, icon="✏️")
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            actor = await self.actor(g, A.member_role_update, after.id)
            parts = ([f"+ {', '.join(r.mention for r in added)}"] if added else []) + \
                    ([f"− {', '.join(r.mention for r in removed)}"] if removed else [])
            self.put(g, "members", f"{who(after)}: роли {' · '.join(parts)}{self.by(actor, after)}", after, actor, icon="🎭")
            if removed and actor is not None and actor.id != after.id:
                await self.suspect(g, actor, "roles")
        if before.timed_out_until != after.timed_out_until:
            actor = await self.actor(g, A.member_update, after.id)
            until = after.timed_out_until
            text = (f"тайм-аут до {discord.utils.format_dt(until, 'f')}" if until and until > now() else "тайм-аут снят")
            self.put(g, "members", f"{who(after)}: {text}{self.by(actor, after)}", after, actor, icon="⏳")
            if until and actor is not None and actor.id != after.id:
                await self.suspect(g, actor, "timeout")
        if before.guild_avatar != after.guild_avatar:
            self.put(g, "members", f"{who(after)} сменил аватар на сервере", after, icon="🖼")

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(f"имя аккаунта «{cut(before.name, 40)}» → «{cut(after.name, 40)}»")
        if before.global_name != after.global_name:
            changes.append(f"отображаемое имя «{cut(before.global_name or '—', 40)}» → «{cut(after.global_name or '—', 40)}»")
        if before.avatar != after.avatar:
            changes.append("сменил аватар")
        if not changes or after.bot:
            return
        for g in list(self.bot.home_guilds):
            m = g.get_member(after.id)
            if m and self.on(g, "members"):
                self.put(g, "members", f"{who(m)}: " + "; ".join(changes), m, icon="🪪")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        if self.on(g, "members"):
            age = (now() - member.created_at).days
            self.put(g, "members", f"{who(member)} зашёл на сервер (аккаунту {age} дн."
                     + (" ⚠️ новый" if age < 7 else "") + ")", member, icon="🟢")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        g = member.guild
        self.joined.pop((g.id, member.id), None)
        if not self.on(g, "members"):
            return
        ranks = [config.ROLE_NAMES[k] for k in config.ROSTER_ORDER
                 if (r := self.bot.role(g, k)) and r in member.roles]
        # кик и бан пишет раздел «server» из журнала аудита — здесь только сам факт ухода
        self.put(g, "members", f"{who(member)} покинул сервер" + (f" (был: {', '.join(ranks[:3])})" if ranks else ""),
                 member, icon="🔴")

    # ---------- сообщения ----------
    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        g = after.guild
        if after.author.bot or not self.on(g, "messages") or before.content == after.content \
                or after.channel.id in self.log_channels(g):
            return
        self.put(g, "messages", f"{who(after.author)} изменил сообщение в {after.channel.mention} "
                                f"([к сообщению]({after.jump_url})):\n> было: {cut(before.content, 300) or '—'}\n"
                                f"> стало: {cut(after.content, 300) or '—'}", after.author, icon="📝")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        g = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        msg = payload.cached_message
        # не из кэша (старое, до перезапуска) — автора и текста не знаем; сообщения ботов не пишем
        if not msg or msg.author.bot or not self.on(g, "messages") or payload.channel_id in self.log_channels(g):
            return
        actor = await self.actor(g, A.message_delete, msg.author.id,
                                 check=lambda e: getattr(getattr(e.extra, "channel", None), "id", None) == payload.channel_id)
        files = f" [+ файлов: {len(msg.attachments)}]" if msg.attachments else ""
        self.put(g, "messages", f"удалено сообщение {who(msg.author)} в {msg.channel.mention}"
                                f"{self.by(actor, msg.author) or ' (сам)'}:\n> {cut(msg.content, 400) or '—'}{files}",
                 msg.author, actor, icon="🗑")
        if actor is not None and actor.id != msg.author.id:
            await self.suspect(g, actor, "delete")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        g = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self.on(g, "messages") or payload.channel_id in self.log_channels(g):
            return
        ch = g.get_channel(payload.channel_id)
        self.put(g, "messages", f"массово удалено сообщений: {len(payload.message_ids)} в "
                                f"{ch.mention if ch else payload.channel_id}", icon="🧹")

    # ---------- сервер (журнал аудита) ----------
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        g = entry.guild
        what = SERVER_ACTIONS.get(entry.action)
        if not what or not self.on(g, "server"):
            return
        actor = entry.user or g.get_member(entry.user_id)
        if actor is not None and self.bot.user and actor.id == self.bot.user.id and entry.action in (
                A.channel_update, A.overwrite_create, A.overwrite_update, A.overwrite_delete, A.role_update):
            return  # бот сам правит каналы/права при сборке и на старте — это шум
        t = entry.target
        before, after = self.diff(entry)
        name = getattr(t, "mention", None) if isinstance(t, (discord.abc.GuildChannel, discord.Role, discord.Member,
                                                             discord.User, discord.Thread)) else None
        name = name or cut(getattr(t, "name", None) or before.get("name") or after.get("name")
                           or (f"id {t.id}" if getattr(t, "id", None) else ""), 80)
        tail = []
        if "name" in before and "name" in after and before["name"] != after["name"]:
            tail.append(f"название «{cut(before['name'], 60)}» → «{cut(after['name'], 60)}»")
        other = [ATTRS.get(k, k) for k in after if k != "name"]
        if other and entry.action not in (A.channel_create, A.role_create, A.invite_create):
            tail.append("изменено: " + ", ".join(dict.fromkeys(other))[:200])
        extra = getattr(entry.extra, "channel", None)
        if extra is not None and entry.action in (A.overwrite_create, A.overwrite_update, A.overwrite_delete):
            name = f"{getattr(extra, 'mention', '')} для {name}"
        if entry.reason:
            tail.append(f"причина: {cut(entry.reason, 150)}")
        user_id = getattr(t, "id", None) if entry.action in (A.kick, A.ban, A.unban) else None
        self.put(g, "server", f"{who(actor)} {what} {name}" + (f" · {'; '.join(tail)}" if tail else ""),
                 user_id, actor, icon="🛠")

    @staticmethod
    def diff(entry):
        def grab(d):
            try:
                return {k: v for k, v in iter(d)}
            except Exception:  # noqa: BLE001 — у некоторых записей diff пустой или нестандартный
                return {}
        return grab(entry.changes.before), grab(entry.changes.after)

    # ---------- /журнал ----------
    @group.command(name="человек", description="Что делал участник: войс, микро, ники, роли, сообщения")
    @app_commands.describe(человек="Чей журнал", раздел="Только этот раздел", часов="За сколько последних часов (по умолчанию — всё)")
    @app_commands.choices(раздел=[app_commands.Choice(name=v, value=k) for k, v in KINDS.items()])
    @staff_only(VIEWERS)
    async def person(self, inter: discord.Interaction, человек: discord.Member, раздел: str = None,
                     часов: app_commands.Range[int, 1, 8760] = None):
        q = "SELECT * FROM member_log WHERE guild_id=? AND (user_id=? OR actor_id=?)"
        args = [inter.guild.id, человек.id, человек.id]
        if раздел:
            q += " AND kind=?"
            args.append(раздел)
        if часов:
            q += " AND ts>=?"
            args.append(int(time.time()) - часов * 3600)
        rows = self.bot.db.all(q + " ORDER BY id DESC LIMIT 40", *args)
        if not rows:
            return await inter.response.send_message(f"📜 По {человек.display_name} записей нет"
                                                     f" (хранится {self.bot.num(inter.guild, 'journal_days')} дн.).",
                                                     ephemeral=True)
        lines, size = [], 0
        for r in rows:
            ln = cut(f"<t:{r['ts']}:f> {r['text']}", 400)
            if size + len(ln) > DESC_MAX:
                break
            lines.append(ln)
            size += len(ln) + 1
        e = self.bot.embed(f"📜 Журнал: {человек.display_name}", "\n".join(lines), 0x546E7A, inter.guild)
        e.set_footer(text=f"Показано {len(lines)} последних · {KINDS.get(раздел, 'все разделы')}")
        await inter.response.send_message(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="последнее", description="Последние действия на сервере")
    @app_commands.describe(раздел="Только этот раздел", сколько="Сколько записей (до 40)")
    @app_commands.choices(раздел=[app_commands.Choice(name=v, value=k) for k, v in KINDS.items()])
    @staff_only(VIEWERS)
    async def recent(self, inter: discord.Interaction, раздел: str = None, сколько: app_commands.Range[int, 1, 40] = 20):
        q, args = "SELECT * FROM member_log WHERE guild_id=?", [inter.guild.id]
        if раздел:
            q += " AND kind=?"
            args.append(раздел)
        rows = self.bot.db.all(q + " ORDER BY id DESC LIMIT ?", *args, сколько)
        text = "\n".join(cut(f"<t:{r['ts']}:t> {r['text']}", 300) for r in rows)[:DESC_MAX] or "Пока пусто."
        await inter.response.send_message(embed=self.bot.embed("📜 Последние действия", text, 0x546E7A, inter.guild),
                                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Journal(bot))
