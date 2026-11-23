# -*- coding: utf-8 -*-
"""Ядро бота: доступ к ролям/каналам, общие действия (ранг, исключение, выговор), события семьи."""
import asyncio
import datetime as dt
import functools
import logging

import discord
from discord import app_commands
from discord.ext import commands

from . import config
from .ai import AI, KEYED
from .db import DB, gid_of as guild_id_of
from . import family as family_mod
from .family import brand as family_brand, feature as family_feature, profile as family_profile
from .kb import KnowledgeBase

log = logging.getLogger("family-bot")


def now():
    return discord.utils.utcnow()


def now_iso():
    return now().isoformat()


def parse_dt(s):
    return dt.datetime.fromisoformat(s) if s else None


def fmt_hours(sec):
    sec = int(sec or 0)
    h, m = divmod(sec // 60, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]
MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
              "Ноябрь", "Декабрь"]


def ru_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def staff_only(keys):
    async def pred(inter: discord.Interaction):
        if inter.client.has(inter.user, keys):
            return True
        await inter.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        return False
    return app_commands.check(pred)


def family_only():
    async def pred(inter: discord.Interaction):
        if inter.client.has(inter.user, config.FAMILY_PLUS):
            return True
        await inter.response.send_message("Эта команда только для семьи.", ephemeral=True)
        return False
    return app_commands.check(pred)


def guarded(fn):
    """Для фоновых задач (tasks.loop): ошибка в одном проходе не должна останавливать задачу навсегда."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Фоновая задача %s: ошибка, следующий проход — по расписанию", fn.__name__)
    return wrapper


_BRAND = None  # FamilyBot.brand — подставляет профиль семьи в текст ЛС участнику сервера


async def answer_after(inter, coro, ephemeral=True, **kwargs):
    """Ответ, который считается дольше 3 с (ЛС, журнал, панели): сначала defer, потом текст из coro.
    Иначе Discord отвечает 10062 Unknown interaction, а человек видит «Приложение не отвечает»."""
    if not inter.response.is_done():
        try:
            await inter.response.defer(ephemeral=ephemeral, thinking=True)
        except discord.HTTPException:
            pass
    text = await coro
    try:
        await inter.followup.send(text, ephemeral=ephemeral, **kwargs)
    except discord.HTTPException:
        pass


async def safe_dm(user, *args, **kwargs):
    try:
        if _BRAND and isinstance(user, discord.Member):
            if args and isinstance(args[0], str):
                args = (_BRAND(user.guild, args[0]),) + tuple(args[1:])
            if isinstance(kwargs.get("content"), str):
                kwargs["content"] = _BRAND(user.guild, kwargs["content"])
        await user.send(*args, **kwargs)
        return True
    except (discord.HTTPException, AttributeError):
        return False


FOREIGN_COMMANDS = {"альянс", "семья"}  # единственные команды на серверах, где семья не подключена


def event_guild_id(args):
    """ID сервера, к которому относится событие (или None для личных сообщений и служебных событий)."""
    for a in args:
        if isinstance(a, discord.Guild):
            return a.id
        g = getattr(a, "guild", None)
        if isinstance(g, discord.Guild):
            return g.id
        gid = getattr(a, "guild_id", None)
        if isinstance(gid, int):
            return gid
    return None


class FamilyTree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        bot = inter.client
        if inter.guild is None or bot.is_family(inter.guild):
            return True
        cmd = inter.command
        parent = getattr(cmd, "root_parent", None)
        root = parent.name if parent else (cmd.name if cmd else "")
        if root in FOREIGN_COMMANDS:
            return True
        await inter.response.send_message("Семья на этом сервере не подключена. Владелец сервера может подключить её "
                                          "командой `/семья подключить`.", ephemeral=True)
        return False


class FamilyBot(commands.Bot):
    def __init__(self, presences=True):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = presences  # статус «Стримит» для анонсов эфиров
        intents.voice_states = True
        intents.message_content = True
        super().__init__(command_prefix="!!ulyanov!!", intents=intents, help_command=None, tree_cls=FamilyTree)
        self.presences_on = presences
        self.db = DB(config.DB_PATH, config.GUILD_ID)
        self.ai = AI(self.db)
        self.kb = KnowledgeBase(self.db)
        self.voice_sessions = {}
        # подключённые семьи (кроме домашней) — в памяти, чтобы не лезть в базу на каждое событие
        self.families = {int(r["guild_id"]) for r in self.db.all(
            "SELECT guild_id FROM guild_settings WHERE key='family:enabled' AND value='1'")}
        self._synced_ids = set()
        self._ai_cache = {}  # guild_id -> (ключи, AI) для семей со своими ключами
        global _BRAND
        _BRAND = self.brand

    # ---------- свой сервер и серверы союзников ----------
    @property
    def home_id(self):
        if config.GUILD_ID:
            return config.GUILD_ID
        hid = self.db.get("home_guild")
        if hid:
            return int(hid)
        return self.guilds[0].id if len(self.guilds) == 1 else None

    @property
    def home_guilds(self):
        """Все серверы, где работает семья (домашний + подключённые). По ним ходят фоновые задачи."""
        if self.home_id is None:
            return list(self.guilds)
        return [g for g in self.guilds if self.is_family_id(g.id)]

    def is_home(self, guild):
        """Главный (домашний) сервер — основатель альянса."""
        return guild is None or self.home_id is None or guild.id == self.home_id

    def is_family_id(self, gid):
        return self.home_id is None or gid == self.home_id or gid in self.families

    def is_family(self, guild):
        """Сервер, где работает полный функционал семьи."""
        return guild is None or self.is_family_id(guild.id)

    def set_family(self, guild, on):
        self.db.gset(guild, "family:enabled", "1" if on else "0")
        (self.families.add if on else self.families.discard)(guild.id)

    # ---------- ИИ и модули семьи ----------
    def ai_for(self, guild):
        """ИИ этой семьи. Домашний сервер — ключи из /ии ключ, если заданы (главнее), иначе/в запас — ключи хоста;
        остальные — только свои ключи (/ии ключ), иначе ИИ выключен."""
        if guild is None:
            return self.ai
        gid = guild_id_of(guild)
        keys = {p: self.db.gget(gid, f"ai:{p}") for p in KEYED}
        keys = {p: k for p, k in keys.items() if k}
        primary = self.db.gget(gid, "ai:primary") or None  # /ии основной; нет — AI_PROVIDER / бесплатный
        home = self.home_id is None or gid == self.home_id
        if home:
            if not keys and not primary:
                return self.ai
            # свой ключ главнее; ключ хоста (в т.ч. ANTHROPIC_API_KEY) — запасной. Чужим семьям ключи хоста
            # не даём (решение владельца: лимит и платный Claude хоста не делим) — у них только свои.
            keys = {**{p: k for p, k in self.ai.keys.items() if p in KEYED}, **keys}
        sig = (home, primary) + tuple(sorted(keys.items()))
        cached = self._ai_cache.get(gid)
        if cached and cached[0] == sig:
            return cached[1]
        ai = AI(self.db, keys=keys, scope=None if home else gid, primary=primary)  # дневной лимит домашнего — общий, как раньше
        self._ai_cache[gid] = (sig, ai)
        return ai

    def feature(self, guild, key):
        """Включён ли модуль у этой семьи (/настройки)."""
        return guild is None or family_feature(self.db, guild_id_of(guild), key)

    def num(self, guild, key):
        """Число семьи из /настройки → «🔢 Числа» (core/family.NUMBERS)."""
        return family_mod.number(self.db, guild_id_of(guild) if guild is not None else self.home_id, key)

    def rules_text(self, guild):
        """Правила семьи: свои (/настройки → «📜 Тексты») или стандартные — под семью."""
        own = family_mod.text(self.db, guild_id_of(guild), "rules") if guild is not None else None
        if own:
            return own
        return self.brand(guild, config.RULES_TEXT.replace("3 активных выговора",
                                                          f"{self.num(guild, 'warn_limit')} активных выговора"))

    def welcome_text(self, guild):
        own = family_mod.text(self.db, guild_id_of(guild), "welcome") if guild is not None else None
        return own or self.brand(guild, config.WELCOME_TEXT)

    def checklist(self, guild):
        return family_mod.checklist(self.db, guild_id_of(guild))

    def situational(self, guild):
        return family_mod.situational(self.db, guild_id_of(guild))

    # ---------- профиль семьи ----------
    def family(self, guild):
        """{'name','gen','server','title'} семьи этого сервера."""
        return family_profile(self.db, guild_id_of(guild) if guild is not None else self.home_id)

    def brand(self, guild, text):
        """Текст, написанный под семью по умолчанию, → под семью этого сервера."""
        if guild is None:
            return text
        return family_brand(self.db, guild_id_of(guild), text)

    async def sync_commands(self, guild):
        """Полный набор команд на сервере семьи."""
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self._synced_ids.add(guild.id)

    def dispatch(self, event_name, /, *args, **kwargs):
        """События с серверов без семьи (союзники по альянсу) получает только модуль альянса."""
        gid = event_guild_id(args)
        if gid is None or self.is_family_id(gid):
            return super().dispatch(event_name, *args, **kwargs)
        allowed = {c for c in (self.get_cog("Alliance"), self.get_cog("Families")) if c}
        if not allowed:
            return
        ev = "on_" + event_name
        for fn in self.extra_events.get(ev, []):
            if getattr(fn, "__self__", None) in allowed:
                self._schedule_event(fn, ev, *args, **kwargs)

    async def setup_hook(self):
        for ext in config.EXTENSIONS:
            try:
                await self.load_extension(ext)
            except Exception:
                log.exception("Модуль %s не загрузился", ext)
        self.tree.on_error = self.on_app_error
        self.invite = (f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
                       f"&permissions=8&scope=bot%20applications.commands")
        self._synced = False
        if config.GUILD_ID:
            g = discord.Object(config.GUILD_ID)
            self.tree.copy_global_to(guild=g)
            try:
                await self.tree.sync(guild=g)
                self._synced = True
                self._synced_ids.add(config.GUILD_ID)
            except discord.Forbidden:
                log.error("Нет доступа к серверу GUILD_ID=%s — после подключения проверю, на каких серверах бот.",
                          config.GUILD_ID)

    async def on_ready(self):
        log.info("Бот запущен как %s (серверов: %d)", self.user, len(self.guilds))
        # запоминаем домашний сервер: если GUILD_ID пропадёт из окружения, а бот стоит на нескольких серверах,
        # без этого «домашним» (с /обновить, /перезапуск, токеном разработчика) стал бы любой сервер
        hid = config.GUILD_ID or (self.guilds[0].id if len(self.guilds) == 1 else None)
        if hid and str(self.db.get("home_guild") or "") != str(hid) and \
                (config.GUILD_ID or not self.db.get("home_guild")):
            self.db.set("home_guild", str(hid))
        if self.home_id:
            self.db.adopt_orphans(self.home_id)
        name = "Россия Онлайн" if self.families else f"Россия Онлайн · семья {config.FAMILY}"
        await self.change_presence(activity=discord.Game(name=name))
        print("\n================================================")
        print("Серверы, на которых стоит бот:")
        for g in self.guilds:
            print(f"  • {g.name} — ID {g.id}{'  ✅ (GUILD_ID из .env)' if g.id == config.GUILD_ID else ''}")
        if not self.guilds:
            print("  (ни одного!)\n❌ Бот не добавлен на сервер. Открой ссылку в браузере и добавь его:\n  " + self.invite)
        else:
            for g in self.home_guilds:
                if g.id in self._synced_ids:
                    continue
                try:
                    await self.sync_commands(g)
                    print(f"✅ Команды зарегистрированы на сервере «{g.name}».")
                except discord.HTTPException:
                    print(f"❌ «{g.name}»: нет права на команды. Пригласи бота ещё раз:\n  {self.invite}")
            self._synced = True
            if config.GUILD_ID not in [g.id for g in self.guilds] and len(self.guilds) == 1:
                print(f"💡 Поставь в .env: GUILD_ID={self.guilds[0].id}")
        print("================================================\n", flush=True)

    async def on_app_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            return
        orig = getattr(error, "original", error)
        log.exception("Ошибка команды /%s", inter.command.name if inter.command else "?", exc_info=orig)
        if isinstance(orig, discord.Forbidden):
            text = "❌ Боту не хватает прав. Перетащи роль бота на самый верх списка ролей."
        else:  # внутренности (пути, SQL) людям не показываем — подробности уже в журнале ошибок разработчику
            text = (f"❌ Что-то пошло не так ({type(orig).__name__}). Ошибка уже записана и уйдёт разработчику — "
                    "попробуй ещё раз чуть позже.")
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    # ---------- роли и каналы ----------
    def role(self, guild, key):
        rid = self.db.gget(guild, f"role:{key}")
        r = guild.get_role(int(rid)) if rid else None
        return r or discord.utils.get(guild.roles, name=config.ROLE_NAMES.get(key, ""))

    def ch(self, guild, key):
        cid = self.db.gget(guild, f"ch:{key}")
        return guild.get_channel(int(cid)) if cid else None

    def role_ids(self, guild, keys):
        return {int(self.db.gget(guild, f"role:{k}") or 0) for k in keys} - {0}

    def has(self, member, keys):
        if not isinstance(member, discord.Member):
            return False
        if not self.is_family(member.guild):
            return False  # роли семьи есть только на серверах семей
        if member.guild_permissions.administrator:
            return True
        ids = self.role_ids(member.guild, keys)
        return any(r.id in ids for r in member.roles)

    def in_family(self, member):
        if not isinstance(member, discord.Member):
            return False
        ids = self.role_ids(member.guild, config.FAMILY_RANKS)
        return any(r.id in ids for r in member.roles)

    def family_members(self, guild):
        ids = self.role_ids(guild, config.FAMILY_RANKS)
        return [m for m in guild.members if not m.bot and any(r.id in ids for r in m.roles)]

    def rank_index(self, member):
        """Место в иерархии семьи: 0 — Глава … 10 — Новичок, 99 — не в семье. Владелец сервера — выше всех.
        Считаем по рангам, а не по самой высокой роли: «🔴 В эфире» и личные роли бустеров стоят высоко,
        но власти не дают."""
        if not isinstance(member, discord.Member):
            return 99
        if member.id == member.guild.owner_id:
            return -1
        ids = [self.db.gget(member.guild, f"role:{k}") for k in config.ROSTER_ORDER]
        have = {str(r.id) for r in member.roles}
        for i, rid in enumerate(ids):
            if rid and rid in have:
                return i
        return 99

    def outranks(self, actor, target):
        """Может ли actor применять кадровые меры к target (строго выше по иерархии)."""
        return self.rank_index(actor) < self.rank_index(target)

    def can_post(self, actor, target, post):
        """Может ли actor выдать/снять должность post участнику target → (можно, причина отказа)."""
        if self.has(actor, ["leader", "deputy"]):
            return True, ""
        if not self.has(actor, config.POST_GIVERS):
            return False, "Должности выдают руководство и старший состав."
        if post not in config.JUNIOR_POSTS:
            return False, "Эту должность выдают только Глава и заместитель."
        if target.id == actor.id:
            return False, "Себе должность выдать нельзя."
        if not self.in_family(target):
            return False, "Этот человек не в семье."
        if self.has(target, config.LEAD + ["hr_head"]):
            return False, "Должности руководства меняют только Глава и заместитель."
        return True, ""

    def rank_name(self, member):
        for key in config.ROSTER_ORDER:
            r = self.role(member.guild, key)
            if r and r in member.roles:
                return r.name
        return "—"

    # ---------- оформление ----------
    def embed(self, title=None, description=None, color=config.COLOR, guild=None):
        e = discord.Embed(title=title, description=description, color=color)
        icon = guild.icon.url if guild and guild.icon else None
        if guild is not None:
            e.title, e.description = self.brand(guild, e.title), self.brand(guild, e.description)
        e.set_footer(text=self.brand(guild, config.FOOTER), icon_url=icon)
        return e

    async def log(self, guild, text, key="logs", color=0x607D8B):
        c = self.ch(guild, key)
        if c:
            try:
                await c.send(embed=discord.Embed(description=text, color=color, timestamp=now()))
            except discord.HTTPException:
                pass

    async def panel_once(self, guild, key, channel_key, repost, make_kwargs, version=1):
        """Отправляет панель один раз. Если панель устарела (новая версия) или repost — пересоздаёт."""
        c = self.ch(guild, channel_key)
        if not c:
            return None
        old = self.db.gget(guild, f"panel:{key}")
        old_ver = int(self.db.gget(guild, f"panelv:{key}") or 0)
        if old:
            try:
                msg = await c.fetch_message(int(old))
                if not repost and old_ver >= version:
                    return msg
                await msg.delete()
            except discord.HTTPException:
                pass
        kwargs = await make_kwargs() if callable(make_kwargs) else make_kwargs
        msg = await c.send(**kwargs)
        self.db.gset(guild, f"panel:{key}", msg.id)
        self.db.gset(guild, f"panelv:{key}", version)
        return msg

    # ---------- общие действия ----------
    def emit(self, guild, kind, **data):
        """Событие семьи → летопись, наставничество и т.д."""
        self.dispatch("family_event", guild, kind, data)

    async def set_rank(self, member, key, by, reason=None, silent=False):
        g = member.guild
        remove = [self.role(g, k) for k in config.BASE_RANKS if k != key] + [self.role(g, "guest")]
        remove = [r for r in remove if r and r in member.roles]
        if remove:
            await member.remove_roles(*remove, reason=reason)
        role = self.role(g, key)
        if role and role not in member.roles:
            await member.add_roles(role, reason=reason)
        name = config.BASE_RANKS.get(key, key)
        if not silent:
            await self.log(g, f"🔁 {member.mention} → **{name}** ({by.mention if by else 'бот'})"
                              f"{' · ' + reason if reason else ''}", "hrlog", 0x1E88E5)
            await safe_dm(member, f"🔁 Твой ранг в семье {config.FAMILY} изменён: **{name}**")
        self.emit(g, "rank", member=member, rank=key, by=by)

    async def expel(self, member, by, reason):
        g = member.guild
        roles = [self.role(g, k) for k in config.FAMILY_RANKS + ["vacation"]]
        roles = [r for r in roles if r and r in member.roles]
        if roles:
            await member.remove_roles(*roles, reason=reason)
        guest = self.role(g, "guest")
        if guest:
            await member.add_roles(guest)
        await self.log(g, f"🚪 {member.mention} исключён из семьи ({by.mention if by else 'бот'}): {reason}",
                       "hrlog", config.RED)
        await safe_dm(member, f"🚪 Ты исключён из семьи {config.FAMILY}. Причина: {reason}")
        self.emit(g, "expelled", member=member, by=by, reason=reason)

    async def give_warn(self, guild, member, moderator, reason):
        self.db.q("INSERT INTO warns(guild_id, user_id, moderator_id, reason, created_at) VALUES(?,?,?,?,?)",
                  guild.id, member.id, moderator.id, reason, now_iso())
        n = self.db.one("SELECT COUNT(*) n FROM warns WHERE guild_id=? AND user_id=? AND active=1", guild.id, member.id)["n"]
        lim = self.num(guild, "warn_limit")
        e = self.embed(f"⚖️ Выговор ({n}/{lim})",
                       f"**Кому:** {member.mention}\n**Выдал:** {moderator.mention}\n**Причина:** {reason}",
                       config.RED, guild)
        c = self.ch(guild, "warns")
        if c:
            await c.send(embed=e)
        await safe_dm(member, f"⚖️ Ты получил выговор в семье {config.FAMILY} ({n}/{lim}).\nПричина: {reason}")
        if n >= lim:
            council = self.ch(guild, "council_chat")
            if council:
                try:
                    from cogs.staff import limit_view  # кнопки решения: исключить · дать шанс · снять выговор
                    view = limit_view(member.id)
                except Exception:  # noqa: BLE001 — без кнопок сообщение всё равно нужно
                    view = discord.utils.MISSING
                await council.send(f"🚨 У {member.mention} **{n} активных выговора** (лимит {lim}). Решите:",
                                   view=view)
        return n

    def blacklist_find(self, guild, discord_id=None, static=None, nick=None):
        """Активная запись ЧС по Discord ID, Static ID или нику."""
        rows = self.db.all("SELECT * FROM blacklist WHERE guild_id=? AND active=1", guild_id_of(guild))
        n = norm_nick(nick) if nick else None
        t = now()
        for r in rows:
            if r["until"] and parse_dt(r["until"]) < t:
                continue
            if discord_id and r["discord_id"] and int(r["discord_id"]) == int(discord_id):
                return r
            if static and r["static_id"] and str(r["static_id"]).strip() == str(static).strip():
                return r
            if n and r["nick"] and norm_nick(r["nick"]) == n:
                return r
        # общий ЧС альянса обязателен только для семей альянса — чужой семье он не указ
        if self.db.one("SELECT 1 FROM alliance_families WHERE guild_id=? AND status='active'", guild_id_of(guild)):
            return self.alliance_bl_find(discord_id, static, nick)
        return None

    def alliance_bl_find(self, discord_id=None, static=None, nick=None):
        """Общий чёрный список альянса. Возвращает словарь с теми же полями, что и ЧС семьи."""
        n = norm_nick(nick) if nick else None
        for r in self.db.all("SELECT b.*, f.family FROM alliance_bl b LEFT JOIN alliance_families f "
                             "ON f.guild_id=b.family_gid WHERE b.active=1"):
            if (discord_id and r["discord_id"] and int(r["discord_id"]) == int(discord_id)) or \
                    (static and r["static_id"] and str(r["static_id"]).strip() == str(static).strip()) or \
                    (n and r["nick"] and norm_nick(r["nick"]) == n):
                d = dict(r)
                d["id"] = f"А-{r['id']}"
                d["reason"] = f"{r['reason']} (ЧС альянса · внесла семья {r['family'] or '?'})"
                d["until"] = None
                return d
        return None


def norm_nick(s):
    return "".join(ch for ch in (s or "").lower().replace("ё", "е") if ch.isalnum())
