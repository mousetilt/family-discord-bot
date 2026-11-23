# -*- coding: utf-8 -*-
"""Квиз недели: разбор ответа ИИ, прохождение кнопками, один заход, итоги."""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from cogs import quiz as qm  # noqa: E402
from core.db import DB  # noqa: E402

QS = [{"q": f"Вопрос {i}", "options": ["верно", "нет1", "нет2", "нет3"], "why": "ст. 1"} for i in range(3)]


class Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kw):
        self.sent.append(("send", content))

    async def edit_message(self, content=None, **kw):
        self.sent.append(("edit", content))


class QuizFlow(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.c.executescript(qm.SCHEMA)
        self.cog = qm.Quiz.__new__(qm.Quiz)
        self.cog.bot = types.SimpleNamespace(db=self.db)
        self.qid = self.db.q("INSERT INTO quizzes(guild_id, created, closes, questions) VALUES(7,?,?,?)",
                             int(time.time()), int(time.time()) + 3600, json.dumps(QS)).lastrowid

    def inter(self):
        return types.SimpleNamespace(guild=types.SimpleNamespace(id=7), user=types.SimpleNamespace(id=5),
                                     response=Resp())

    def test_clean(self):
        bad = [{"q": "", "options": ["a", "b", "c", "d"]}, {"q": "x", "options": ["a", "a", "b", "c"]}, "мусор",
               {"q": "ок", "options": ["a", "b", "c", "d"]}]
        self.assertEqual([q["q"] for q in qm.clean_questions(bad)], ["ок"])
        self.assertEqual(qm.clean_questions("не список"), [])

    def test_run(self):
        async def go():
            i = self.inter()
            await self.cog.begin(i, self.qid)
            self.assertEqual(i.response.sent[0][0], "send")
            run = {"qid": self.qid, "qs": QS, "i": 0, "score": 0, "wrong": [], "orders": [[0, 1, 2, 3]] * 3, "user": 5}
            for idx in (0, 2, 0):  # верно, мимо, верно
                await self.cog.answer(i, run, idx)
            last = i.response.sent[-1][1]
            self.assertIn("2/3", last)
            self.assertIn("Разбор ошибок", last)
            again = self.inter()
            await self.cog.begin(again, self.qid)  # второй заход — только результат
            self.assertIn("уже прошёл", again.response.sent[0][1])
        asyncio.run(go())

    def test_score_line(self):
        rows = [{"user_id": 1, "score": 3, "total": 3}, {"user_id": 2, "score": 0, "total": 3}]
        text = qm.score_line(rows, None)
        self.assertIn("Участвовали: **2**", text)
        self.assertIn("🥇 <@1>", text)
        self.assertNotIn("<@2>", text)


if __name__ == "__main__":
    unittest.main()
