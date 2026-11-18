# -*- coding: utf-8 -*-
"""Фаза E: смысловой поиск базы — векторы Gemini в kb_vec (по guild_id), доиндексация, чистка, фолбэк на слова."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import kb as kb_mod  # noqa: E402
from core.db import DB  # noqa: E402


class FakeAI:
    def __init__(self, gemini=True, claude=False):
        self.keys = {"gemini": "g"} if gemini else {}
        self.is_claude = claude
        self.embedded = 0

    async def embed(self, texts, query=False):
        self.embedded += len(texts)
        # «смысл»: вектор по наличию слов — достаточно для порядка в тесте
        return [[1.0 if "кража" in t.lower() else 0.1, 1.0 if "штраф" in t.lower() else 0.1, 0.5] for t in texts]


def make():
    db = DB(":memory:")
    kb = kb_mod.KB(db) if hasattr(kb_mod, "KB") else next(v for v in vars(kb_mod).values()
                                                            if isinstance(v, type) and hasattr(v, "context_smart"))(db)
    return db, kb


class Semantic(unittest.TestCase):
    def test_warm_prune_and_guild(self):
        db, kb = make()
        kb.add(7, "УК РО", None, "laws", "Статья 1. Кража. " * 300, 1)
        ai = FakeAI()
        n = asyncio.run(kb.warm(7, ai))
        self.assertGreater(n, 0)
        self.assertEqual(kb.missing(7), 0)                         # вся база проиндексирована
        rows = db.all("SELECT guild_id FROM kb_vec")
        self.assertTrue(rows and all(r["guild_id"] in (7, 0) for r in rows))
        doc = db.one("SELECT id FROM kb_docs WHERE guild_id=7")["id"]
        gone = {c["hash"] for c in kb._build(7)[0] if c["doc_id"] == doc}
        kb.delete(7, doc)
        kb.add(7, "КоАП", None, "laws", "Статья 5. Штраф за парковку.", 1)
        asyncio.run(kb.warm(7, ai))
        hashes = {r["hash"] for r in db.all("SELECT hash FROM kb_vec WHERE guild_id=7")}
        alive = {c["hash"] for c in kb._build(7)[0] if c["gid"] == 7}
        self.assertEqual(hashes, alive)                            # векторы удалённого документа вычищены
        self.assertFalse(hashes & gone)

    def test_no_gemini_falls_back(self):
        _, kb = make()
        filler = " ".join(f"Статья {i}. Порядок парковки транспорта номер {i} в городе." for i in range(1500))
        kb.add(7, "КоАП РО", None, "laws", filler, 1)
        kb.add(7, "УК РО", None, "laws", "Статья 158. Кража имущества наказывается лишением свободы.", 1)
        ai = FakeAI(gemini=False, claude=True)
        self.assertEqual(asyncio.run(kb.warm(7, ai)), 0)
        text, src, _ = asyncio.run(kb.context_smart(7, "кража имущества", ai))
        self.assertIn("Кража", text)                               # поиск по словам работает
        self.assertEqual(ai.embedded, 0)

    def test_claude_gets_more_context(self):
        _, kb = make()
        for i in range(40):
            kb.add(7, f"Закон {i}", None, "laws", (f"Статья {i}. Кража и штраф. " * 120), 1)
        free, claude = FakeAI(), FakeAI(claude=True)
        asyncio.run(kb.warm(7, free))
        t_free, _, _ = asyncio.run(kb.context_smart(7, "кража", free))
        t_claude, _, _ = asyncio.run(kb.context_smart(7, "кража", claude))
        self.assertLessEqual(len(t_free), kb_mod.SMART_CHARS)
        self.assertGreater(len(t_claude), len(t_free))
        self.assertLessEqual(len(t_claude), kb_mod.SMART_CHARS_CLAUDE)


if __name__ == "__main__":
    unittest.main()
