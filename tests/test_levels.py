# -*- coding: utf-8 -*-
"""Уровни: очки из войса/сборов/взносов/стажа, выговор отнимает, магазин тратит очки, но не уровень."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core.botcore import now  # noqa: E402
from core.db import DB  # noqa: E402
from core.levels import earned, guild_stats, level_of  # noqa: E402
from cogs.levels import SCHEMA  # noqa: E402


class Levels(unittest.TestCase):
    def test_xp(self):
        db = DB(":memory:")
        db.c.executescript(SCHEMA)
        db.c.executescript("CREATE TABLE IF NOT EXISTS treasury_tx(id INTEGER PRIMARY KEY, guild_id INTEGER, "
                           "user_id INTEGER, actor_id INTEGER, amount INTEGER, kind TEXT, reason TEXT, ts INTEGER)")
        g = NS(id=4)
        m = NS(id=1, joined_at=now() - dt.timedelta(days=40))
        db.q("INSERT INTO activity(guild_id, user_id, voice_seconds) VALUES(4,1,?)", 101 * 3600)
        db.q("INSERT INTO events(message_id, guild_id, data, created_at) VALUES(1,4,?,?)",
             json.dumps({"checked": True, "yes": [1], "came": [1]}), now().isoformat())
        db.q("INSERT INTO treasury_tx(guild_id,user_id,actor_id,amount,kind,ts) VALUES(4,1,2,100,'due',0)")
        st = guild_stats(NS(db=db), g, [m])[1]
        self.assertEqual(st["xp"], 606 + 50 + 30 + 80)
        self.assertEqual(st["level"], level_of(766))
        self.assertIn("voice100", earned(st))
        self.assertIn("month", earned(st))
        db.q("INSERT INTO warns(guild_id, user_id, reason, active) VALUES(4,1,'x',1)")
        db.q("INSERT INTO shop_orders(guild_id, user_id, item_id, price, status) VALUES(4,1,1,500,'done')")
        st = guild_stats(NS(db=db), g, [m])[1]
        self.assertEqual(st["xp"], 666)
        self.assertEqual(st["points"], 166)

    def test_level_curve(self):
        self.assertEqual([level_of(x) for x in (0, 99, 100, 399, 400, 2500)], [0, 0, 1, 1, 2, 5])


if __name__ == "__main__":
    unittest.main()
