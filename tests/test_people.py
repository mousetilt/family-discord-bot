# -*- coding: utf-8 -*-
"""Действие people: списки «кто…» строго по данным сервера (по guild_id), без выдуманных имён."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core import family, people  # noqa: E402
from core.botcore import now  # noqa: E402
from core.db import DB  # noqa: E402
from cogs.mentorship import Mentorship  # noqa: E402
from cogs.treasury import Treasury  # noqa: E402

GID = 7
RANKS = ["newbie", "member", "senior"]


class FakeApps:
    def __init__(self, passed):
        self.passed = passed

    def exam_passed(self, guild, uid):
        return uid in self.passed


class FakeBot:
    def __init__(self, db, roles, members, exam_on=True):
        self.db, self.roles, self.members, self.exam_on = db, roles, members, exam_on
        self.voice_sessions, self.cogs = {}, {}

    def role(self, guild, key):
        return self.roles.get(key)

    def family_members(self, guild):
        fam = [self.roles[k] for k in RANKS]
        return [m for m in self.members if any(r in m.roles for r in fam)]

    def feature(self, guild, key):
        return self.exam_on if key == "hr:exam" else True

    def num(self, guild, key):
        return family.number(self.db, guild.id, key)

    def rank_index(self, m):
        return 0

    def get_cog(self, name):
        return self.cogs.get(name)


class People(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.roles = {k: SimpleNamespace(name=k) for k in RANKS + ["vacation"]}
        self.members = []
        self.guild = SimpleNamespace(id=GID, guild_id=GID, members=self.members)  # guild_id — для db.gget
        self.bot = FakeBot(self.db, self.roles, self.members)
        men = object.__new__(Mentorship)
        men.bot = self.bot
        tr = object.__new__(Treasury)
        tr.bot = self.bot
        self.bot.cogs = {"Mentorship": men, "Treasury": tr, "Applications": FakeApps({2, 3})}

    def add(self, uid, rank, seen_days_ago=0, voice=None):
        m = SimpleNamespace(id=uid, bot=False, roles=[self.roles[rank]], display_name=f"Чел{uid}",
                            joined_at=now() - dt.timedelta(days=30),
                            voice=SimpleNamespace(channel=SimpleNamespace(name=voice)) if voice else None)
        self.members.append(m)
        self.db.q("INSERT INTO activity(guild_id, user_id, last_seen) VALUES(?,?,?)", GID, uid,
                  (now() - dt.timedelta(days=seen_days_ago)).isoformat())
        return m

    def ids(self, a):
        return {m.id for m, _ in people.query(self.bot, self.guild, a)["rows"]}

    def test_without_exam(self):  # «кто без экзамена»
        for uid in (1, 2, 3):
            self.add(uid, "newbie")
        self.assertEqual(self.ids({"exam": False}), {1})
        self.assertEqual(self.ids({"exam": True, "rank": "newbie"}), {2, 3})

    def test_dues_unpaid(self):  # «кто должен взнос»
        self.add(1, "member")
        self.add(2, "member")
        self.add(3, "newbie")  # новички взнос не платят
        self.db.gset(GID, "dues:amount", 100000)
        self.db.gset(GID, "dues:period", "week")
        self.db.q("INSERT INTO treasury_tx(guild_id, user_id, actor_id, amount, kind, reason, ts) VALUES(?,?,?,?,?,?,?)",
                  GID, 2, 2, 100000, "due", "", int(now().timestamp()))
        res = people.query(self.bot, self.guild, {"dues": "unpaid"})
        self.assertEqual({m.id for m, _ in res["rows"]}, {1})
        self.assertIn("из", res["rows"][0][1])
        self.assertEqual(self.ids({"dues": "paid"}), {2})

    def test_dues_not_configured(self):
        self.add(1, "member")
        res = people.query(self.bot, self.guild, {"dues": "unpaid"})
        self.assertEqual(res["rows"], [])
        self.assertTrue(res["notes"])

    def test_voice_and_inactive(self):  # «кто в войсе», «кто 5+ дней неактив»
        self.add(1, "member", 0, voice="Кухня")
        self.add(2, "member", 10)
        self.add(3, "member", 1)
        self.assertEqual(self.ids({"in_voice": True}), {1})
        self.assertEqual(self.ids({"inactive_days": 5}), {2})
        self.assertEqual(self.ids({"active": True}), {1, 3})
        res = people.query(self.bot, self.guild, {"in_voice": True, "inactive_days": 5})
        self.assertEqual(res["rows"], [])  # в войсе = активен по мерке «Неактив»
        self.assertTrue(res["notes"])

    def test_warns_and_other_guild(self):
        self.add(1, "member")
        self.add(2, "member")
        self.db.q("INSERT INTO warns(guild_id, user_id, reason, active) VALUES(?,?,?,1)", GID, 1, "x")
        self.db.q("INSERT INTO warns(guild_id, user_id, reason, active) VALUES(?,?,?,1)", GID + 1, 2, "чужой сервер")
        self.assertEqual(self.ids({"warns": True}), {1})

    def test_rank_and_render_limit(self):
        for uid in range(1, 8):
            self.add(uid, "newbie" if uid < 6 else "senior")
        self.assertEqual(self.ids({"rank": "senior"}), {6, 7})
        text = people.render(people.query(self.bot, self.guild, {"rank": "newbie", "limit": 2}))
        self.assertIn("…и ещё 3", text)
        names = {f"Чел{i}" for i in range(1, 8)}
        listed = {w.strip("•— \n") for w in text.split("\n") if w.startswith("•")}
        self.assertTrue(listed <= names)  # в ответе — только реальные люди

    def test_garbage_filters_ignored(self):
        self.add(1, "member")
        self.assertEqual(self.ids({"rank": "король", "exam": "может", "limit": "много"}), {1})


if __name__ == "__main__":
    unittest.main()
