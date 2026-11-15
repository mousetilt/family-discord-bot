# -*- coding: utf-8 -*-
"""SQLite. Почти все данные привязаны к серверу семьи (guild_id) — так один бот обслуживает несколько семей.
Глобальное: settings (служебное: home_guild, hub:*, whisper:*), ai_usage, alliance_*.
Настройки сервера (роли, каналы, панели…) — guild_settings, доступ через gget/gset."""
import logging
import os
import shutil
import sqlite3

import discord

log = logging.getLogger("family-bot.db")

SCHEMA_VERSION = 3  # v3: казна (treasury_tx), штрафы (fines) — новые таблицы, данные не трогаются

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS guild_settings(
    guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS applications(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, nick TEXT,
    static_id TEXT, age_exp TEXT, reason TEXT, online TEXT, status TEXT DEFAULT 'exam',
    reviewer_id INTEGER, deny_reason TEXT, created_at TEXT, message_id INTEGER,
    exam_score INTEGER, exam_total INTEGER, exam_attempt INTEGER, exam_wrong TEXT,
    situational TEXT, ai_review TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS exam_questions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, question TEXT, options TEXT,
    answer INTEGER, topic TEXT, source TEXT, enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS exam_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, score INTEGER,
    total INTEGER, passed INTEGER, training INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS mentorships(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, newbie_id INTEGER, mentor_id INTEGER,
    thread_id INTEGER, message_id INTEGER, checklist TEXT, started_at TEXT, due_at TEXT, status TEXT DEFAULT 'active',
    asked INTEGER DEFAULT 0, voice_start INTEGER DEFAULT 0, closed_at TEXT);
CREATE TABLE IF NOT EXISTS temp_voice(
    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL DEFAULT 0, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS warns(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, moderator_id INTEGER,
    reason TEXT, created_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS activity(
    guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, voice_seconds INTEGER DEFAULT 0,
    messages INTEGER DEFAULT 0, last_seen TEXT, week_voice INTEGER DEFAULT 0, week_messages INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS events(
    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL DEFAULT 0, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS vacations(
    guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, until TEXT, reason TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS flags(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, channel_id INTEGER,
    message_id INTEGER, content TEXT, category TEXT, reason TEXT, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS kb_docs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, title TEXT, url TEXT, category TEXT,
    content TEXT, added_by INTEGER, added_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chronicle(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, kind TEXT, text TEXT, user_id INTEGER,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS relations(
    guild_id INTEGER NOT NULL DEFAULT 0, family TEXT, status TEXT, note TEXT, updated_by INTEGER, updated_at TEXT,
    PRIMARY KEY(guild_id, family));
CREATE TABLE IF NOT EXISTS treaties(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, family TEXT, title TEXT, terms TEXT,
    until TEXT, created_by INTEGER, created_at TEXT, message_id INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS negotiations(
    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL DEFAULT 0, family TEXT, requester_id INTEGER,
    purpose TEXT, created_at TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS blacklist(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, discord_id INTEGER, static_id TEXT,
    nick TEXT, reason TEXT, added_by INTEGER, created_at TEXT, until TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS market(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, kind TEXT,
    category TEXT, title TEXT, price TEXT, price_num INTEGER, description TEXT, message_id INTEGER,
    status TEXT DEFAULT 'active', created_at TEXT, bumped_at TEXT, sold_price INTEGER, sold_at TEXT,
    has_image INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS streams(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, platform TEXT,
    title TEXT, url TEXT, game TEXT, started_at TEXT, ended_at TEXT, message_id INTEGER, channel_id INTEGER,
    manual INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS appeals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, kind TEXT,
    anon INTEGER, topic TEXT, text TEXT, status TEXT DEFAULT 'open', thread_id INTEGER, message_id INTEGER,
    created_at TEXT, closed_by INTEGER, closed_at TEXT);
CREATE TABLE IF NOT EXISTS persona(
    guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, score INTEGER DEFAULT 0, notes TEXT DEFAULT '[]',
    updated TEXT, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS alliance_families(
    guild_id INTEGER PRIMARY KEY, family TEXT, status TEXT, family_role_id INTEGER, lead_role_id INTEGER,
    ally_role_id INTEGER, cat_id INTEGER, chat_id INTEGER, news_id INTEGER, council_id INTEGER, bl_id INTEGER,
    webhook TEXT, joined_at TEXT, added_by INTEGER);
CREATE TABLE IF NOT EXISTS alliance_votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, text TEXT, target INTEGER, created_by INTEGER,
    created_gid INTEGER, created_at TEXT, ends_at TEXT, status TEXT DEFAULT 'open', messages TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS alliance_ballots(
    vote_id INTEGER, guild_id INTEGER, user_id INTEGER, choice TEXT, PRIMARY KEY(vote_id, guild_id));
CREATE TABLE IF NOT EXISTS alliance_bl(
    id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id INTEGER, static_id TEXT, nick TEXT, reason TEXT,
    family_gid INTEGER, added_by INTEGER, created_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS alliance_log(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS treasury_tx(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER, actor_id INTEGER,
    amount INTEGER NOT NULL, kind TEXT NOT NULL, reason TEXT, ts INTEGER NOT NULL);  -- kind: in | out | due
CREATE TABLE IF NOT EXISTS fines(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, actor_id INTEGER,
    amount INTEGER NOT NULL, reason TEXT, paid INTEGER DEFAULT 0, ts INTEGER NOT NULL, paid_ts INTEGER,
    reminded INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_treasury_g ON treasury_tx(guild_id, kind, ts);
CREATE INDEX IF NOT EXISTS ix_fines_g ON fines(guild_id, paid);
CREATE INDEX IF NOT EXISTS ix_applications_g ON applications(guild_id, user_id);
CREATE INDEX IF NOT EXISTS ix_warns_g ON warns(guild_id, user_id);
CREATE INDEX IF NOT EXISTS ix_market_g ON market(guild_id, status);
CREATE INDEX IF NOT EXISTS ix_mentorships_g ON mentorships(guild_id, status);
"""

# Колонки, которые могли появиться в новых версиях (для старых баз)
MIGRATIONS = {
    "applications": {"exam_score": "INTEGER", "exam_total": "INTEGER", "exam_attempt": "INTEGER",
                     "exam_wrong": "TEXT", "situational": "TEXT", "ai_review": "TEXT", "decided_at": "TEXT",
                     "manual": "INTEGER DEFAULT 0"},  # 1 — принят вручную, минуя экзамен
    "mentorships": {"reminded": "INTEGER DEFAULT 0"},  # 1 — напомнили на середине срока, 2 — за сутки до конца
    "events": {"created_at": "TEXT"},
    # семья альянса без нашего бота: guild_id — условный отрицательный номер, участники — по роли в штабе,
    # leaders — JSON-список Discord ID её руководства (голосуют из штаба)
    "alliance_families": {"external": "INTEGER DEFAULT 0", "leaders": "TEXT"},
}

# ---- миграция v1 → v2 (всё на guild_id) ----
# Таблицы, в которые просто добавляется колонка guild_id (первичный ключ не меняется)
ADD_GID = ["applications", "exam_questions", "exam_attempts", "mentorships", "temp_voice", "warns", "events", "flags",
           "kb_docs", "chronicle", "treaties", "negotiations", "blacklist", "market", "streams", "appeals"]
# Таблицы, где ключ был «один на всех» (user_id/family) — пересоздаются с ключом (guild_id, …)
REBUILD = {
    "activity": ("user_id, voice_seconds, messages, last_seen, week_voice, week_messages"),
    "vacations": ("user_id, until, reason"),
    "relations": ("family, status, note, updated_by, updated_at"),
    "persona": ("user_id, score, notes, updated"),
}
# Глобальные ключи settings (остальные переезжают в guild_settings домашнего сервера)
GLOBAL_KEYS = ("home_guild", "alliance_charter", "bot_avatar_set")  # аватар — у бота один на всех
GLOBAL_PREFIXES = ("hub:", "whisper:", "tts:", "update:")  # tts:voice — пока общий голос (озвучка не знает сервер)
ORPHAN = -1  # guild_id «ещё не знаем, чей» — проставится, когда бот узнает свой сервер


def is_global_key(key):
    return key in GLOBAL_KEYS or key.startswith(GLOBAL_PREFIXES)


class DB:
    def __init__(self, path, home_gid=0):
        self.path = path
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self._migrate_legacy(home_gid)
        self.c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = self._cols(table)
            for col, typ in cols.items():
                if col not in have:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.c.commit()

    # ---------- миграции ----------
    def _cols(self, table):
        return {r["name"] for r in self.c.execute(f"PRAGMA table_info({table})")}

    def _has_table(self, table):
        return bool(self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def _migrate_legacy(self, home_gid):
        """Старая база (без guild_id) → всё привязываем к домашнему серверу. Один раз, в одной транзакции,
        перед этим — копия файла базы рядом (*.bak-v1)."""
        if self.c.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        if not self._has_table("applications") or "guild_id" in self._cols("applications"):
            return  # новая (пустая) база или уже мигрирована
        if not home_gid:
            r = self.c.execute("SELECT value FROM settings WHERE key='home_guild'").fetchone()
            home_gid = int(r[0]) if r and str(r[0]).isdigit() else 0
        gid = home_gid or ORPHAN
        if self.path != ":memory:" and os.path.isfile(self.path):
            bak = self.path + ".bak-v1"
            if not os.path.exists(bak):
                shutil.copy2(self.path, bak)
                log.warning("Копия базы перед миграцией: %s", bak)
        log.warning("Миграция базы на мультисемейность (guild_id=%s)…", gid)
        c = self.c
        try:
            c.execute("BEGIN")
            for t in ADD_GID:
                if self._has_table(t) and "guild_id" not in self._cols(t):
                    c.execute(f"ALTER TABLE {t} ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
                    c.execute(f"UPDATE {t} SET guild_id=?", (gid,))
            # общие правила проекта (ООС) — для всех семей; остальное — домашнего сервера
            c.execute("UPDATE kb_docs SET guild_id=0 WHERE category='ooc'")
            for t, cols in REBUILD.items():
                if not self._has_table(t) or "guild_id" in self._cols(t):
                    continue
                c.execute(f"ALTER TABLE {t} RENAME TO {t}_v1")
            # новые таблицы создаём из SCHEMA, затем переносим данные
            for stmt in SCHEMA.split(";"):
                s = stmt.strip()
                if s.startswith("CREATE TABLE") and any(f" {t}(" in s for t in list(REBUILD) + ["guild_settings"]):
                    c.execute(s)
            for t, cols in REBUILD.items():
                if self._has_table(f"{t}_v1"):
                    c.execute(f"INSERT INTO {t}(guild_id, {cols}) SELECT ?, {cols} FROM {t}_v1", (gid,))
                    c.execute(f"DROP TABLE {t}_v1")
            for r in c.execute("SELECT key, value FROM settings").fetchall():
                if not is_global_key(r["key"]):
                    c.execute("INSERT OR REPLACE INTO guild_settings(guild_id, key, value) VALUES(?,?,?)",
                              (gid, r["key"], r["value"]))
                    c.execute("DELETE FROM settings WHERE key=?", (r["key"],))
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            log.exception("Миграция базы не удалась — откатил, база не изменена")
            raise
        log.warning("Миграция базы завершена.")

    def adopt_orphans(self, gid):
        """Если при миграции свой сервер был неизвестен — отдаём ему «ничьи» данные."""
        if not gid:
            return
        n = 0
        for t in ADD_GID + list(REBUILD) + ["guild_settings"]:
            n += self.c.execute(f"UPDATE {t} SET guild_id=? WHERE guild_id=?", (gid, ORPHAN)).rowcount
        self.c.commit()
        if n:
            log.warning("Данные старой базы привязаны к серверу %s (%d строк)", gid, n)

    # ---------- запросы ----------
    def q(self, sql, *args):
        cur = self.c.execute(sql, args)
        self.c.commit()
        return cur

    def one(self, sql, *args):
        return self.c.execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.c.execute(sql, args).fetchall()

    # ---------- глобальные настройки (служебное) ----------
    def get(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", key)
        return r["value"] if r else default

    def set(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               key, str(value))

    # ---------- настройки сервера ----------
    def gget(self, gid, key, default=None):
        r = self.one("SELECT value FROM guild_settings WHERE guild_id=? AND key=?", gid_of(gid), key)
        return r["value"] if r else default

    def gset(self, gid, key, value):
        self.q("INSERT INTO guild_settings(guild_id,key,value) VALUES(?,?,?) "
               "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", gid_of(gid), key, str(value))

    def gdel(self, gid, key):
        self.q("DELETE FROM guild_settings WHERE guild_id=? AND key=?", gid_of(gid), key)

    def touch(self, gid, uid, voice=0, msgs=0):
        now = discord.utils.utcnow().isoformat()
        self.q("""INSERT INTO activity(guild_id, user_id, voice_seconds, messages, last_seen, week_voice, week_messages)
                  VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(guild_id, user_id) DO UPDATE SET voice_seconds=voice_seconds+excluded.voice_seconds,
                  messages=messages+excluded.messages, last_seen=excluded.last_seen,
                  week_voice=week_voice+excluded.week_voice, week_messages=week_messages+excluded.week_messages""",
               gid_of(gid), uid, voice, msgs, now, voice, msgs)


def gid_of(g):
    """guild / member / interaction / message / int → ID сервера (int)."""
    if isinstance(g, int):
        return g
    if isinstance(g, (discord.Guild, discord.Object)):
        return g.id
    guild = getattr(g, "guild", None)
    if guild is not None:
        return guild.id
    gid = getattr(g, "guild_id", None)
    if isinstance(gid, int):
        return gid
    raise TypeError(f"Не могу определить сервер из {type(g).__name__}")
