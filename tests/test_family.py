# -*- coding: utf-8 -*-
"""Настройки семьи (числа и тексты из /настройки) и офлайн-загрузка всех модулей бота."""
import asyncio
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core import config, family  # noqa: E402
from core.db import DB  # noqa: E402


class FamilySettings(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_numbers_default_and_clamp(self):
        self.assertEqual(family.number(self.db, 5, "warn_limit"), 3)
        self.db.gset(5, "set:warn_limit", 99)
        self.assertEqual(family.number(self.db, 5, "warn_limit"), 10)  # потолок
        self.db.gset(5, "set:warn_limit", "мусор")
        self.assertEqual(family.number(self.db, 5, "warn_limit"), 3)
        self.assertEqual(family.number(self.db, 6, "warn_limit"), 3)  # у другой семьи — своё

    def test_probation_legacy_key(self):
        self.db.gset(5, "probation:days", 10)
        self.assertEqual(family.number(self.db, 5, "probation_days"), 10)

    def test_texts(self):
        self.assertEqual(family.checklist(self.db, 5), list(config.MENTOR_CHECKLIST))
        self.db.gset(5, "text:checklist", "• Первое\n\n- Второе\n")
        self.assertEqual(family.checklist(self.db, 5), ["Первое", "Второе"])
        self.db.gset(5, "text:situational", "Вопрос один | подсказка\nВопрос два")
        self.assertEqual(family.situational(self.db, 5)[1], ("Вопрос два", "Ответь честно и развёрнуто"))
        self.db.gset(5, "text:situational", "один вопрос")  # меньше двух — стандартные
        self.assertEqual(family.situational(self.db, 5), list(config.SITUATIONAL))

    def test_modules(self):
        self.assertTrue(family.feature(self.db, 5, "treasury:fines"))
        self.assertFalse(family.feature(self.db, 5, "hr:auto"))  # по умолчанию выключено
        family.set_module(self.db, 5, "treasury", False)
        self.assertFalse(family.feature(self.db, 5, "treasury:fines"))


class LoadAllCogs(unittest.TestCase):
    def test_load(self):
        """Все модули из config.EXTENSIONS загружаются на офлайн-боте (как перед каждым деплоем)."""
        from core.botcore import FamilyBot
        logging.disable(logging.CRITICAL)

        async def run():
            bot = FamilyBot(presences=False)
            failed = []
            for ext in config.EXTENSIONS:
                try:
                    await bot.load_extension(ext)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{ext}: {e!r}")
            names = [c.name for c in bot.tree.get_commands()]
            await bot.close()
            return failed, names

        failed, names = asyncio.run(run())
        logging.disable(logging.NOTSET)
        self.assertEqual(failed, [])
        self.assertEqual(len(names), len(set(names)), "дубли слэш-команд")
        self.assertLessEqual(len(names), 100, "лимит Discord — 100 команд")


if __name__ == "__main__":
    unittest.main()
