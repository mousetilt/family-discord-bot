# -*- coding: utf-8 -*-
"""Разборщики человеческих фраз: суммы, время, шаблон ника, номера машин, «подождите N минут» от ИИ."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai import parse_json, retry_after  # noqa: E402
from core.parse import MSK, find_amount, fmt_amount, parse_amount, parse_when  # noqa: E402


class Amounts(unittest.TestCase):
    def test_suffixes(self):
        for raw, want in [("50к", 50_000), ("1.5кк", 1_500_000), ("1,2 ляма", 1_200_000), ("2м", 2_000_000),
                          ("1 200 000", 1_200_000), ("1.500.000", 1_500_000), ("200000", 200_000), ("3ккк", 3 * 10 ** 9),
                          ("100к вирт", 100_000), (75, 75)]:
            self.assertEqual(parse_amount(raw), want, raw)

    def test_rejects(self):
        for raw in ["", "абв", "0", "-5", "1.5", "50.5", "5кг", True, None, "10" + "0" * 13]:
            self.assertIsNone(parse_amount(raw), raw)

    def test_find_in_text(self):
        self.assertEqual(find_amount("1.5кк, торг уместен"), 1_500_000)
        self.assertEqual(find_amount("BMW за 2 500 000"), 2_500_000)
        self.assertIsNone(find_amount("договорная"))

    def test_fmt(self):
        self.assertEqual(fmt_amount(1500000), "1 500 000 вирт")


class When(unittest.TestCase):
    base = dt.datetime(2026, 11, 17, 21, 0, tzinfo=MSK)  # вторник, 21:00 по Москве

    def at(self, text, **kw):
        t = parse_when(text, self.base, **kw)
        return t.astimezone(MSK).strftime("%d.%m %H:%M") if t else None

    def test_phrases(self):
        cases = {"22:30": "17.11 22:30", "20:00": "18.11 20:00", "20.30": "18.11 20:30", "в 9": "18.11 09:00",
                 "завтра 18:30": "18.11 18:30", "послезавтра в 7": "19.11 07:00", "25.11 19:00": "25.11 19:00",
                 "через 40 минут": "17.11 21:40", "через 2 часа": "17.11 23:00", "через 3 дня": "20.11 21:00"}
        for text, want in cases.items():
            self.assertEqual(self.at(text), want, text)

    def test_limits(self):
        self.assertIsNone(self.at("абв"))
        self.assertIsNone(self.at("25:00"))
        self.assertIsNone(self.at("через 100 дней"))
        self.assertIsNone(self.at("сегодня 20:58"))  # уже прошло
        self.assertEqual(self.at("сегодня 20:58", grace_min=5), "17.11 20:58")  # сбор только что начался
        self.assertIsNone(self.at("через 40 дней", max_days=30))

    def test_returns_utc(self):
        t = parse_when("22:30", self.base)
        self.assertEqual(t.utcoffset(), dt.timedelta(0))


class Misc(unittest.TestCase):
    def test_nick_template(self):
        from cogs.nickcheck import compile_template, example
        rx = compile_template("Имя Фамилия [ID]")
        self.assertTrue(rx.match("Ivan Ivanov [123456]"))
        self.assertTrue(rx.match("Иван_Иванов[1]"))
        self.assertFalse(rx.match("Ivan [12]"))
        self.assertIsNone(compile_template("просто текст"))
        self.assertEqual(example("Имя_Фамилия"), "Иван_Иванов")

    def test_plate(self):
        from cogs.garage import norm_plate
        self.assertEqual(norm_plate("a123bc 77"), "А123ВС77")
        self.assertIsNone(norm_plate("  "))

    def test_retry_after(self):
        self.assertEqual(retry_after("Please try again in 7m12.5s."), 432)
        self.assertEqual(retry_after("try again in 1h"), 3600)
        self.assertEqual(retry_after("no hint"), 60)

    def test_parse_json(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('ответ: {"reply": "ок", "actions": []} конец'), {"reply": "ок", "actions": []})
        self.assertIsNone(parse_json("нет json"))


if __name__ == "__main__":
    unittest.main()
