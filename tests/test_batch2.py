# -*- coding: utf-8 -*-
"""Пачка 2026.11.18.7: выгрузка CSV по серверу, ремонт → казна (разбор суммы)."""
import csv
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core.db import DB  # noqa: E402
from cogs.export import rows_for, to_csv, ts_text  # noqa: E402


class ExportCSV(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.c.executescript("CREATE TABLE IF NOT EXISTS treasury_tx(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                                "guild_id INTEGER, user_id INTEGER, actor_id INTEGER, amount INTEGER, kind TEXT, "
                                "reason TEXT, ts INTEGER)")
        self.g = NS(id=5, get_member=lambda uid: NS(display_name="Петя") if uid == 1 else None)

    def test_only_this_guild(self):
        self.db.q("INSERT INTO treasury_tx(guild_id,user_id,actor_id,amount,kind,reason,ts) VALUES(5,1,2,100,'in','x',0)")
        self.db.q("INSERT INTO treasury_tx(guild_id,user_id,actor_id,amount,kind,reason,ts) VALUES(6,1,2,999,'in','чужое',0)")
        self.db.q("INSERT INTO treasury_tx(guild_id,user_id,actor_id,amount,kind,reason,ts) VALUES(5,NULL,2,30,'out','y',0)")
        head, rows = rows_for(self.db, self.g, "treasury")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][4], "Петя")
        self.assertEqual(rows[1][3], -30)  # расход — с минусом
        text = to_csv(head, rows).decode("utf-8-sig")
        parsed = list(csv.reader(io.StringIO(text), delimiter=";"))
        self.assertEqual(parsed[0][0], "№")
        self.assertNotIn("чужое", text)

    def test_ts(self):
        self.assertEqual(ts_text(0), "1970-01-01 00:00")
        self.assertEqual(ts_text(""), "")
        self.assertEqual(ts_text("мусор"), "мусор")


if __name__ == "__main__":
    unittest.main()
