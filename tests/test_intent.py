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
            self.assertEqual(forum_intent(t), {"target_name": None, "fresh": False}, t)

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


if __name__ == "__main__":
    unittest.main()
