# -*- coding: utf-8 -*-
"""Фаза D: документы юриста — карточка дела из forum_cases, известные поля, объём на Claude, самопроверка."""
import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core.db import DB  # noqa: E402


class FakeKB:
    async def context_smart(self, gid, q, ai):
        return "=== УК РО ===\nСтатья 12. Превышение полномочий.", [("УК РО", "")], False

    def docs(self, gid, cats=None):
        return [{"title": "Уголовный кодекс (УК РО)", "content": "Статья 12. Превышение полномочий.", "url": "",
                 "category": "laws"}]


class AI:
    def __init__(self, claude):
        self.on, self.is_claude, self.vision, self.calls = True, claude, claude, []

    async def ask(self, system, prompt, **kw):
        self.calls.append((system, kw))
        return "ИСКОВОЕ ЗАЯВЛЕНИЕ\nОтветчик: Ваня Ульянов, static 64524\nНарушена ст. 12 УК РО и ст. 999 УК РО."

    async def ask_json(self, *a, **kw):
        return {"enough": True}


def make(claude=True):
    from cogs.forumwatch import SCHEMA, ForumWatch
    from cogs.lawyer import Lawyer
    db = DB(":memory:")
    db.c.executescript(SCHEMA)
    db.q("INSERT INTO forum_cases(guild_id, thread_id, title, url, kind, role, defendant, applicant, case_no, status, "
         "summary, body) VALUES(7, 47492, 'Иск №58 в Верховный суд', 'https://f/47492', 'иск', 'defendant', "
         "'Ваня Ульянов (64524)', 'Иса Мавроди', '58', 'pending', 'Иск против Вани', 'Прошу наказать Ваню')")
    db.q("INSERT INTO applications(guild_id, user_id, nick, static_id, status) VALUES(7, 5, 'Юлиан Ульянов', '104589', "
         "'accepted')")
    guild = types.SimpleNamespace(id=7, name="g")
    ai = AI(claude)
    bot = types.SimpleNamespace(db=db, kb=FakeKB(), get_guild=lambda gid: guild, ai_for=lambda g: ai,
                                family=lambda g: {"name": "Ульяновы", "server": "Арбатский"},
                                brand=lambda g, t: t, in_family=lambda m: True, feature=lambda g, k: False)
    fw = ForumWatch.__new__(ForumWatch)
    fw.bot = bot
    bot.get_cog = lambda name: fw if name == "ForumWatch" else None
    law = Lawyer.__new__(Lawyer)
    law.bot = bot
    return law, ai, db


class Docs(unittest.TestCase):
    def test_case_and_fields_in_prompt(self):
        law, ai, db = make(claude=True)
        me = types.SimpleNamespace(id=5)
        cid = db.one("SELECT id FROM forum_cases")["id"]
        res = asyncio.run(law.answer_full(7, "отзыв на иск", "doc", member=me, template="response", clarify=True,
                                          case_id=cid))
        system, kw = ai.calls[-1]
        self.assertIn("Иск №58", system)                       # карточка дела
        self.assertIn("Прошу наказать Ваню", system)           # текст заявления
        self.assertIn("Юлиан Ульянов, static 104589", system)  # известные поля из анкеты
        self.assertIn("[заполнить]", system)
        self.assertEqual(kw["max_tokens"], 3500)               # Claude пишет документ целиком
        self.assertTrue(kw["smart"])
        self.assertIn("999", res["text"])                      # выдуманная статья помечена
        self.assertIn("⚠️", res["text"])

    def test_free_keeps_old_size(self):
        law, ai, _ = make(claude=False)
        asyncio.run(law.answer_full(7, "составь жалобу на Петю за превышение", "doc", member=None))
        self.assertEqual(ai.calls[-1][1]["max_tokens"], 1600)

    def test_case_card_other_guild(self):
        law, _, db = make()
        fw = law.bot.get_cog("ForumWatch")
        cid = db.one("SELECT id FROM forum_cases")["id"]
        self.assertEqual(fw.case_card(types.SimpleNamespace(id=8), cid), "")   # чужое дело не отдаём
        self.assertEqual(fw.case_card(types.SimpleNamespace(id=7), "мусор"), "")


if __name__ == "__main__":
    unittest.main()
