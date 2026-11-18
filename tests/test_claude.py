# -*- coding: utf-8 -*-
"""Claude как провайдер ИИ: выбор основного, уровни моделей, откат на бесплатные, картинки, ключи семей."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from core import ai as ai_mod, config  # noqa: E402
from core.ai import AI, AIUnavailable  # noqa: E402
from core.db import DB  # noqa: E402

FAST, SMART = config.CLAUDE_MODEL_FAST, config.CLAUDE_MODEL_SMART


def run(coro):
    return asyncio.run(coro)


class Selection(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_default_stays_free(self):
        ai = AI(self.db, keys={"gemini": "g", "claude": "c"}, scope=5)
        self.assertEqual(ai.provider, "gemini")
        self.assertFalse(ai.is_claude)
        self.assertNotIn("claude", ai.backups)  # платный запасным сам не становится

    def test_primary_claude(self):
        ai = AI(self.db, keys={"gemini": "g", "groq": "q", "claude": "c"}, scope=5, primary="claude")
        self.assertTrue(ai.is_claude)
        self.assertEqual(ai.backups, ["gemini", "groq"])
        self.assertTrue(ai.vision)

    def test_primary_without_key_falls_back(self):
        ai = AI(self.db, keys={"groq": "q"}, scope=5, primary="claude")
        self.assertEqual(ai.provider, "groq")
        self.assertFalse(ai.vision)

    def test_only_claude_key(self):
        ai = AI(self.db, keys={"claude": "c"}, scope=5)
        self.assertEqual(ai.provider, "claude")
        self.assertEqual(ai.backups, [])

    def test_no_keys_off(self):
        ai = AI(self.db, keys={}, scope=5, primary="claude")
        self.assertFalse(ai.on)
        with self.assertRaises(AIUnavailable):
            run(ai.ask("s", "p"))

    def test_legacy_name(self):
        self.assertTrue(AI(self.db, keys={"claude": "c"}, scope=5, primary="anthropic").is_claude)


class Tiers(unittest.TestCase):
    def setUp(self):
        self.ai = AI(DB(":memory:"), keys={"gemini": "g", "claude": "c"}, scope=5, primary="claude")

    def test_models(self):
        self.assertEqual(self.ai.tier_models("claude"), [FAST])
        self.assertEqual(self.ai.tier_models("claude", light=True), [FAST])
        self.assertEqual(self.ai.tier_models("claude", smart=True), [SMART, FAST])
        self.assertEqual(self.ai.tier_models("claude", smart=True, light=True), [SMART, FAST])  # smart главнее
        self.assertEqual(self.ai.active_model(), FAST)
        self.assertEqual(self.ai.active_model(smart=True), SMART)

    def test_chain_ends_with_free(self):
        chain = self.ai.chain(smart=True)
        self.assertEqual(chain[0], ("claude", SMART))
        self.assertEqual({p for p, _ in chain[2:]}, {"gemini"})

    def test_free_unchanged_by_smart(self):
        free = AI(DB(":memory:"), keys={"groq": "q", "gemini": "g"}, scope=5, primary="groq")
        self.assertEqual(free.chain(), free.chain(smart=True))


class Fallback(unittest.TestCase):
    def setUp(self):
        self.ai = AI(DB(":memory:"), keys={"gemini": "g", "claude": "c"}, scope=5, primary="claude")

    def test_rate_limit_goes_to_gemini(self):
        async def claude(*a, **k):
            raise ai_mod._RateLimited("rate limit")

        async def gemini(*a, **k):
            return "ответ gemini"
        with mock.patch.object(self.ai, "_claude", claude), mock.patch.object(self.ai, "_gemini", gemini):
            self.assertEqual(run(self.ai.ask("s", "p", smart=True)), "ответ gemini")
        self.assertTrue(all(self.ai.cool.get(("claude", m), 0) > 0 for m in (SMART, FAST)))

    def test_bad_key_goes_to_gemini(self):
        async def claude(*a, **k):
            raise AIUnavailable("Ключ ИИ не подходит")

        async def gemini(*a, **k):
            return "ок"
        with mock.patch.object(self.ai, "_claude", claude), mock.patch.object(self.ai, "_gemini", gemini):
            self.assertEqual(run(self.ai.ask("s", "p")), "ок")

    def test_claude_gets_tier_and_cache(self):
        seen = {}

        async def claude(model, system, prompt, max_tokens, **k):
            seen.update(model=model, **k)
            return '{"a": 1}'
        with mock.patch.object(self.ai, "_claude", claude):
            self.assertEqual(run(self.ai.ask_json("s", "p", smart=True, cache=True)), {"a": 1})
        self.assertEqual(seen["model"], SMART)
        self.assertTrue(seen["smart"] and seen["cache"])

    def test_images_only_vision(self):
        free = AI(DB(":memory:"), keys={"groq": "q"}, scope=5)
        with self.assertRaises(AIUnavailable):
            run(free.ask("s", "p", images=[("image/png", b"x")]))


class Payload(unittest.TestCase):
    """Запрос к Anthropic: заголовки, картинки, effort, разбор ответа и отказа — без сети."""

    def setUp(self):
        self.ai = AI(DB(":memory:"), keys={"claude": "sk-test"}, scope=5)

    def test_request_and_text(self):
        sent = {}

        async def post(url, payload, headers, timeout=30):
            sent.update(url=url, payload=payload, headers=headers)
            return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "привет"}],
                    "stop_reason": "end_turn"}
        with mock.patch.object(self.ai, "_post", post):
            out = run(self.ai._claude(SMART, "sys", "q", 100, smart=True, cache=True,
                                      images=[("image/png", b"\x89PNG")]))
        self.assertEqual(out, "привет")
        self.assertEqual(sent["url"], ai_mod.CLAUDE_URL)
        self.assertEqual(sent["headers"]["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", sent["headers"])
        p = sent["payload"]
        self.assertEqual(p["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(p["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(p["output_config"]["effort"], "medium")
        self.assertNotIn("temperature", p)

    def test_refusal(self):
        async def post(*a, **k):
            return {"content": [], "stop_reason": "refusal"}
        with mock.patch.object(self.ai, "_post", post):
            with self.assertRaises(AIUnavailable):
                run(self.ai._claude(FAST, "s", "p", 50))


class FamilyKeys(unittest.TestCase):
    """bot.ai_for: ключ Claude семьи и выбор основного; ключи хоста чужим семьям не достаются."""

    def test_ai_for(self):
        from core.botcore import FamilyBot

        async def make():
            return FamilyBot(presences=False)
        bot = run(make())
        with mock.patch.object(config, "GUILD_ID", 1), \
                mock.patch.dict(bot.ai.keys, {"claude": "host-key", "gemini": "host-g"}, clear=True):
            self.assertFalse(bot.ai_for(2).on)  # чужая семья без своих ключей — ИИ выключен
            bot.db.gset(2, "ai:claude", "fam-key")
            bot.db.gset(2, "ai:primary", "claude")
            fam = bot.ai_for(2)
            self.assertTrue(fam.is_claude)
            self.assertEqual(fam.keys["claude"], "fam-key")
            self.assertNotIn("gemini", fam.keys)
            bot.db.gset(1, "ai:primary", "claude")  # домашний: ключ хоста подхватывается
            home = bot.ai_for(1)
            self.assertTrue(home.is_claude)
            self.assertEqual(home.keys["claude"], "host-key")
            self.assertEqual(home.backups, ["gemini"])


if __name__ == "__main__":
    unittest.main()
