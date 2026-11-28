import unittest

from cogs import applications as ap


class ApplyFlow(unittest.TestCase):
    """Заявка не теряется: окна разбираются по постоянному custom_id (долго писал / бот перезапустился)."""

    def test_modal_values_rows_and_labels(self):
        data = {"custom_id": ap.FORM_ID, "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": "nick", "value": "Ivan_Ivanov"}]},
            {"type": 18, "component": {"type": 4, "custom_id": "static", "value": "123"}},
        ]}
        self.assertEqual(ap.modal_values(data), {"nick": "Ivan_Ivanov", "static": "123"})

    def test_situational_answers_by_index(self):
        pool = [("Первый", "п1"), ("Второй", "п2"), ("Третий", "")]
        got = ap.situational_answers({"q2": "ответ три", "q0": "ответ один", "x": "мусор"}, pool)
        self.assertEqual(sorted(a["q"] for a in got), ["Первый. п1", "Третий"])
        self.assertEqual(ap.situational_answers({"q9": "ответ"}, pool)[0]["q"], "Вопрос")  # вопросы поменяли

    def test_step3_pending(self):
        base = {"status": "exam", "situational": None, "exam_total": 10}
        self.assertTrue(ap.step3_pending(base, True))  # тест сдан — остался шаг 3
        self.assertFalse(ap.step3_pending({**base, "exam_total": 0}, True))  # тест не пройден
        self.assertTrue(ap.step3_pending({**base, "exam_total": 0}, False))  # экзамен выключен
        self.assertFalse(ap.step3_pending({**base, "status": "new"}, True))
        self.assertFalse(ap.step3_pending(None, True))

    def test_modals_have_fixed_ids(self):
        self.assertEqual(ap.ApplicationModal().custom_id, ap.FORM_ID)
        m = ap.SituationalModal(42, [("А", "a"), ("Б", "b"), ("В", "c")])
        self.assertEqual(m.custom_id, "appsit:42")
        self.assertEqual(len(m.children), 2)
        self.assertTrue(all(c.custom_id.startswith("q") for c in m.children))
        self.assertEqual(ap.Step3Button(7).item.custom_id, "appstep3:7")


class Listener(unittest.IsolatedAsyncioTestCase):
    async def test_routes_form_and_step3(self):
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, MagicMock

        import discord
        cog = object.__new__(ap.Applications)
        cog.bot = MagicMock()
        cog.bot.situational.return_value = [("Вопрос", "подсказка")]
        cog.anketa_submitted, cog.submit = AsyncMock(), AsyncMock()

        def inter(cid, comps):
            return NS(type=discord.InteractionType.modal_submit, guild=object(), user="u",
                      data={"custom_id": cid, "components": comps})
        await cog.on_interaction(inter(ap.FORM_ID, [{"custom_id": "nick", "value": "A_B"}]))
        cog.anketa_submitted.assert_awaited_once()
        self.assertEqual(cog.anketa_submitted.call_args[0][1], {"nick": "A_B"})
        await cog.on_interaction(inter("appsit:5", [{"custom_id": "q0", "value": "длинный ответ"}]))
        self.assertEqual(cog.submit.call_args[0][1:], (5, [{"q": "Вопрос. подсказка", "a": "длинный ответ"}]))
        await cog.on_interaction(inter("other", []))  # чужие окна не трогаем
        self.assertEqual(cog.submit.await_count, 1)
