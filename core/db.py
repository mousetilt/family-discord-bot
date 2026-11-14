# -*- coding: utf-8 -*-
import sqlite3
import discord

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applications(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nick TEXT, static_id TEXT,
    age_exp TEXT, reason TEXT, online TEXT, status TEXT DEFAULT 'exam',
    reviewer_id INTEGER, deny_reason TEXT, created_at TEXT, message_id INTEGER,
    exam_score INTEGER, exam_total INTEGER, exam_attempt INTEGER, exam_wrong TEXT,
    situational TEXT, ai_review TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS exam_questions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, options TEXT, answer INTEGER,
    topic TEXT, source TEXT, enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS exam_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, score INTEGER, total INTEGER,
    passed INTEGER, training INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS mentorships(
    id INTEGER PRIMARY KEY AUTOINCREMENT, newbie_id INTEGER, mentor_id INTEGER, thread_id INTEGER,
    message_id INTEGER, checklist TEXT, started_at TEXT, due_at TEXT, status TEXT DEFAULT 'active',
    asked INTEGER DEFAULT 0, voice_start INTEGER DEFAULT 0, closed_at TEXT);
CREATE TABLE IF NOT EXISTS temp_voice(channel_id INTEGER PRIMARY KEY, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS warns(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, moderator_id INTEGER,
    reason TEXT, created_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS activity(
    user_id INTEGER PRIMARY KEY, voice_seconds INTEGER DEFAULT 0, messages INTEGER DEFAULT 0,
    last_seen TEXT, week_voice INTEGER DEFAULT 0, week_messages INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(message_id INTEGER PRIMARY KEY, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS vacations(user_id INTEGER PRIMARY KEY, until TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS flags(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER, message_id INTEGER,
    content TEXT, category TEXT, reason TEXT, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS kb_docs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, category TEXT, content TEXT,
    added_by INTEGER, added_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chronicle(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, text TEXT, user_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS relations(
    family TEXT PRIMARY KEY, status TEXT, note TEXT, updated_by INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS treaties(
    id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT, title TEXT, terms TEXT, until TEXT,
    created_by INTEGER, created_at TEXT, message_id INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS negotiations(
    channel_id INTEGER PRIMARY KEY, family TEXT, requester_id INTEGER, purpose TEXT,
    created_at TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS blacklist(
    id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id INTEGER, static_id TEXT, nick TEXT,
    reason TEXT, added_by INTEGER, created_at TEXT, until TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS market(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, category TEXT, title TEXT,
    price TEXT, price_num INTEGER, description TEXT, message_id INTEGER, status TEXT DEFAULT 'active',
    created_at TEXT, bumped_at TEXT, sold_price INTEGER, sold_at TEXT, has_image INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS streams(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, platform TEXT, title TEXT, url TEXT, game TEXT,
    started_at TEXT, ended_at TEXT, message_id INTEGER, channel_id INTEGER, manual INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS appeals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, anon INTEGER, topic TEXT, text TEXT,
    status TEXT DEFAULT 'open', thread_id INTEGER, message_id INTEGER, created_at TEXT, closed_by INTEGER, closed_at TEXT);
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
"""

# Колонки, которые могли появиться в новых версиях (для старых баз)
MIGRATIONS = {
    "applications": {"exam_score": "INTEGER", "exam_total": "INTEGER", "exam_attempt": "INTEGER",
                     "exam_wrong": "TEXT", "situational": "TEXT", "ai_review": "TEXT", "decided_at": "TEXT"},
    "events": {"created_at": "TEXT"},
}


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.c.commit()

    def q(self, sql, *args):
        cur = self.c.execute(sql, args)
        self.c.commit()
        return cur

    def one(self, sql, *args):
        return self.c.execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.c.execute(sql, args).fetchall()

    def get(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", key)
        return r["value"] if r else default

    def set(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               key, str(value))

    def touch(self, uid, voice=0, msgs=0):
        now = discord.utils.utcnow().isoformat()
        self.q("""INSERT INTO activity(user_id, voice_seconds, messages, last_seen, week_voice, week_messages)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET voice_seconds=voice_seconds+excluded.voice_seconds,
                  messages=messages+excluded.messages, last_seen=excluded.last_seen,
                  week_voice=week_voice+excluded.week_voice, week_messages=week_messages+excluded.week_messages""",
               uid, voice, msgs, now, voice, msgs)
