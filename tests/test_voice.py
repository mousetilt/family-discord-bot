import types
import unittest

from cogs import jarvis as j


class Wake(unittest.TestCase):
    def fw(self, text, first=False):
        return j.Jarvis.fuzzy_wake(types.SimpleNamespace(), text, first=first)

    def test_crooked_name(self):
        for t in ("Швицар, включи музыку", "Шварцар включи", "Швейцарю скажи"):
            self.assertIsNotNone(self.fw(t, first=True), t)

    def test_not_name(self):
        for t in ("Швейцария красивая", "швабра где", "шварц пришёл", "привет всем"):
            self.assertFalse(self.fw(t), t)
            self.assertIsNone(j.WAKE.match(t), t)

    def test_novisit(self):
        for t in ("не заходи ко мне", "больше не приходи", "не беспокой меня", "не надо заходить"):
            self.assertTrue(j.NOVISIT.search(t), t)
        self.assertFalse(j.NOVISIT.search("заходи почаще"))

    def test_junk(self):
        self.assertTrue(j.JUNK.search("Субтитры сделал DimaTorzok"))
        self.assertFalse(j.JUNK.search("Швейцар, включи музыку"))


class Catchup(unittest.TestCase):
    """Пересказ при входе в войс: только тем, кто включил, и только после долгого отсутствия."""

    def run_event(self, persona, member, before, after):
        import asyncio
        started = []
        persona.bot.spawn = lambda coro, name=None: (started.append(coro), coro.close())
        asyncio.run(persona.on_voice_state_update(member, before, after))
        return started

    def test_trigger(self):
        import time
        from cogs import persona as pm
        from core.db import DB
        db = DB(":memory:")
        bot = types.SimpleNamespace(db=db, feature=lambda g, k: True)
        p = pm.Persona.__new__(pm.Persona)
        p.bot = bot
        g = types.SimpleNamespace(id=7, guild_id=7)
        m = types.SimpleNamespace(id=5, bot=False, guild=g)
        out = types.SimpleNamespace(channel=None)
        inside = types.SimpleNamespace(channel=types.SimpleNamespace(id=9))
        self.assertEqual(self.run_event(p, m, out, inside), [])  # не включал — молчим
        db.gset(7, "persona:catchup:5", 1)
        db.gset(7, "persona:catchup_seen:5", int(time.time()) - 3600)
        self.assertEqual(self.run_event(p, m, out, inside), [])  # не было всего час
        db.gset(7, "persona:catchup_seen:5", int(time.time()) - 20 * 3600)
        self.assertEqual(len(self.run_event(p, m, out, inside)), 1)  # 20 часов — рассказываем
        self.assertEqual(self.run_event(p, m, inside, out), [])  # вышел — только метка


if __name__ == "__main__":
    unittest.main()
