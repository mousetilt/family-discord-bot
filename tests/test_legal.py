# -*- coding: utf-8 -*-
"""ИИ-юрист: поиск по базе (сокращения, номер статьи), самопроверка цитат, разница законов, шаблоны, дела семьи."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB  # noqa: E402
from core.kb import KnowledgeBase  # noqa: E402
from core.legal import (citations, diff_articles, diff_line, expand_query, pick_template, title_fits,  # noqa: E402
                        verify)

UK = "\n".join(f"Статья {n}. {t}\nНаказывается лишением свободы на срок до {n} лет. " + "Текст статьи. " * 40
               for n, t in [(12, "Убийство"), (30, "Кража"), (31, "Грабёж"), (45, "Взятка")])
UPK = "Статья 99. Сроки следствия\nСледствие длится не более 3 дней. " + "Процесс. " * 60
RULES = "2.1. Запрещено DM (убийство без причины).\n2.2. Запрещено RK.\n" + "Пункт правил. " * 60
DOCS = [{"title": "Уголовный кодекс РО", "content": UK}, {"title": "Уголовно-процессуальный кодекс РО", "content": UPK},
        {"title": "Основные правила проекта", "content": RULES}]


def kb_with_docs():
    db = DB(":memory:")
    kb = KnowledgeBase(db)
    for d in DOCS:
        kb.add(5, d["title"], None, "laws", d["content"], 0)
    return db, kb


class Citations(unittest.TestCase):
    def test_extract(self):
        cs = citations("По ч. 2 ст. 12 УК РО и п. 2.1 правил; статья 99 УПК.")
        self.assertEqual([(c["kind"], c["num"], c["code"]) for c in cs],
                         [("article", "12", "ук"), ("point", "2.1", ""), ("article", "99", "упк")])

    def test_verify(self):
        text, missing = verify("Грозит ст. 12 УК, ещё ст. 99 УК и п. 2.1, а также п. 7.7 и статья 77.", DOCS)
        self.assertEqual(missing, ["ст. 99 УК", "п. 7.7", "статья 77"])  # 99 есть только в УПК
        self.assertIn("ст. 99 УК ⚠️", text)
        self.assertIn("ст. 12 УК,", text)  # настоящая — без пометки
        self.assertEqual(verify("Ст. 30 УК — кража.", DOCS)[1], [])

    def test_yo_kept(self):
        text, missing = verify("Ещё раз: статьёй 77 УК.", DOCS)
        self.assertTrue(text.startswith("Ещё раз"))
        self.assertIn("⚠️", text)

    def test_codes(self):
        self.assertTrue(title_fits("ук", "Уголовный кодекс РО"))
        self.assertFalse(title_fits("ук", "Уголовно-процессуальный кодекс РО"))
        self.assertTrue(title_fits("", "что угодно"))


class Search(unittest.TestCase):
    def test_article_number_first(self):
        _, kb = kb_with_docs()
        hits = kb.search(5, "что за ст. 31 УК", k=3)
        self.assertIn("Статья 31", hits[0]["text"])

    def test_abbreviation(self):
        self.assertIn("deathmatch", expand_query("ДМ на въезде"))
        _, kb = kb_with_docs()
        hits = kb.search(5, "за ДМ что будет", k=2)
        self.assertIn("2.1", hits[0]["text"])


class Diff(unittest.TestCase):
    def test_articles(self):
        old = "Статья 1. А\nтекст\nСтатья 2. Б\nтекст\nСтатья 3. В\nтекст"
        new = "Статья 1. А\nтекст\nСтатья 2. Б\nновый текст\nСтатья 4. Г\nтекст"
        self.assertEqual(diff_articles(old, new), (["4"], ["3"], ["2"]))
        self.assertEqual(diff_line("УК", old, new), "«УК» — изменены: 2; добавлены: 4; удалены: 3")
        self.assertIn("без смены номеров", diff_line("X", "просто текст", "просто текст2"))


class Templates(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(pick_template("составь апелляцию"), "appeal")
        self.assertEqual(pick_template("отзыв на иск Мавроди"), "response")
        self.assertEqual(pick_template("обращение в прокуратуру"), "prosecutor")
        self.assertEqual(pick_template("иск в Верховный суд"), "supreme")
        self.assertIsNone(pick_template("что такое ДМ"))

    def test_forum_form_preferred(self):
        from cogs.lawyer import Lawyer
        db, kb = kb_with_docs()
        law = Lawyer.__new__(Lawyer)
        law.bot = NS(kb=kb)
        self.assertIn("ШАБЛОН «Апелляционная жалоба»", law.template_text(5, "составь апелляцию"))
        kb.add(5, "Форма: Апелляционный суд — Форма подачи жалобы", None, "forms", "1. Ник:\n2. Номер дела:\n", 0)
        self.assertIn("ОБРАЗЕЦ ФОРМЫ С ФОРУМА", law.template_text(5, "составь апелляцию"))


class FakeAI:
    on = True
    keys = {}

    def __init__(self, enough=True):
        self.enough = enough
        self.systems = []

    async def ask_json(self, system, prompt, **kw):
        return {"enough": self.enough, "questions": ["Когда это было?", "Есть ли видео?"]}

    async def ask(self, system, prompt, **kw):
        self.systems.append(system)
        return "По ст. 12 УК грозит срок, а по ст. 77 УК — штраф."


class Answer(unittest.TestCase):
    def setUp(self):
        from cogs.forumwatch import SCHEMA, ForumWatch
        from cogs.lawyer import Lawyer
        import discord
        self.db, kb = kb_with_docs()
        self.db.c.executescript(SCHEMA)
        self.db.q("INSERT INTO forum_people(guild_id, name, static_id) VALUES(5, 'Ваня Ульянов', '64524')")
        self.db.q("INSERT INTO forum_cases(guild_id, thread_id, title, url, kind, role, defendant, applicant, summary, "
                  "case_no, status, body, updated_ts) VALUES(5, 47339, 'Иск №99', 'u', 'иск', 'defendant', "
                  "'Ваня Ульянов', 'Иса Мавроди', 'Иск о взятке', '99', 'pending', 'Истец просит наказать', 1)")

        class G(discord.Object):
            def get_member(self, i):
                return None
        self.g = G(5)
        self.ai = FakeAI()
        bot = NS(db=self.db, kb=kb, ai_for=lambda g: self.ai, get_guild=lambda i: self.g, in_family=lambda m: True,
                 feature=lambda g, k: True, brand=lambda g, t: t, is_home=lambda g: False,
                 family=lambda g: {"name": "Ульяновы"}, role=lambda g, k: None, family_members=lambda g: [])
        fw = ForumWatch.__new__(ForumWatch)
        fw.bot = bot
        bot.get_cog = lambda name: fw if name == "ForumWatch" else None
        self.law = Lawyer.__new__(Lawyer)
        self.law.bot = bot

    def run_(self, *a, **kw):
        return asyncio.run(self.law.answer_full(5, *a, **kw))

    def test_verify_and_defense(self):
        res = self.run_("что делать по делу Вани?", "q", member=object())
        self.assertEqual(res["mode"], "defense")
        self.assertIn("ПОДРОБНО ПО ДЕЛУ «Иск №99»", self.ai.systems[-1])
        self.assertIn("ст. 77 УК ⚠️", res["text"])
        self.assertIn("Не нашёл в базе: ст. 77 УК", res["text"])
        self.assertNotIn("ст. 12 УК ⚠️", res["text"])

    def test_no_family_context_for_outsiders(self):
        self.run_("что делать по делу Вани?", "q")  # member=None — не член семьи
        self.assertNotIn("ДЕЛА ПРОТИВ СЕМЬИ", self.ai.systems[-1])

    def test_clarify(self):
        self.ai.enough = False
        res = self.run_("меня убили на въезде, что делать", "case", member=object(), clarify=True)
        self.assertEqual(res["questions"], ["Когда это было?", "Есть ли видео?"])
        self.assertEqual(res["text"], "")
        res = self.run_("меня убили\n\nУточнения:\n— вчера", "case", member=object(), clarify=True)
        self.assertEqual(res["questions"], [])  # второй заход — уже не спрашиваем
        self.ai.enough = False
        self.assertEqual(self.run_("что такое ДМ", "q", clarify=True)["questions"], [])  # обычный вопрос


if __name__ == "__main__":
    unittest.main()
