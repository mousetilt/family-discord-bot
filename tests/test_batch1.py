# -*- coding: utf-8 -*-
"""Пачка доработок 2026.11.18.6: ник по шаблону из анкеты, память хоста, JSON-режим ИИ."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from cogs.nickcheck import build_nick  # noqa: E402
from cogs.maintenance import mem_usage, mem_line  # noqa: E402


class BuildNick(unittest.TestCase):
    def test_templates(self):
        self.assertEqual(build_nick("Имя Фамилия [ID]", "Ivan_Ivanov", "123456"), "Ivan Ivanov [123456]")
        self.assertEqual(build_nick("Имя_Фамилия", "Пётр Сидоров"), "Пётр_Сидоров")
        self.assertEqual(build_nick("Имя Фамилия | ID", "Ivan Ivanov", 77), "Ivan Ivanov | 77")

    def test_missing_parts(self):
        self.assertIsNone(build_nick("Имя Фамилия [ID]", "Ivan", "1"))        # нет фамилии
        self.assertIsNone(build_nick("Имя Фамилия [ID]", "Ivan Ivanov", ""))  # нет Static
        self.assertIsNone(build_nick("Имя Фамилия", "", None))
        self.assertIsNone(build_nick("Имя Фамилия [ID]", "Ivan Ivanov", "12ab"))


class Memory(unittest.TestCase):
    def test_no_crash(self):
        used, limit = mem_usage()  # на Windows нет cgroup и /proc — просто None
        self.assertTrue(used is None or used > 0)
        mem_line()


class JsonMode(unittest.TestCase):
    def test_payloads(self):
        import asyncio
        from core.ai import AI
        from core.db import DB
        ai = AI(DB(":memory:"), keys={"gemini": "x", "groq": "y"})
        sent = []

        async def fake_post(url, payload, headers):
            sent.append(payload)
            if "groq" in url:
                return {"choices": [{"message": {"content": "{}"}}]}
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
        ai._post = fake_post
        asyncio.run(ai._gemini("m", "s", "p", 10, json_mode=True, images=[("image/png", b"\x89PNG")]))
        asyncio.run(ai._groq("m", "s", "p", 10, json_mode=True))
        self.assertEqual(sent[0]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("inline_data", sent[0]["contents"][0]["parts"][0])
        self.assertEqual(sent[1]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
