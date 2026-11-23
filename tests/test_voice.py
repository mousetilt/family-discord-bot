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


if __name__ == "__main__":
    unittest.main()
