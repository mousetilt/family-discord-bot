# -*- coding: utf-8 -*-
"""Пачка 2026.11.18.8: смысловой поиск по базе знаний, падежи имён, «на грани неактива»."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core.botcore import now  # noqa: E402
from core.db import DB  # noqa: E402
from core.kb import KnowledgeBase  # noqa: E402
from core.names import forms  # noqa: E402


class FakeAI:
    """Вектор = «есть ли в тексте слово»: таран/полиция и тема еды разводятся по разным осям."""
    keys = {"gemini": "x"}
    AXES = [("таран", "полиц", "сотрудник", "нападен"), ("еда", "кухн", "повар"), ("машин", "авто")]

    def __init__(self):
        self.calls = 0

    async def embed(self, texts, query=False):
        self.calls += 1
        return [[1.0 + 9 * any(w in t.lower() for w in ax) for ax in self.AXES] for t in texts]


class Semantic(unittest.TestCase):
    def test_finds_by_meaning_and_caches(self):
        db = DB(":memory:")
        kb = KnowledgeBase(db)
        filler = "\n".join(f"Пункт {i}. Общие положения о порядке, пункт номер {i}." for i in range(400))
        kb.add(7, "Кодекс", None, "laws", filler + "\nСтатья 99. Нападение на сотрудника полиции при исполнении.", 1)
        kb.add(7, "Кухня", None, "family", "Повар готовит еду на кухне.\n" * 40, 1)
        ai = FakeAI()
        hits = asyncio.run(kb.semantic(7, "можно ли таранить полицию", ai, k=3))
        self.assertTrue(hits and "полиц" in hits[0]["text"].lower())
        calls = ai.calls
        asyncio.run(kb.semantic(7, "таран", ai, k=3))
        self.assertEqual(ai.calls, calls + 1)  # куски уже проиндексированы — только вектор вопроса
        text, _, full = asyncio.run(kb.context_smart(7, "таран полиции", ai))
        self.assertFalse(full)
        self.assertIn("Статья 99", text)

    def test_no_key_fallback(self):
        kb = KnowledgeBase(DB(":memory:"))
        self.assertIsNone(asyncio.run(kb.semantic(7, "x", NS(keys={}))))


class Names(unittest.TestCase):
    def test_cases(self):
        self.assertIn("петя", forms("Пете"))
        self.assertIn("димон", forms("Димону"))
        self.assertEqual(forms("ab"), frozenset({"ab"}))


class Risk(unittest.TestCase):
    def test_fading_member(self):
        from core.people import at_risk
        db = DB(":memory:")
        g = NS(id=3)
        bot = NS(db=db, role=lambda guild, k: None)
        m = NS(id=1, voice=None, roles=[], joined_at=now() - dt.timedelta(days=60))
        db.q("INSERT INTO activity(guild_id, user_id, last_seen, voice_seconds, week_voice) VALUES(3,1,?,?,0)",
             (now() - dt.timedelta(days=4)).isoformat(), 40 * 3600)
        self.assertIn("не заходил 4", at_risk(bot, g, m) or "")
        db.q("UPDATE activity SET last_seen=? WHERE user_id=1", (now() - dt.timedelta(days=9)).isoformat())
        self.assertIsNone(at_risk(bot, g, m))  # уже неактив — это другая мерка


if __name__ == "__main__":
    unittest.main()
