# -*- coding: utf-8 -*-
"""Массовый перевод новичков: в «готовы» попадает только тот, кто отбыл испытательный срок."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core import family  # noqa: E402
from core.botcore import now  # noqa: E402
from core.db import DB  # noqa: E402
from cogs.mentorship import Mentorship, clamp_days  # noqa: E402

GID = 5


class FakeBot:
    def __init__(self, db, newbie_role):
        self.db, self._newbie = db, newbie_role

    def role(self, guild, key):
        return self._newbie if key == "newbie" else None

    def feature(self, guild, key):
        return False  # экзамен выключен — проверяем только выслугу и активность

    def num(self, guild, key):
        return family.number(self.db, guild.id, key)

    def outranks(self, actor, target):
        return True

    def get_cog(self, name):
        return None


class NewbiePool(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.guild = SimpleNamespace(id=GID)
        self.members = []
        self.role = SimpleNamespace(members=self.members)
        self.cog = object.__new__(Mentorship)  # без __init__: цикл due_loop в тесте не нужен
        self.cog.bot = FakeBot(self.db, self.role)

    def newbie(self, uid, joined_days_ago):
        m = SimpleNamespace(id=uid, bot=False, voice=None, roles=[], display_name=f"n{uid}",
                            joined_at=now() - dt.timedelta(days=joined_days_ago))
        self.members.append(m)
        self.db.q("INSERT INTO activity(guild_id, user_id, last_seen) VALUES(?,?,?)", GID, uid, now().isoformat())
        return m

    def names(self, bucket):
        return {m.id for m in bucket}

    def test_fresh_not_ready_old_ready(self):
        self.newbie(1, 2)    # пришёл 2 дня назад
        self.newbie(2, 30)   # месяц в семье
        ready, idle, no_exam, fresh, higher = self.cog.newbie_pool(self.guild)
        self.assertEqual(self.names(ready), {2})
        self.assertEqual(self.names(fresh), {1})

    def test_probation_setting_and_explicit_days(self):
        self.newbie(1, 10)
        self.db.gset(GID, "probation:days", 14)  # срок семьи 14 дн. — 10 дней мало
        self.assertEqual(self.names(self.cog.newbie_pool(self.guild)[3]), {1})
        # в команде явно «7 дней или более» — главнее настройки
        self.assertEqual(self.names(self.cog.newbie_pool(self.guild, min_days=7)[0]), {1})

    def test_application_date_beats_join(self):
        self.newbie(1, 100)  # на сервере давно, но принят в семью вчера
        self.db.q("INSERT INTO applications(guild_id, user_id, status, created_at, decided_at) VALUES(?,?,?,?,?)",
                  GID, 1, "accepted", (now() - dt.timedelta(days=3)).isoformat(),
                  (now() - dt.timedelta(days=1)).isoformat())
        self.assertEqual(self.names(self.cog.newbie_pool(self.guild)[3]), {1})

    def test_active_term_uses_due_at(self):
        self.newbie(1, 30)
        start = now() - dt.timedelta(days=8)  # срок начат 8 дн. назад, но продлён — due_at ещё впереди
        self.db.q("INSERT INTO mentorships(guild_id, newbie_id, mentor_id, checklist, started_at, due_at, status) "
                  "VALUES(?,?,?,?,?,?,?)", GID, 1, 0, "[]", start.isoformat(),
                  (now() + dt.timedelta(days=2)).isoformat(), "active")
        self.assertEqual(self.names(self.cog.newbie_pool(self.guild)[3]), {1})

    def test_other_guild_term_ignored(self):
        self.newbie(1, 30)
        self.db.q("INSERT INTO mentorships(guild_id, newbie_id, mentor_id, checklist, started_at, due_at, status) "
                  "VALUES(?,?,?,?,?,?,?)", GID + 1, 1, 0, "[]", now().isoformat(),
                  (now() + dt.timedelta(days=7)).isoformat(), "active")
        self.assertEqual(self.names(self.cog.newbie_pool(self.guild)[0]), {1})

    def test_clamp_days(self):
        self.assertIsNone(clamp_days(None))
        self.assertIsNone(clamp_days("мусор"))
        self.assertEqual(clamp_days("7"), 7)
        self.assertEqual(clamp_days(9999), 365)


if __name__ == "__main__":
    unittest.main()
