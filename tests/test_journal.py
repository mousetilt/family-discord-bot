# -*- coding: utf-8 -*-
"""Журнал действий: войс (вход/выход/переход), микро/звук/камера/стрим, ник и роли — строки и записи в базе."""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))  # не трогать боевую базу

from core import family  # noqa: E402
from core.db import DB  # noqa: E402
from cogs.journal import Journal, SCHEMA  # noqa: E402

GID = 9


class FakeBot:
    def __init__(self, db, off=()):
        self.db, self.off = db, set(off)
        self.user = NS(id=1)

    def is_family(self, g):
        return g.id == GID

    def feature(self, g, key):
        return key not in self.off

    def num(self, g, key):
        return family.number(self.db, g.id, key)

    def ch(self, g, key):
        return None

    def role(self, g, key):
        return None


def chan(cid, name):
    return NS(id=cid, name=name, mention=f"#{name}")


def vs(channel=None, **flags):
    base = dict(self_mute=False, self_deaf=False, self_video=False, self_stream=False, mute=False, deaf=False)
    base.update(flags)
    return NS(channel=channel, **base)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.c.executescript(SCHEMA)
        self.bot = FakeBot(self.db)
        self.j = object.__new__(Journal)  # без __init__: фоновые циклы в тесте не нужны
        self.j.bot, self.j.buf, self.j.joined = self.bot, {}, {}
        perms = NS(view_audit_log=False)  # без журнала аудита «кто сделал» не ищем — и не ждём
        self.g = NS(id=GID, me=NS(guild_permissions=perms))
        self.m = NS(id=50, bot=False, guild=self.g, mention="<@50>", display_name="Даша", name="dasha")
        self.kitchen, self.balcony = chan(1, "Кухня"), chan(2, "Балкон")

    def run_(self, coro):
        return asyncio.run(coro)

    def lines(self, key="voice_log"):
        return self.j.buf.get((GID, key), [])

    def kinds(self):
        return [r["kind"] for r in self.db.all("SELECT kind FROM member_log WHERE guild_id=? ORDER BY id", GID)]

    def test_voice_join_move_leave(self):
        self.run_(self.j.on_voice_state_update(self.m, vs(), vs(self.kitchen, self_mute=True)))
        self.run_(self.j.on_voice_state_update(self.m, vs(self.kitchen), vs(self.balcony)))
        self.run_(self.j.on_voice_state_update(self.m, vs(self.balcony), vs()))
        text = "\n".join(self.lines())
        self.assertIn("зашёл в #Кухня (микро выкл)", text)
        self.assertIn("#Кухня → #Балкон", text)
        self.assertIn("вышел из #Балкон, пробыл", text)
        self.assertEqual(self.kinds(), ["voice"] * 3)

    def test_mic_camera_stream(self):
        k = self.kitchen
        self.run_(self.j.on_voice_state_update(self.m, vs(k), vs(k, self_mute=True)))
        self.run_(self.j.on_voice_state_update(self.m, vs(k, self_mute=True), vs(k, self_mute=True, self_deaf=True)))
        self.run_(self.j.on_voice_state_update(self.m, vs(k), vs(k, self_video=True)))
        self.run_(self.j.on_voice_state_update(self.m, vs(k, self_video=True), vs(k)))
        self.run_(self.j.on_voice_state_update(self.m, vs(k), vs(k, self_stream=True)))
        text = "\n".join(self.lines())
        for s in ("выключил микрофон", "выключил звук", "включил камеру", "выключил камеру", "начал стрим"):
            self.assertIn(s, text)
        self.assertEqual(text.count("микрофон"), 1)  # заглушка не дублирует «выключил микрофон»

    def test_join_does_not_log_flags_as_toggles(self):
        self.run_(self.j.on_voice_state_update(self.m, vs(), vs(self.kitchen, self_mute=True, self_video=True)))
        self.assertEqual(self.kinds(), ["voice"])

    def test_toggle_off(self):
        self.bot.off = {"journal:mic"}
        k = self.kitchen
        self.run_(self.j.on_voice_state_update(self.m, vs(k), vs(k, self_mute=True)))
        self.assertEqual(self.kinds(), [])

    def test_nick_and_roles(self):
        r1 = NS(id=7, mention="@Новичок")
        before = NS(guild=self.g, id=50, nick="Dasha_Old", name="dasha", roles=[], timed_out_until=None,
                    guild_avatar=None, mention="<@50>", display_name="Dasha_Old")
        after = NS(guild=self.g, id=50, nick="Дарья_Ульянова", name="dasha", roles=[r1], timed_out_until=None,
                   guild_avatar=None, mention="<@50>", display_name="Дарья_Ульянова")
        self.run_(self.j.on_member_update(before, after))
        text = "\n".join(self.lines("audit"))
        self.assertIn("ник «Dasha_Old» → «Дарья_Ульянова»", text)
        self.assertIn("+ @Новичок", text)
        self.assertEqual(self.kinds(), ["members", "members"])

    def test_other_server_ignored(self):
        other = NS(id=GID + 1, me=self.g.me)
        m = NS(id=51, bot=False, guild=other, mention="<@51>", display_name="X", name="x")
        self.run_(self.j.on_voice_state_update(m, vs(), vs(self.kitchen)))
        self.assertEqual(self.j.buf, {})


if __name__ == "__main__":
    unittest.main()
