# -*- coding: utf-8 -*-
"""Обращение к Швейцару: имя с опечатками и в падежах, «бот,», исключения; продолжение разговора по смыслу."""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import addressing  # noqa: E402


class Name(unittest.TestCase):
    def test_called(self):
        for t in ("Швейцар, включи музыку", "швецар включи музыку", "Швейцр, перенеси нас", "швейцер, ты тут?",
                  "швайцар, глянь", "щвейцар привет", "Швейцарь, ау", "скажи швейцару, пусть включит",
                  "спроси у швейцара", "швейцарик, привет", "включи музыку, швейцар", "shveitsar, privet",
                  "бот, включи музыку", "ШВЕЙЦАР!!!"):
            self.assertTrue(addressing.called(t), t)

    def test_not_called(self):
        for t in ("летим в Швейцарию", "швейцарский сыр", "швейцарские часы", "купил швейную машинку",
                  "бот тупит", "у меня ботинки", "привет всем", "шваброй помыли", ""):
            self.assertFalse(addressing.called(t), t)

    def test_strip(self):
        self.assertEqual(addressing.strip_call("Швецар, включи музыку"), "включи музыку")
        self.assertEqual(addressing.strip_call("бот: громче"), "громче")
        self.assertEqual(addressing.strip_call("включи музыку, швейцар"), "включи музыку, швейцар")
        self.assertEqual(addressing.strip_call("привет всем"), "привет всем")


class Followup(unittest.TestCase):
    def make(self, to_bot=True, ai_on=True):
        from cogs.assistant import Assistant
        calls = []

        class AI:
            on = ai_on

            async def ask_json(self, system, prompt, **kw):
                calls.append((prompt, kw))
                return {"to_bot": to_bot}
        bot = types.SimpleNamespace(feature=lambda g, k: True, has=lambda m, r: True, ch=lambda g, k: None,
                                    ai_for=lambda g: AI())
        cog = Assistant.__new__(Assistant)
        Assistant.__init__(cog, bot)
        cog.talk[(1, 2)] = {"t": time.monotonic(), "ask": False, "turns": [("включи музыку", "Включил.")]}
        return cog, calls

    def msg(self, text, **kw):
        return types.SimpleNamespace(content=text, guild=types.SimpleNamespace(id=7),
                                     channel=types.SimpleNamespace(id=1), author=types.SimpleNamespace(id=2, bot=False),
                                     id=50, reference=kw.get("reference"), mentions=kw.get("mentions", []),
                                     mention_everyone=False)

    def test_yes(self):
        cog, calls = self.make(True)
        self.assertTrue(asyncio.run(cog.followup(self.msg("а теперь погромче"))))
        self.assertTrue(calls[0][1]["light"])
        self.assertIn("включи музыку", calls[0][0])

    def test_model_says_no(self):
        cog, _ = self.make(False)
        self.assertFalse(asyncio.run(cog.followup(self.msg("Петя, го на выезд"))))

    def test_skips_without_call(self):
        cog, calls = self.make(True)
        other = types.SimpleNamespace(bot=False)
        self.assertFalse(asyncio.run(cog.followup(self.msg("ты где?", mentions=[other]))))   # говорит с другим
        self.assertFalse(asyncio.run(cog.followup(self.msg("да", reference=object()))))      # ответ другому
        cog.talk[(1, 2)]["t"] -= addressing.FOLLOW_SEC + 1                                   # разговор остыл
        self.assertFalse(asyncio.run(cog.followup(self.msg("а теперь погромче"))))
        self.assertEqual(calls, [])                                                          # ИИ даже не звали

    def test_no_ai(self):
        cog, _ = self.make(True, ai_on=False)
        self.assertFalse(asyncio.run(cog.followup(self.msg("а теперь погромче"))))


if __name__ == "__main__":
    unittest.main()
