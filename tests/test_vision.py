# -*- coding: utf-8 -*-
"""Фаза C: зрение — разбор ответа ИИ, предложение завести дело (через подтверждение), ручное дело в forum_cases."""
import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import vision  # noqa: E402
from core.db import DB  # noqa: E402

RAW = {"kind": "complaint", "summary": "Жалоба в прокуратуру на сотрудника ФСО",
       "complaint": {"kind": "прокуратура", "against": "Ваня Ульянов", "static": "static 64-524", "filed_by": "Иса Мавроди",
                     "essence": "превышение полномочий", "section": "Прокуратура", "case_no": "220",
                     "url": "https://forum.russia.online/threads/zhaloba.47770/", "position": None, "date": "null"},
       "document": None, "payment": None, "text": "Жалоба №220"}


class Normalize(unittest.TestCase):
    def test_complaint(self):
        seen = vision.normalize(RAW)
        c = seen["complaint"]
        self.assertEqual(c["static"], "64524")       # только цифры
        self.assertIsNone(c["date"])                 # «null» строкой — не данные
        act = vision.case_action(seen)
        self.assertEqual(act["type"], "case_add")
        self.assertEqual(act["defendant"], "Ваня Ульянов")
        self.assertIn("№220", act["title"])
        self.assertIn("РАСПОЗНАНО", vision.to_prompt(seen))

    def test_garbage(self):
        self.assertIsNone(vision.normalize("не json"))
        seen = vision.normalize({"kind": "complaint", "complaint": {"against": None}})
        self.assertEqual(seen["kind"], "other")      # не на кого — заводить нечего
        self.assertIsNone(vision.case_action(seen))
        self.assertEqual(vision.normalize({"kind": "что-то"})["kind"], "other")
        bad_url = vision.normalize({**RAW, "complaint": {**RAW["complaint"], "url": "javascript:x"}})
        self.assertIsNone(bad_url["complaint"]["url"])

    def test_has_images(self):
        msg = types.SimpleNamespace(attachments=[types.SimpleNamespace(content_type="image/png; x", size=10)])
        self.assertTrue(vision.has_images(msg))
        self.assertFalse(vision.has_images(types.SimpleNamespace(attachments=[])))
        self.assertFalse(vision.has_images(types.SimpleNamespace()))

    def test_extract_passes_images(self):
        seen_kw = {}

        class AI:
            async def ask_json(self, system, prompt, **kw):
                seen_kw.update(kw)
                return RAW
        out = asyncio.run(vision.extract(AI(), [("image/png", b"x")], "вот скрин"))
        self.assertEqual(seen_kw["images"], [("image/png", b"x")])
        self.assertEqual(out["complaint"]["case_no"], "220")


class ManualCase(unittest.TestCase):
    def make(self):
        from cogs.forumwatch import SCHEMA, ForumWatch
        db = DB(":memory:")
        db.c.executescript(SCHEMA)
        bot = types.SimpleNamespace(db=db, ch=lambda g, k: None, feature=lambda g, k: True)
        fw = ForumWatch.__new__(ForumWatch)
        fw.bot = bot
        fw.people = lambda g: [{"name": "Ваня Ульянов", "user_id": 5, "head": 0, "static_id": "64524"}]
        return fw, db

    def test_with_url_and_without(self):
        fw, db = self.make()
        g = types.SimpleNamespace(id=7, name="g")
        me = types.SimpleNamespace(display_name="Глава")
        act = vision.case_action(vision.normalize(RAW))
        self.assertIn("отслеживать", asyncio.run(fw.add_manual_case(g, act, me)))
        self.assertIn("уже есть", asyncio.run(fw.add_manual_case(g, act, me)))
        c = db.one("SELECT * FROM forum_cases WHERE guild_id=7 AND thread_id=47770")
        self.assertEqual(c["role"], "defendant")
        no_url = {**act, "url": None, "case_no": "300"}
        asyncio.run(fw.add_manual_case(g, no_url, me))
        self.assertIn("уже есть", asyncio.run(fw.add_manual_case(g, no_url, me)))  # тот же номер — не дубль
        asyncio.run(fw.add_manual_case(g, {**no_url, "case_no": None}, me))
        neg = db.all("SELECT thread_id FROM forum_cases WHERE guild_id=7 AND thread_id<0 ORDER BY thread_id DESC")
        self.assertEqual([r["thread_id"] for r in neg], [-1, -2])  # без ссылки — свои отрицательные id
        self.assertIn("❌", asyncio.run(fw.add_manual_case(g, {"defendant": ""}, me)))


if __name__ == "__main__":
    unittest.main()
