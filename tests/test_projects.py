# -*- coding: utf-8 -*-
"""Игровые проекты: Россия Онлайн / Majestic RP / другой — профиль, тексты, промпты, форум."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import family, projects  # noqa: E402
from core.ai import AI  # noqa: E402
from core.db import DB  # noqa: E402


class Projects(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_detect(self):
        for t, k in (("Россия Онлайн", "ro"), ("РО", "ro"), ("Majestic RP", "majestic"), ("маджестик", "majestic"),
                     ("GTA5RP", "other"), ("", projects.DEFAULT)):
            self.assertEqual(projects.detect(t), k, t)

    def test_default_is_ro(self):
        self.assertEqual(projects.key_of(self.db, 5), "ro")
        self.assertTrue(projects.forum_ok(self.db, 5))  # сервер по умолчанию — Арбатский
        self.assertEqual(family.profile(self.db, 5)["project"], "Россия Онлайн")

    def test_other_ro_server_no_forum(self):
        self.db.gset(5, "family:server", "Тверской")
        self.assertFalse(projects.forum_ok(self.db, 5))
        self.assertIn("Арбатский", projects.why_no_forum(self.db, 5))

    def test_majestic(self):
        self.db.gset(5, "family:project", "Majestic RP")
        self.db.gset(5, "family:server", "New York")
        self.assertFalse(projects.forum_ok(self.db, 5))
        self.assertIn("анти-DDoS", projects.why_no_forum(self.db, 5))
        text = family.brand(self.db, 5, "Семья · Россия Онлайн · игра «Россия Онлайн»")
        self.assertNotIn("Россия Онлайн", text)
        self.assertIn("Majestic RP", text)

    def test_custom_project(self):
        self.db.gset(5, "family:project", "Radmir")
        p = projects.info(self.db, 5)
        self.assertEqual((p["key"], p["name"]), ("other", "Radmir"))
        self.assertEqual(projects.rebrand("RP-проект «Россия Онлайн»", p), "RP-проект «Radmir»")

    def test_prompt_rebranded(self):
        self.db.gset(5, "family:project", "Majestic")
        ai = AI(self.db, keys={"gemini": "g"}, scope=5)
        ai.project = projects.info(self.db, 5)
        seen = {}

        async def gemini(model, system, prompt, max_tokens, **k):
            seen["system"] = system
            return "ок"
        with mock.patch.object(ai, "_gemini", gemini):
            asyncio.run(ai.ask("Ты юрист семьи (игра «Россия Онлайн»).", "вопрос"))
        self.assertIn("Majestic RP", seen["system"])
        self.assertNotIn("Россия Онлайн", seen["system"])

    def test_profile_modal_fits(self):
        self.assertLessEqual(len(family.FIELDS), 5)  # окно Discord — максимум 5 полей


class Portable(unittest.TestCase):
    """Экспорт/импорт настроек: без секретов и ID сервера, файл чужого формата не принимается."""

    def test_roundtrip(self):
        from core import portable
        a, b = DB(":memory:"), DB(":memory:")
        for k, v in (("mod:forum", "0"), ("set:warn_limit", "4"), ("text:rules", "Правила"), ("family:name", "Петровы"),
                     ("family:project", "Majestic RP"), ("ai:gemini", "SECRET"), ("forum:pass", "x"),
                     ("role:leader", "123"), ("ch:news", "456"), ("panel:rules", "789"), ("alias:5", "Петя")):
            a.gset(1, k, v)
        payload = portable.export(a, 1, "Петровы")
        raw = portable.dumps(payload)
        self.assertNotIn(b"SECRET", raw)
        data, src = portable.parse(raw)
        self.assertEqual(src, "Петровы")
        self.assertEqual(set(data), {"mod:forum", "set:warn_limit", "text:rules", "family:name", "family:project",
                                     "alias:5"})
        self.assertEqual(portable.apply(b, 2, data), 6)
        self.assertEqual(b.gget(2, "set:warn_limit"), "4")
        self.assertIsNone(b.gget(2, "role:leader"))

    def test_rejects(self):
        from core import portable
        for raw in (b"not json", b'{"a": 1}', b'{"format": "family-bot-settings", "version": 99, "settings": {}}'):
            with self.assertRaises(ValueError):
                portable.parse(raw)
        data, _ = portable.parse(b'{"format": "family-bot-settings", "version": 1, '
                                 b'"settings": {"ai:claude": "k", "dev:gh_token": "t", "set:x": "1"}}')
        self.assertEqual(data, {"set:x": "1"})


if __name__ == "__main__":
    unittest.main()
