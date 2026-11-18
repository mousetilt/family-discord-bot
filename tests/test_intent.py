# -*- coding: utf-8 -*-
"""Автономность Швейцара: «проверь меня по форуму» — распознавание без ИИ, кто такой «я» (молча из анкеты/ника),
кто кого может пробивать; шифрование общего аккаунта форума и его сессии."""
import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_TOKEN", "test-token-for-secret")

import discord  # noqa: E402

from core.db import DB  # noqa: E402
from core.forum import ForumClient, forum_intent  # noqa: E402


class Intent(unittest.TestCase):
    def test_self(self):
        for t in ("Швейцар, проверь меня по форуму", "пробей меня", "есть на меня жалобы?", "есть ли на меня иски",
                  "жалобы на меня есть?", "глянь по форуму меня", "проверь меня на форуме"):
            self.assertEqual(forum_intent(t), {"target_name": None, "fresh": False, "scope": "self", "status": "all"},
                             t)

    def test_scope_and_status(self):
        def sc(t):
            r = forum_intent(t)
            return r and (r["scope"], r["status"])
        self.assertEqual(sc("на кого-то из семьи есть не рассмотренные жалобы?"), ("family", "open"))
        self.assertEqual(sc("есть жалобы на наших?"), ("family", "all"))
        self.assertEqual(sc("чем кончились жалобы на нас"), ("family", "reviewed"))
        self.assertEqual(sc("проверь всю семью по форуму"), ("family", "all"))
        self.assertEqual(sc("у кого-нибудь из наших есть дела на форуме?"), ("family", "all"))
        self.assertEqual(sc("на меня есть открытые?"), ("self", "open"))
        self.assertEqual(sc("есть ли на меня не рассмотренные жалобы"), ("self", "open"))
        self.assertEqual(sc("есть ли на меня рассмотренные жалобы"), ("self", "reviewed"))
        self.assertEqual(sc("пробей Петю"), ("member", "all"))
        self.assertIsNone(forum_intent("у нас есть жалобы на ДМ?"))

    def test_other_and_fresh(self):
        self.assertEqual(forum_intent("пробей Петю")["target_name"], "петю")
        r = forum_intent("проверь Ваню Ульянова на форуме посвежее")
        self.assertEqual((r["target_name"], r["fresh"]), ("ваню ульянова", True))
        self.assertTrue(forum_intent("проверь меня по форуму прямо сейчас")["fresh"])

    def test_not_forum(self):
        for t in ("есть ли на форуме жалобы на ДМ?", "что за ст. 12 УК", "какие правила подачи жалобы",
                  "пробей мне трек", "включи музыку", "Швейцар, как дела"):
            self.assertIsNone(forum_intent(t), t)


class Secret(unittest.TestCase):
    def test_roundtrip(self):
        from core.secret import decrypt, encrypt
        e = encrypt("пароль-123")
        self.assertTrue(e.startswith("enc1:"))
        self.assertNotIn("пароль", e)
        self.assertNotEqual(e, encrypt("пароль-123"))  # случайный nonce
        self.assertEqual(decrypt(e), "пароль-123")
        self.assertEqual(decrypt("старый-открытый"), "старый-открытый")  # значение из прошлой версии
        bad = e[:-4] + ("AAAA" if not e.endswith("AAAA") else "BBBB")
        self.assertIsNone(decrypt(bad))  # порча/подмена

    def test_other_key(self):
        from core.secret import decrypt, encrypt
        e = encrypt("x")
        old = os.environ.get("FORUM_SECRET")
        os.environ["FORUM_SECRET"] = "другой ключ"
        try:
            self.assertIsNone(decrypt(e))
        finally:
            if old is None:
                os.environ.pop("FORUM_SECRET")
            else:
                os.environ["FORUM_SECRET"] = old

    def test_client_cookies(self):
        async def run():
            c = ForumClient("bot", "pw", cookies={"xf_user": "123,abc", "чужое": "x"})
            c._open()
            got = c.export_cookies()
            await c.close()
            return c.logged_in, got
        logged, got = asyncio.run(run())
        self.assertEqual(got, {"xf_user": "123,abc"})
        guest = ForumClient()
        self.assertIsNone(guest.password)


class G(discord.Object):
    owner_id = 1

    def __init__(self, i, members):
        super().__init__(i)
        self.members = {m.id: m for m in members}

    def get_member(self, i):
        return self.members.get(i)


def make_fw(db, members, lead_ids=()):
    from cogs.forumwatch import SCHEMA, ForumWatch
    db.c.executescript(SCHEMA)
    bot = NS(db=db, is_home=lambda g: False, family=lambda g: {"name": "Ульяновы"}, role=lambda g, k: None,
             family_members=lambda g: members, feature=lambda g, k: True,
             has=lambda m, keys: getattr(m, "id", 0) in lead_ids)
    fw = ForumWatch.__new__(ForumWatch)
    fw.bot = bot
    fw.net_lock = asyncio.Lock()
    return fw, bot


class PersonCheck(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.vanya = NS(id=11, display_name="Ваня", bot=False, roles=[])
        self.petya = NS(id=12, display_name="Петя Петров [55555]", bot=False, roles=[])
        self.nobody = NS(id=13, display_name="cool_guy", bot=False, roles=[])
        self.g = G(5, [self.vanya, self.petya, self.nobody])
        self.fw, self.bot = make_fw(self.db, [self.vanya, self.petya, self.nobody], lead_ids={99})
        self.db.q("INSERT INTO applications(guild_id, user_id, nick, static_id, status, created_at) "
                  "VALUES(5, 11, 'Vanya_Ulyanov', '64524', 'accepted', '2026-01-01')")
        self.db.q("INSERT INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, applicant, "
                  "summary, status, updated_ts) VALUES(5, 47492, 1491, 'Иск №58', 'u1', 'иск', 'defendant', "
                  "'Ваня Ульянов (64524)', 'Иса Мавроди', 's', 'reviewed', 1)")
        self.db.q("INSERT INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, applicant, "
                  "summary, status, body, updated_ts) VALUES(5, 600, 1513, 'Жалоба на ДМ', 'u2', 'жалоба', 'witness', "
                  "'Феликс Ульянов', 'Кто-то', 's', 'pending', 'Свидетели: Петя Петров, Феликс Ульянов', 2)")
        self.db.gset(5, "forum:last", int(time.time()) - 60)

    def test_self_from_application(self):
        """Себя бот узнаёт молча: в Discord он «Ваня», статик и игровое имя — из анкеты."""
        r = self.fw.person_report(self.g, self.vanya)
        self.assertIn("Vanya Ulyanov", r)
        self.assertIn("64524", r)
        self.assertIn("Иск №58", r)
        self.assertIn("ответчик", r)
        self.assertNotIn("Жалоба на ДМ", r)

    def test_from_nick_and_role(self):
        r = self.fw.person_report(self.g, self.petya)  # статик и имя — из ника «Петя Петров [55555]»
        self.assertIn("Жалоба на ДМ", r)
        self.assertIn("свидетель", r)

    def test_unknown_asks_once(self):
        r = self.fw.person_report(self.g, self.nobody)
        self.assertTrue(r.endswith("?"))
        self.assertIn("Имя Фамилия и статик", r)
        r = self.fw.person_report(self.g, self.nobody, game_name="Коля Кольцов", static="77777")
        self.assertIn("Чисто", r)  # данные дал — проверили, без новых вопросов

    def test_never_checked_is_not_clean(self):
        self.db.gdel(5, "forum:last")
        r = self.fw.person_report(self.g, self.nobody, game_name="Коля Кольцов")
        self.assertNotIn("Чисто", r)
        self.assertIn("/форум вход", r)

    def test_access(self):
        from cogs.assistant import Assistant
        asst = Assistant.__new__(Asst := Assistant)
        asst.bot = NS(get_cog=lambda n: self.fw, has=self.bot.has)
        asst.find_members = lambda g, q: [self.petya] if q.get("names") == ["петю"] else []
        msg = NS(channel=None)

        def ask(me, a):
            return asyncio.run(Asst.forum_check(asst, msg, self.g, me, a))
        self.assertIn("руководство и старший состав", ask(self.vanya, {"target_name": "петю"}))
        self.assertIn("Иск №58", ask(self.vanya, {"target_name": None}))  # себя — можно
        self.assertIn("Иск №58", ask(self.vanya, {"target_name": "меня"}))
        lead = NS(id=99, display_name="Глава", bot=False, roles=[])
        self.assertIn("Жалоба на ДМ", ask(lead, {"target_name": "петю"}))
        # сводка по семье — любому из семьи; фильтр статуса доходит до отчёта
        r = ask(self.vanya, {"scope": "family", "status": "open"})
        self.assertIn("Жалоба на ДМ", r)
        self.assertNotIn("Иск №58", r)
        r = ask(self.vanya, {"scope": "self", "target_name": "Ваня", "status": "open"})
        self.assertNotIn("Иск №58", r)
        self.assertIn("Рассмотренных и закрытых — 1", r)

    def test_open_never_shows_reviewed(self):
        """Баг: на «не рассмотренные» бот выдал рассмотренные. Рассмотренный иск — только в reviewed/all."""
        r = self.fw.person_report(self.g, self.vanya, status="open")
        self.assertNotIn("Иск №58", r)
        self.assertIn("Чисто: не рассмотренные", r)
        self.assertIn("Иск №58", self.fw.person_report(self.g, self.vanya, status="reviewed"))
        self.assertIn("Иск №58", self.fw.person_report(self.g, self.vanya, status="all"))

    def test_moved_to_reviewed_section(self):
        """В записи дела ещё «на рассмотрении», но тему уже перенесли в «рассмотренные» — это НЕ open."""
        self.db.q("INSERT INTO forum_threads(guild_id, thread_id, forum_id, title, author, last_ts, seen_ts) "
                  "VALUES(5, 600, 1514, 'Жалоба на ДМ', 'Кто-то', 10, 0)")
        r = self.fw.family_report(self.g, "open")
        self.assertNotIn("Жалоба на ДМ", r)
        self.assertIn("Чисто у всех", r)
        r = self.fw.family_report(self.g, "reviewed")
        self.assertIn("Жалоба на ДМ", r)
        self.assertNotIn("на рассмотрении", r)

    def test_family_grouped(self):
        r = self.fw.family_report(self.g, "all")
        self.assertIn("**Ваня**", r)  # иск привязан к Ване (анкета: Vanya Ulyanov, 64524)
        self.assertIn("Иск №58", r)
        self.assertIn("**Петя Петров [55555]**", r)  # свидетель в жалобе
        self.assertIn("⚪ 📝", r)  # на рассмотрении — серый кружок, итога ещё нет
        self.assertIn("на рассмотрении", r)
        self.assertNotIn("Чисто", r)
        self.db.gdel(5, "forum:last")
        self.assertNotIn("Чисто", self.fw.family_report(self.g, "open"))  # ни разу не проверяли — «чисто» не говорим


class Verdict(unittest.TestCase):
    def test_guess(self):
        from cogs.forumwatch import guess_verdict as gv
        self.assertEqual(gv("Жалоба отклонена, нарушений нет.", "reviewed", "defendant", "жалоба"),
                         ("жалоба отклонена", "good"))
        self.assertEqual(gv("В удовлетворении иска отказано", "reviewed", "defendant", "иск")[1], "good")
        self.assertEqual(gv("Жалоба не удовлетворена", "reviewed", "defendant", "жалоба")[1], "good")
        out, v = gv("Жалоба удовлетворена. Игроку выдан штраф 50 000 и выговор.", "reviewed", "defendant", "жалоба")
        self.assertEqual(v, "bad")
        self.assertIn("удовлетворена, штраф", out)
        self.assertEqual(gv("Иск удовлетворён частично", "reviewed", "defendant", "иск")[1], "neutral")
        self.assertEqual(gv("Отправлено на доработку", "closed", "defendant", "жалоба")[1], "neutral")
        self.assertEqual(gv("Подсудимый оправдан", "reviewed", "defendant", "иск"), ("оправдан", "good"))
        self.assertEqual(gv("что-то", "rejected", "defendant", "жалоба")[1], "good")  # раздел «отклонённые»
        self.assertEqual(gv("Жалоба отклонена", "reviewed", "plaintiff", "жалоба")[1], "bad")  # сам подал
        self.assertEqual(gv("Жалоба удовлетворена, штраф", "reviewed", "witness", "жалоба")[1], "neutral")


class VerdictCache(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.vanya = NS(id=11, display_name="Ваня", bot=False, roles=[])
        self.g = G(5, [self.vanya])
        self.fw, self.bot = make_fw(self.db, [self.vanya])
        self.calls = 0
        test = self

        class AI:
            on = True

            async def ask_json(self, system, user, **kw):
                test.calls += 1
                return {"decided": True, "short": "суд признал виновным, штраф 100к", "verdict": "bad"}
        self.bot.ai_for = lambda g: AI()
        self.db.q("INSERT INTO applications(guild_id, user_id, nick, static_id, status, created_at) "
                  "VALUES(5, 11, 'Vanya_Ulyanov', '64524', 'accepted', '2026-01-01')")
        self.db.q("INSERT INTO forum_cases(guild_id, thread_id, forum_id, title, url, kind, role, defendant, member_id, "
                  "applicant, status, decision, updated_ts) VALUES(5, 70, 1491, 'Иск №7', 'u7', 'иск', 'defendant', "
                  "'Ваня Ульянов', 11, 'Истец', 'reviewed', 'Суд решил: виновен, штраф 100к', 1)")
        self.db.gset(5, "forum:last", int(time.time()) - 60)

    def test_ai_once_then_cache(self):
        asyncio.run(self.fw.fill_verdicts(self.g))
        asyncio.run(self.fw.fill_verdicts(self.g))
        self.assertEqual(self.calls, 1)  # второй цикл — из кэша, модель не дёргаем
        r = self.fw.person_report(self.g, self.vanya)
        self.assertEqual(self.calls, 1)  # вывод — тоже из кэша
        self.assertIn("🔴 ⚖️ Иск · Верховный суд · подал Истец — **суд признал виновным, штраф 100к**", r)
        # в теме новый ответ — пересчитываем ровно один раз
        self.db.q("UPDATE forum_cases SET decision='Апелляция: решение отменено' WHERE thread_id=70")
        asyncio.run(self.fw.fill_verdicts(self.g))
        asyncio.run(self.fw.fill_verdicts(self.g))
        self.assertEqual(self.calls, 2)

    def test_no_ai_heuristic(self):
        self.bot.ai_for = lambda g: NS(on=False)
        self.db.q("UPDATE forum_cases SET decision='В удовлетворении иска отказано' WHERE thread_id=70")
        asyncio.run(self.fw.fill_verdicts(self.g))
        r = self.fw.person_report(self.g, self.vanya, status="reviewed")
        self.assertIn("🟢", r)
        self.assertIn("иск отклонён", r)


if __name__ == "__main__":
    unittest.main()
