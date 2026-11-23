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


if __name__ == "__main__":
    unittest.main()
