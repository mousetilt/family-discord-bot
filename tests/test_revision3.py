import asyncio
import os
import sqlite3
import tempfile
import unittest
import zipfile


from core.family import NUMBER_GROUPS, NUMBERS


class NumberGroups(unittest.TestCase):
    def test_modal_fits(self):
        # окно Discord вмещает не больше 5 полей — иначе /настройки → Числа не откроется
        for g, (title, keys) in NUMBER_GROUPS.items():
            self.assertLessEqual(len(keys), 5, g)
            self.assertLessEqual(len(title), 45, g)

    def test_all_numbers_reachable(self):
        grouped = [k for _, ks in NUMBER_GROUPS.values() for k in ks]
        self.assertEqual(sorted(grouped), sorted(set(grouped)))
        self.assertEqual(set(grouped), set(NUMBERS))

    def test_modal_builds(self):
        from cogs.control import NumbersModal

        class Bot:
            def num(self, guild, k):
                return NUMBERS[k][1]

        async def build():
            for g in NUMBER_GROUPS:
                NumbersModal(Bot(), None, g)
        asyncio.run(build())


class Backup(unittest.TestCase):
    def test_no_secrets(self):
        from cogs.daily import make_backup
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE guild_settings (guild_id INTEGER, key TEXT, value TEXT)")
        c.executemany("INSERT INTO settings VALUES (?,?)", [("dev:gh_token", "x"), ("home_guild", "1")])
        c.executemany("INSERT INTO guild_settings VALUES (1,?,?)",
                      [("ai:gemini", "k"), ("ai:claude", "k"), ("ai:primary", "claude"), ("forum:pass", "p")])
        c.commit()
        c.close()
        buf = make_backup(path, home_gid=1)
        out = os.path.join(tempfile.mkdtemp(), "o.db")
        with zipfile.ZipFile(buf) as z, open(out, "wb") as f:
            f.write(z.read("family.db"))
        d = sqlite3.connect(out)
        self.assertEqual({r[0] for r in d.execute("SELECT key FROM settings")}, {"home_guild"})
        self.assertEqual({r[0] for r in d.execute("SELECT key FROM guild_settings")}, {"ai:primary"})
        d.close()


class PublicUrl(unittest.TestCase):
    def test_blocks_internal(self):
        from core.kb import check_public_url
        for u in ("http://127.0.0.1:8080/x", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/",
                  "file:///etc/passwd", "ftp://example.com/"):
            with self.assertRaises(ValueError, msg=u):
                asyncio.run(check_public_url(u))


class Health(unittest.TestCase):
    """/семья проверка: находит проблемы и предлагает правильную кнопку."""

    def make(self, built=True, keys=None):
        import types
        from core.ai import AI
        from core.db import DB
        db = DB(":memory:")
        perms = types.SimpleNamespace(administrator=True)
        top = types.SimpleNamespace(position=99)
        guild = types.SimpleNamespace(guild_id=7, id=7, roles=[], channels=[],
                                      me=types.SimpleNamespace(guild_permissions=perms, top_role=top))
        if built:
            db.gset(7, "role:leader", 1)

        class Bot:
            intents = types.SimpleNamespace(members=True)

            def __init__(self):
                self.db = db

            def feature(self, g, k):
                return k in ("lawyer", "forum")

            def role(self, g, k):
                return object() if k != "treasurer" else None

            def ch(self, g, k):
                return None if k == "news" else object()

            def is_home(self, g):
                return False

            def ai_for(self, g):
                return AI(db, keys=keys or {}, scope=7)
        return Bot(), guild

    def test_not_built(self):
        from core import health
        bot, g = self.make(built=False)
        items = health.check(bot, g)
        self.assertTrue(any(i.fix == "wizard" and i.level == health.BAD for i in items))

    def test_problems(self):
        from core import health
        bot, g = self.make()
        items = health.check(bot, g)
        fixes = {i.fix for i in items if i.level != health.OK}
        self.assertTrue({"ai", "build", "profile"} <= fixes, fixes)
        text = health.render(items)
        self.assertIn("форума", text)
        self.assertIn("База юриста", text)
        self.assertTrue(health.summary(items).startswith("❌"))

    def test_ai_ok(self):
        from core import health
        bot, g = self.make(keys={"groq": "q", "gemini": "g"})
        items = health.check(bot, g)
        self.assertFalse(any(i.fix == "ai" for i in items))


class HostBackups(unittest.TestCase):
    """Копии на хосте: ротация, проверка файла, восстановление с сохранением ключей."""

    def db(self, path, note):
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER, key TEXT, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS applications (id INTEGER)")
        c.execute("INSERT INTO settings VALUES ('note', ?)", (note,))
        c.commit()
        return c

    def test_rotation_and_restore(self):
        from cogs.daily import check_copy, host_backup, host_backups, restore_into
        d = tempfile.mkdtemp()
        live_path = os.path.join(d, "live.db")
        live = self.db(live_path, "old")
        folder = os.path.join(d, "backups")
        for day in range(1, 10):
            host_backup(live_path, folder, f"family-2026-11-{day:02d}.db", keep=7)
        self.assertEqual(len(host_backups(folder)), 7)
        self.assertEqual(host_backups(folder)[0][1], "2026-11-09")
        # после копии база ушла вперёд, и появились ключи
        live.execute("UPDATE settings SET value='new' WHERE key='note'")
        live.execute("INSERT INTO guild_settings VALUES (1, 'ai:gemini', 'secret')")
        live.execute("INSERT INTO settings VALUES ('dev:gh_token', 'tok')")
        live.commit()
        restore_into(live, os.path.join(folder, "family-2026-11-09.db"))
        self.assertEqual(live.execute("SELECT value FROM settings WHERE key='note'").fetchone()[0], "old")
        self.assertEqual(live.execute("SELECT value FROM guild_settings WHERE key='ai:gemini'").fetchone()[0], "secret")
        self.assertEqual(live.execute("SELECT value FROM settings WHERE key='dev:gh_token'").fetchone()[0], "tok")
        live.close()
        junk = os.path.join(d, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"not a database at all" * 50)
        with self.assertRaises(ValueError):
            check_copy(junk)


class CommandGroups(unittest.TestCase):
    """Служебные команды — в /бот и /помощник; после перезагрузки модуля не дублируются и привязаны к нему."""

    def test_groups(self):
        import logging
        from discord import app_commands
        from core import config
        from core.botcore import FamilyBot
        logging.disable(logging.CRITICAL)

        async def run():
            bot = FamilyBot(presences=False)
            for ext in config.EXTENSIONS:
                await bot.load_extension(ext)
            roots = [c.name for c in bot.tree.get_commands()]
            await bot.reload_extension("cogs.persona")
            roots2 = [c.name for c in bot.tree.get_commands()]
            grp = bot.tree.get_command("помощник")
            visits = grp.get_command("визиты")
            same = visits.binding is bot.get_cog("Persona")
            subs = {g: sorted(c.name for c in bot.tree.get_command(g).commands) for g in bot.COMMAND_GROUPS}
            qn = {c.qualified_name for c in bot.tree.walk_commands() if isinstance(c, app_commands.Command)}
            await bot.close()
            return roots, roots2, same, subs, qn

        roots, roots2, same, subs, qn = asyncio.run(run())
        logging.disable(logging.NOTSET)
        for g, (_, _, names) in __import__("core.botcore", fromlist=["FamilyBot"]).FamilyBot.COMMAND_GROUPS.items():
            self.assertEqual(subs[g], sorted(names))
            for n in names:
                self.assertNotIn(n, roots)
        self.assertEqual(sorted(roots), sorted(roots2))
        self.assertTrue(same)
        self.assertLessEqual(len(roots), 75)  # было 86 — запас под новые фичи
        from cogs.hub import ACTIONS
        for key, (_, _, cmd, _) in ACTIONS.items():
            if not cmd:
                continue
            self.assertIn(cmd, qn, f"кнопка панели {key} ведёт на несуществующую команду /{cmd}")


class MorningDigest(unittest.TestCase):
    """Утренняя сводка: сборы на сутки, новые дела форума."""

    def test_today_and_forum(self):
        import json
        import time
        import types
        from cogs import daily
        from cogs.forumwatch import SCHEMA as FSCHEMA
        from core.db import DB
        db = DB(":memory:")
        db.c.executescript(FSCHEMA)
        t = int(time.time())
        db.q("INSERT INTO events(message_id, guild_id, data, created_at) VALUES(1, 7, ?, '')",
             json.dumps({"what": "Стрела", "ts": t + 3600, "yes": [1, 2]}))
        db.q("INSERT INTO events(message_id, guild_id, data, created_at) VALUES(2, 7, ?, '')",
             json.dumps({"what": "Через неделю", "ts": t + 7 * 86400, "yes": []}))
        db.q("INSERT INTO forum_cases(guild_id, thread_id, title, url, role, status, created_ts) "
             "VALUES(7, 5, 'Иск к Ульянову', 'https://f/5', 'defendant', 'pending', ?)", t)

        class FW:
            def case_status(self, g, c):
                return c["status"]

        bot = types.SimpleNamespace(db=db, get_cog=lambda n: FW() if n == "ForumWatch" else None,
                                    feature=lambda g, k: True)
        d = daily.Daily.__new__(daily.Daily)
        d.bot = bot
        g = types.SimpleNamespace(id=7)
        today = d.today_events(g)
        self.assertEqual(len(today), 1)
        self.assertIn("Стрела", today[0])
        self.assertIn("идут: 2", today[0])
        lines = d.forum_lines(g, "2000-01-01T00:00:00+00:00")
        self.assertIn("Иск к Ульянову", lines[0])
        self.assertIn("**1**", lines[-1])


if __name__ == "__main__":
    unittest.main()
