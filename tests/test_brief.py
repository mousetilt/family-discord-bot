# -*- coding: utf-8 -*-
"""Фаза B: сборщик сквозного контекста (core.brief) — только реальные данные, в бюджете, по фокусу."""
import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import agent, brief  # noqa: E402
from core.db import DB  # noqa: E402


class FakeKB:
    def __init__(self):
        self.asked = []

    async def context_smart(self, gid, q, ai):
        self.asked.append((gid, q))
        return "=== УК РО ===\nСтатья 12. Превышение полномочий…", [("УК РО", "")], False

    def docs(self, gid):
        return []


def make_bot(n_members=3):
    from cogs.forumwatch import SCHEMA
    db = DB(":memory:")
    db.c.executescript(SCHEMA)
    db.q("INSERT INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, applicant, case_no, "
         "status, summary, body, decision, outcome) VALUES(7, 1, NULL, 'Иск №58 в Верховный суд', 'https://f/1', 'иск', "
         "'defendant', 'Ваня Ульянов (64524)', 'Иса Мавроди', '58', 'pending', 'Иск против Вани', 'текст', NULL, NULL)")
    db.q("INSERT INTO forum_cases(guild_id, thread_id, title, kind, role, defendant, status) "
         "VALUES(8, 2, 'Чужая семья', 'иск', 'defendant', 'Петров', 'pending')")
    members = [types.SimpleNamespace(id=100 + i, display_name=f"Человек{i}", bot=False) for i in range(n_members)]
    db.q("INSERT INTO applications(guild_id, user_id, nick, static_id, status) VALUES(7, 100, 'Ваня Ульянов', '64524', "
         "'accepted')")
    return types.SimpleNamespace(
        db=db, kb=FakeKB(), get_cog=lambda name: None, family_members=lambda g: members,
        rank_index=lambda m: 1, rank_name=lambda m: "Член семьи", num=lambda g, k: 3,
        family=lambda g: {"name": "Ульяновы", "server": "Арбатский"})


class Collect(unittest.TestCase):
    def test_only_own_guild_and_real_fields(self):
        bot = make_bot()
        g = types.SimpleNamespace(id=7)
        ai = types.SimpleNamespace(is_claude=True)
        text, src = asyncio.run(brief.collect(bot, g, "что в целом по нашим на форуме?", ai))
        self.assertIn("Иск №58", text)
        self.assertIn("Ваня Ульянов [64524]", text)        # ник и статик из анкеты
        self.assertIn("Статья 12", text)
        self.assertNotIn("Петров", text)                   # дела другой семьи не попадают
        self.assertEqual(src, [("УК РО", "")])

    def test_budget(self):
        bot = make_bot(n_members=2000)
        g = types.SimpleNamespace(id=7)
        ai = types.SimpleNamespace(is_claude=False)
        text, _ = asyncio.run(brief.collect(bot, g, "кто у нас в семье", ai))
        self.assertLess(len(text), brief.BUDGET_FREE + 600)
        self.assertIn("не влезло", text)

    def test_focus(self):
        self.assertEqual(brief.focus_of("позиция защиты по всем активным делам"), "forum")
        self.assertEqual(brief.focus_of("что говорит закон о превышении"), "law")
        self.assertEqual(brief.focus_of("кто у нас с выговорами"), "family")
        self.assertEqual(brief.focus_of("как дела", "law"), "law")

    def test_answer_uses_smart(self):
        bot = make_bot()
        seen = {}

        class AI:
            on, is_claude = True, True

            async def ask(self, system, prompt, **kw):
                seen.update(system=system, **kw)
                return "Итого: одно открытое дело — иск №58 против Вани."
        bot.ai_for = lambda g: AI()
        out = asyncio.run(brief.answer(bot, types.SimpleNamespace(id=7), "что в целом по нашим на форуме"))
        self.assertTrue(seen["smart"])
        self.assertIn("ДАННЫЕ", seen["system"])
        self.assertIn("№58", out)

    def test_agent_hands_analysis_to_model(self):
        self.assertTrue(agent.multi_step("что в целом по нашим на форуме"))
        self.assertTrue(agent.multi_step("позиция защиты по всем активным делам с учётом практики"))


if __name__ == "__main__":
    unittest.main()
