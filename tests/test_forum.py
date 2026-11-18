# -*- coding: utf-8 -*-
"""Форум-монитор: разбор страниц XenForo, поиск фамилии семьи во всех формах, роль (ответчик/заявитель/свидетель)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.forum import (HEAD_TITLES, analyze, case_number, parse_forum_list, parse_thread_page,  # noqa: E402
                        status_from, surname_rx, surname_stem, thread_id, titles_rx)

PEOPLE = [{"name": "Ваня Ульянов", "static": "64524"}, {"name": "Юлиан Ульянов", "static": "104589", "head": True},
          {"name": "Феликс Ульянов", "static": None}]
RX = surname_rx(surname_stem("Ульяновы"))
HRX = titles_rx(HEAD_TITLES)

LIST_HTML = """
<div class="structItem structItem--thread js-threadListItem-47492" data-author="Isa_Mavrodi">
  <div class="structItem-title"><a href="/forums/1491/?prefix_id=3" class="labelLink"><span class="label">Рассмотрено</span></a>
    <a href="/threads/isk-58.47492/">Иск №58</a></div>
  <div class="structItem-startDate"><time class="u-dt" data-timestamp="1763300000">…</time></div>
  <time class="structItem-latestDate u-dt" data-timestamp="1763400000">…</time>
</div>
<div class="structItem structItem--thread" data-author="Леонид">
  <div class="structItem-title"><a href="/threads/220.47770/">Обращение №220</a></div>
  <span class="structItem-status structItem-status--locked"></span>
  <div class="structItem-startDate"><time data-timestamp="1763000000"></time></div>
</div>"""

THREAD_HTML = """
<html><body>
<div class="p-breadcrumbs"><a href="/forums/arbat.1400/">Арбатский</a><a href="/forums/vs-rassmotrennye.1491/">ВС</a></div>
<h1 class="p-title-value"><span class="label">Рассмотрено</span> Иск №58</h1>
<article class="message message--post" data-author="Isa_Mavrodi" data-content="post-900">
  <div class="message-attribution-main"><time data-timestamp="1763300000"></time></div>
  <div class="message-body"><div class="bbWrapper">Истец: Иса Мавроди<br>Ответчик: Ваня Ульянов (64524)</div></div>
</article>
<article class="message message--post" data-author="Судья" data-content="post-901">
  <div class="message-attribution-main"><time data-timestamp="1763400000"></time></div>
  <div class="message-body"><div class="bbWrapper">Суд постановил: иск удовлетворить частично.</div></div>
</article>
<div class="pageNav-main"><a>1</a><a>2</a></div>
</body></html>"""


class Parse(unittest.TestCase):
    def test_list(self):
        items = parse_forum_list(LIST_HTML)
        self.assertEqual([i["id"] for i in items], [47492, 47770])
        self.assertEqual(items[0]["title"], "Иск №58")
        self.assertEqual(items[0]["author"], "Isa_Mavrodi")
        self.assertEqual((items[0]["start_ts"], items[0]["last_ts"]), (1763300000, 1763400000))
        self.assertEqual(items[0]["prefix"], "Рассмотрено")
        self.assertTrue(items[1]["locked"])
        self.assertEqual(items[1]["last_ts"], 1763000000)  # ответов нет — время создания

    def test_thread(self):
        meta, posts = parse_thread_page(THREAD_HTML)
        self.assertEqual(meta["forum_id"], 1491)
        self.assertEqual(meta["title"], "Иск №58")
        self.assertEqual(meta["pages"], 2)
        self.assertEqual([p["id"] for p in posts], [900, 901])
        self.assertIn("Ответчик: Ваня Ульянов", posts[0]["text"])
        self.assertEqual(posts[1]["ts"], 1763400000)

    def test_ids_and_status(self):
        self.assertEqual(thread_id("/threads/zhaloba-na-igroka.12345/page-2"), 12345)
        self.assertEqual(thread_id("https://forum.russia.online/threads/46522/"), 46522)
        self.assertEqual(case_number("Иск №58 в ВС"), "58")
        self.assertEqual(status_from(1491), "reviewed")
        self.assertEqual(status_from(1515), "rejected")
        self.assertEqual(status_from(1490, "На рассмотрении"), "pending")
        self.assertEqual(status_from(1490, "Отказано"), "rejected")
        self.assertEqual(status_from(1490, "", locked=True), "closed")


class Match(unittest.TestCase):
    def test_forms(self):
        for w in ("Ульянов", "Ульянова", "Ульянову", "Ульяновым", "Ульяновых", "Ульяновы", "Ульяновой", "Ulyanov",
                  "Ulianova", "ульянов"):
            self.assertTrue(RX.search(f"жалоба на {w} за ДМ"), w)
        for w in ("Ульяновск", "Ульяновка", "Ульяновская"):
            self.assertFalse(RX.search(f"город {w}"), w)

    def test_head_titles(self):
        for t in ("Жалоба на лидера правительства", "действия Премьер-министра", "премьер министру"):
            self.assertTrue(HRX.search(t), t)

    def role(self, title, text, author="x", **kw):
        r = analyze(title, text, author, RX, PEOPLE, HRX, **kw)
        return (r["role"], (r["who"] or {}).get("name"), r["head"]) if r else None

    def test_roles(self):
        cases = {
            ("Иск №58", "Истец: Иса Мавроди\nОтветчик: Ваня Ульянов (64524)"): ("defendant", "Ваня Ульянов", False),
            ("Мавроди vs Ульянов", ""): ("defendant", None, False),
            ("Ульянов vs Мавроди", ""): ("plaintiff", None, False),
            ("Жалоба", "Ник нарушителя: Felix_Ulyanov\nВаш ник: Леонид"): ("defendant", "Феликс Ульянов", False),
            ("Жалоба", "Ваш ник: Ваня Ульянов\nНик нарушителя: Петров"): ("plaintiff", "Ваня Ульянов", False),
            ("Жалоба на лидера правительства", "текст"): ("defendant", None, True),
            ("ДМ", "Свидетели: Феликс Ульянов, Петров"): ("witness", "Феликс Ульянов", False),
            ("Обращение №220", "Прошу проверить сотрудника Феликса Ульянова"): ("defendant", "Феликс Ульянов", False),
            ("Иск", "В отношении Юлиана Ульянова"): ("defendant", "Юлиан Ульянов", True),
            ("Жалоба от Ивана на Ульянова", ""): ("defendant", None, False),
            ("Жалоба", "Обвиняемый: 104589"): ("defendant", "Юлиан Ульянов", True),
        }
        for (title, text), want in cases.items():
            self.assertEqual(self.role(title, text), want, f"{title} | {text}")

    def test_misses_and_ignore(self):
        self.assertIsNone(self.role("Оскорбление", "ничего такого"))
        self.assertIsNone(self.role("ДМ", "Был с Сакрадовым и Ульяновым", ignore=["Сакрадов"]))
        self.assertIsNone(self.role("Дело АДМ-41", "Ответчик: Ваня Ульянов", ignore=["АДМ-41"]))

    def test_author_is_family(self):
        self.assertEqual(self.role("Жалоба", "упомянул Ульянова", author="Vanya_Ulyanov")[0], "plaintiff")


class Cog(unittest.TestCase):
    def test_seed_and_people(self):
        """Известные дела импортируются один раз, только домашнему серверу; ростер берёт фамилию из ников."""
        from types import SimpleNamespace as NS

        from cogs.forumwatch import SCHEMA, ForumWatch
        from core.db import DB
        db = DB(":memory:")
        db.c.executescript(SCHEMA)
        leader = NS(id=1)
        m1 = NS(id=11, display_name="Ваня Ульянов [64524]", roles=[], bot=False)
        m2 = NS(id=12, display_name="Петя Петров", roles=[], bot=False)
        import discord

        class G(discord.Object):
            def get_member(self, i):
                return {11: m1, 12: m2}.get(i)
        g = G(5)
        bot = NS(db=db, is_home=lambda g: True, family=lambda g: {"name": "Ульяновы"}, role=lambda g, k: leader,
                 family_members=lambda g: [m1, m2])
        fw = ForumWatch.__new__(ForumWatch)
        fw.bot = bot
        fw.seed(g)
        fw.seed(g)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM forum_cases WHERE guild_id=5")["n"], 4)
        ps = {p["name"]: p for p in fw.people(g)}
        self.assertEqual(ps["Ваня Ульянов"]["user_id"], 11)  # ручная запись связана с участником по статику
        self.assertTrue(ps["Юлиан Ульянов"]["head"])
        self.assertNotIn("Петя Петров", ps)
        other = G(6)
        bot.is_home = lambda g: False
        fw.seed(other)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM forum_cases WHERE guild_id=6")["n"], 0)


if __name__ == "__main__":
    unittest.main()
