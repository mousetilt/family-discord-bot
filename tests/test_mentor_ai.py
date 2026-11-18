# -*- coding: utf-8 -*-
"""Фаза F: адаптивный ИИ-наставник (Claude) и инсайты по реальным цифрам."""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import insights  # noqa: E402
from core.db import DB  # noqa: E402

ITEMS = ["Изучить правила", "Сходить на сбор", "Сдать экзамен"]


class FakeKB:
    async def context_smart(self, gid, q, ai):
        return "Правила семьи: на сборы ходить обязательно.", [], False


class AI:
    on, is_claude = True, True

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def ask_json(self, system, prompt, **kw):
        self.calls.append((system, kw))
        return self.answer

    async def ask(self, system, prompt, **kw):
        self.calls.append((system, kw))
        return self.answer


def make_mentor(answer):
    from cogs.mentorship import Mentorship
    db = DB(":memory:")
    now = dt.datetime.now(dt.timezone.utc)
    db.q("INSERT INTO mentorships(guild_id, newbie_id, mentor_id, thread_id, checklist, started_at, due_at, status) "
         "VALUES(7, 10, 20, 99, ?, ?, ?, 'active')", json.dumps([True, False, False]), now.isoformat(),
         (now + dt.timedelta(days=5)).isoformat())
    ai = AI(answer)
    bot = types.SimpleNamespace(db=db, kb=FakeKB(), checklist=lambda g: ITEMS, feature=lambda g, k: False,
                                family=lambda g: {"name": "Ульяновы"}, ai_for=lambda g: ai)
    m = Mentorship.__new__(Mentorship)
    m.bot = bot
    return m, db, ai


class Mentor(unittest.TestCase):
    def test_adaptive(self):
        m, db, ai = make_mentor({"answer": "Сборы — обязательно, приходи в пятницу.", "next_step": "Сдай экзамен",
                                 "done": [1, 2, 3, 9, "x"], "flag": "хочет уйти"})
        row = db.one("SELECT * FROM mentorships")
        sent = []

        class Ch:
            async def send(self, text, **kw):
                sent.append(("ch", text))

        class Msg:
            content, guild, channel = "я сходил на сбор, что дальше?", types.SimpleNamespace(id=7), Ch()

            async def reply(self, text, view=None, **kw):
                sent.append(("reply", text, view))
        asyncio.run(m.adaptive_answer(Msg(), row, ai))
        system, kw = ai.calls[0]
        self.assertTrue(kw["smart"])
        self.assertIn("[x] Изучить правила", system)            # реальный статус новичка
        self.assertIn("[ ] Сходить на сбор", system)
        self.assertIn("на сборы ходить", system)                  # база знаний
        reply = sent[0]
        self.assertIn("Следующий шаг", reply[1])
        ids = [it.custom_id for it in reply[2].children]
        self.assertEqual(ids, ["msug:1:1", "msug:1:2"])            # п.1 уже отмечен, 9 и «x» отброшены
        self.assertEqual(json.loads(db.one("SELECT checklist FROM mentorships")["checklist"]),
                         [True, False, False])                    # ИИ сам ничего не отметил
        self.assertTrue(any(k == "ch" and "<@20>" in t for k, t, *_ in sent))  # важное — наставнику

    def test_status_text(self):
        m, db, _ = make_mentor({})
        txt = m.status_text(types.SimpleNamespace(id=7), db.one("SELECT * FROM mentorships"))
        self.assertIn("до решения", txt)
        self.assertIn("выговоров 0", txt)


class Insights(unittest.TestCase):
    def make_bot(self):
        db = DB(":memory:")
        members = [types.SimpleNamespace(id=i, display_name=f"Игрок{i}") for i in (1, 2, 3)]
        db.q("INSERT INTO activity(guild_id, user_id, week_voice, week_messages) VALUES(7, 1, 7200, 10)")
        db.q("INSERT INTO activity(guild_id, user_id, week_voice, week_messages) VALUES(8, 2, 99999, 99)")  # чужая
        db.q("INSERT INTO applications(guild_id, user_id, status, created_at) VALUES(7, 5, 'accepted', ?)",
             dt.datetime.now(dt.timezone.utc).isoformat())
        guild = types.SimpleNamespace(id=7, get_member=lambda uid: next((m for m in members if m.id == uid), None))
        bot = types.SimpleNamespace(db=db, family_members=lambda g: members, rank_name=lambda m: "Член семьи",
                                    get_cog=lambda n: None, family=lambda g: {"name": "Ульяновы"})
        return bot, guild

    def test_numbers(self):
        bot, guild = self.make_bot()
        d = insights.collect(bot, guild, 7)
        self.assertEqual(d["members"], 3)
        self.assertEqual(d["week_voice_h"], 2.0)          # чужая семья не считается
        self.assertEqual(d["silent_week"], 2)
        self.assertEqual(d["applications"], {"accepted": 1})
        text = insights.render(d)
        self.assertIn("Игрок1 — 2.0 ч", text)
        self.assertIn("приняты — 1", text)

    def test_narrate_smart_and_fallback(self):
        bot, guild = self.make_bot()
        ai = AI("Неделя бодрая: Игрок1 тащит войс.")
        bot.ai_for = lambda g: ai
        out = asyncio.run(insights.narrate(bot, guild))
        self.assertTrue(ai.calls[0][1]["smart"])
        self.assertIn("Игрок1 тащит", out)
        self.assertIn("По цифрам бота", out)
        bot.ai_for = lambda g: types.SimpleNamespace(on=False)
        self.assertIn("Цифры семьи", asyncio.run(insights.narrate(bot, guild)))


if __name__ == "__main__":
    unittest.main()
