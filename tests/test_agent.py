# -*- coding: utf-8 -*-
"""Фаза A: агентные цепочки Швейцара — помощники core.agent и цикл Assistant.agent (без Discord и сети)."""
import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import agent  # noqa: E402


class Helpers(unittest.TestCase):
    def test_multi_step(self):
        self.assertTrue(agent.multi_step("собери дела Ульяновых и сделай черновик иска по Ване"))
        self.assertTrue(agent.multi_step("проверь меня по форуму, потом составь жалобу"))
        self.assertTrue(agent.multi_step("нужна позиция защиты по делу Пети"))
        self.assertFalse(agent.multi_step("проверь меня по форуму"))
        self.assertFalse(agent.multi_step("есть на меня жалобы?"))

    def test_split_merge(self):
        acts = [{"type": "forum_check"}, {"type": "warn", "user": 1}, {"type": "people"}]
        reads, muts = agent.split(acts, {"forum_check", "people"})
        self.assertEqual([a["type"] for a in reads], ["forum_check", "people"])
        self.assertEqual(muts, [{"type": "warn", "user": 1}])
        merged = agent.merge(muts, [{"user": 1, "type": "warn"}, {"type": "mute", "user": 2}])
        self.assertEqual([a["type"] for a in merged], ["warn", "mute"])  # одинаковый выговор — один раз

    def test_transcript_budget(self):
        tr = agent.Transcript(budget=3000, step_chars=2000)
        for i in range(5):
            tr.add(i + 1, [{"type": "people"}], "x" * 2000)
        out = tr.render(last=True)
        self.assertLess(len(out), 3000 + 1500)
        self.assertIn("ШАГ 5", out)          # свежий шаг целиком
        self.assertIn("сокращено", out)      # старые ужаты
        self.assertIn("ПОСЛЕДНИЙ ШАГ", out)

    def test_progress(self):
        self.assertEqual(agent.progress_text(["проверяю форум", "проверяю форум", "готовлю черновик документа"]),
                         "⏳ проверяю форум → готовлю черновик документа…")
        self.assertTrue(agent.progress_text(["x"], finished=True).startswith("✅"))
        self.assertEqual(agent.progress_text([]), "")


class FakeAI:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    async def ask_json(self, system, prompt, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Loop(unittest.TestCase):
    """Справки выполняются сразу, изменяющие — только копятся (в финальный план, на подтверждение)."""

    def make(self):
        from cogs.assistant import Assistant
        bot = types.SimpleNamespace(has=lambda m, roles: True)
        cog = Assistant.__new__(Assistant)
        Assistant.__init__(cog, bot)
        cog.ran = []

        async def run(ctx, plan, extra=None):
            cog.ran += [a["type"] for a in plan]
            return ["​" + ("📄 ИСКОВОЕ ЗАЯВЛЕНИЕ…" if a["type"] == "law_doc" else "Дело №12: Ваня — ответчик")
                    for a in plan]
        cog.run = run
        cog.may = lambda member, t: True
        return cog

    def test_chain(self):
        cog = self.make()
        sent = []

        class Msg:
            async def edit(self, content):
                sent.append(("edit", content))

        async def send(**kw):
            sent.append(("send", kw.get("content")))
            return Msg()
        ai = FakeAI([
            {"continue": True, "actions": [{"type": "law_doc", "kind": "иск", "facts": "дело №12"},
                                           {"type": "warn", "user": 5, "reason": "x"}]},
            {"continue": False, "reply": "Готово: черновик по делу №12 ниже.", "actions": []},
        ])
        first = {"continue": True, "actions": [{"type": "forum_check", "scope": "family"}]}
        ctx = types.SimpleNamespace(author=object(), voice=False)
        out = asyncio.run(cog.agent(ctx, ai, "sys", "prompt", first, send))
        self.assertEqual(cog.ran, ["forum_check", "law_doc"])        # выговор НЕ выполнен
        self.assertEqual([a["type"] for a in out["actions"]], ["warn"])  # а ушёл в финальный план
        self.assertTrue(out["_agent"])
        self.assertIn("Дело №12", ai.prompts[0])                   # результат шага вернулся модели
        self.assertTrue(any(c and c.startswith("📄") for k, c in sent))  # документ показан целиком
        self.assertTrue(any(k == "edit" and c.startswith("✅") for k, c in sent))  # ход цепочки

    def test_step_limit(self):
        cog = self.make()

        async def send(**kw):
            return None
        loop = {"continue": True, "actions": [{"type": "people"}]}
        ai = FakeAI([dict(loop) for _ in range(10)])
        ctx = types.SimpleNamespace(author=object(), voice=True)
        asyncio.run(cog.agent(ctx, ai, "s", "p", dict(loop), send))
        self.assertLessEqual(len(ai.prompts), agent.MAX_STEPS - 1)
        self.assertIn("ПОСЛЕДНИЙ ШАГ", ai.prompts[-1])


if __name__ == "__main__":
    unittest.main()
