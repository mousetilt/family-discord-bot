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


if __name__ == "__main__":
    unittest.main()
