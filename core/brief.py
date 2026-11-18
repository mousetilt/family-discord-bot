# -*- coding: utf-8 -*-
"""Фаза B: сквозные вопросы по реальным данным семьи («что в целом по нашим на форуме», «позиция защиты по всем
активным делам с учётом практики»).

collect() аккуратно собирает релевантное — дела с форума, ростер семьи, ручной список форума, куски базы знаний
(юрист: законы, практика, формы), профиль и числа семьи — в пределах бюджета символов, и отдаёт ИИ одним
контекстом. С Claude бюджет большой (длинный контекст, сильная модель, smart=True); на бесплатных — короткий,
чтобы не упереться в их лимит токенов в минуту. Данные — только из базы, ИИ запрещено достраивать."""
import logging
import re

log = logging.getLogger("family-bot.brief")

BUDGET_CLAUDE = 60000   # ≈ 15–20 тыс. токенов — заметно дешевле лимита, но хватает на всю семью
BUDGET_FREE = 9000
FOCUS = ("forum", "family", "law", "all")
# доля бюджета на раздел по фокусу: дела · люди · база · настройки
SHARES = {"forum": (0.45, 0.15, 0.35, 0.05), "family": (0.2, 0.6, 0.15, 0.05),
          "law": (0.3, 0.05, 0.6, 0.05), "all": (0.35, 0.25, 0.35, 0.05)}
ROLE_RU = {"defendant": "ответчик", "plaintiff": "заявитель", "witness": "свидетель", "mention": "упомянут"}
STATUS_RU = {"pending": "на рассмотрении", "reviewed": "рассмотрено", "rejected": "отклонено", "closed": "закрыто"}

SYSTEM = """Ты аналитик и юрист RP-семьи «{family}» (игра «Россия Онлайн», сервер {server}). Ниже — РЕАЛЬНЫЕ данные бота:
дела с форума, люди семьи, выдержки из законов/практики/форм и настройки. Ответь на вопрос связно и по делу:
обобщи, сгруппируй, выдели главное, дай выводы и, если просят, позицию/план действий.
ПРАВИЛА: имена, статики, номера дел, итоги, суммы, статьи — ТОЛЬКО из данных ниже, ничего не достраивай; чего в
данных нет — прямо скажи «в данных нет». На статьи ссылайся так, как они названы в выдержках. Ссылки на темы форума
давай как есть. Пиши по-русски, без воды, до {limit} знаков, разметка Discord (жирный, списки)."""


def _cut(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:max(0, n - 1)] + "…"


def _fit(lines, budget):
    """Строки целиком, пока влезают в бюджет."""
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            out.append(f"… ещё {len(lines) - len(out)} — не влезло")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def focus_of(question, given=None):
    if given in FOCUS:
        return given
    q = str(question or "").lower()
    if re.search(r"форум|жалоб|иск|дел[оау]|суд|прокурат|обвин|ответчик", q):
        return "forum"
    if re.search(r"закон|стать|кодекс|практик|устав|правил", q):
        return "law"
    if re.search(r"кто|состав|люд|участник|новичк|ранг|выговор|актив", q):
        return "family"
    return "all"


def case_lines(bot, guild, full=False):
    """Дела семьи с форума: статус сейчас (раздел темы главнее), роль, стороны, итог; с full — суть и решение."""
    fw = bot.get_cog("ForumWatch")
    rows = bot.db.all("SELECT * FROM forum_cases WHERE guild_id=? ORDER BY (status='pending') DESC, updated_ts DESC "
                      "LIMIT 80", guild.id)
    out = []
    for c in rows:
        try:
            st = fw.case_status(guild, c) if fw else (c["status"] or "pending")
            _dot, outcome = fw.case_verdict(c, c["role"], st) if fw else ("", c["outcome"] or "")
        except Exception:  # noqa: BLE001 — одно кривое дело не ломает сводку
            st, outcome = c["status"] or "pending", c["outcome"] or ""
        head = (f"• {(c['kind'] or 'дело').capitalize()}" + (f" №{c['case_no']}" if c["case_no"] else "") +
                f" — {STATUS_RU.get(st, st)}; наш: {_cut(c['defendant'], 80) or '?'} ({ROLE_RU.get(c['role'], c['role'])})"
                f"; подал: {_cut(c['applicant'], 60) or '?'}" + (f"; итог: {_cut(outcome, 160)}" if st != "pending" else "")
                + f"\n  {_cut(c['title'], 100)} {c['url'] or ''}")
        if full:
            if c["summary"]:
                head += f"\n  суть: {_cut(c['summary'], 400)}"
            if c["body"]:
                head += f"\n  текст жалобы: {_cut(c['body'], 900)}"
            if st != "pending" and c["decision"]:
                head += f"\n  решение: {_cut(c['decision'], 700)}"
        out.append(head)
    return out


def roster_lines(bot, guild):
    """Люди семьи: ранг, ник/статик из анкеты, активные выговоры. Без личного — только то, что видно руководству."""
    db = bot.db
    warns = {r["user_id"]: r["n"] for r in db.all(
        "SELECT user_id, COUNT(*) n FROM warns WHERE guild_id=? AND active=1 GROUP BY user_id", guild.id)}
    apps = {}
    for r in db.all("SELECT user_id, nick, static_id FROM applications WHERE guild_id=? AND status='accepted' "
                    "ORDER BY id", guild.id):
        apps[r["user_id"]] = r
    members = sorted(bot.family_members(guild), key=lambda m: (bot.rank_index(m), m.display_name.lower()))
    out = []
    for m in members:
        a = apps.get(m.id)
        game = f"{a['nick'] or ''} [{a['static_id']}]".strip() if a and (a["nick"] or a["static_id"]) else ""
        out.append(f"• {m.display_name} — {bot.rank_name(m) or '?'}" + (f"; в игре: {game}" if game else "") +
                   (f"; выговоров: {warns[m.id]}" if warns.get(m.id) else ""))
    for r in db.all("SELECT name, static_id, head FROM forum_people WHERE guild_id=?", guild.id):
        out.append(f"• (форум-список) {r['name']}" + (f" [{r['static_id']}]" if r["static_id"] else "") +
                   (" — Глава" if r["head"] else ""))
    return out


def settings_text(bot, guild):
    fam = bot.family(guild)
    nums = []
    for key in ("warn_limit", "probation_days"):
        try:
            nums.append(f"{key}={bot.num(guild, key)}")
        except Exception:  # noqa: BLE001
            pass
    return f"Семья: {fam.get('name')} · сервер: {fam.get('server')} · " + ", ".join(nums)


async def collect(bot, guild, question, ai, focus=None, budget=None):
    """→ (текст контекста, источники базы). Разделы в доле бюджета по фокусу; пустой раздел отдаёт место базе."""
    focus = focus_of(question, focus)
    budget = budget or (BUDGET_CLAUDE if getattr(ai, "is_claude", False) else BUDGET_FREE)
    s_cases, s_people, s_kb, s_set = (int(budget * x) for x in SHARES[focus])
    parts, sources = [], []
    try:
        cases = case_lines(bot, guild, full=getattr(ai, "is_claude", False) or focus == "forum")
    except Exception:  # noqa: BLE001
        log.exception("brief: дела")
        cases = []
    try:
        people = roster_lines(bot, guild)
    except Exception:  # noqa: BLE001
        log.exception("brief: люди")
        people = []
    spare = (0 if cases else s_cases) + (0 if people else s_people)
    parts.append("=== ДЕЛА НА ФОРУМЕ ===\n" + (_fit(cases, s_cases) if cases else "дел в базе нет"))
    parts.append(f"=== ЛЮДИ СЕМЬИ ({len(people)}) ===\n" + (_fit(people, s_people) if people else "нет данных"))
    try:
        kb_text, sources, _ = await bot.kb.context_smart(guild.id, question, ai)
        parts.append("=== БАЗА: ЗАКОНЫ, ПРАКТИКА, ФОРМЫ ===\n" + _cut(kb_text, s_kb + spare) if kb_text else
                     "=== БАЗА ===\nпусто")
    except Exception:  # noqa: BLE001 — без базы ответ всё равно будет по делам и людям
        log.exception("brief: база")
    parts.append("=== НАСТРОЙКИ ===\n" + _cut(settings_text(bot, guild), s_set))
    return "\n\n".join(parts), sources


async def answer(bot, guild, question, focus=None):
    """Сквозной ответ сильной моделью по собранному контексту → текст (≤1900)."""
    ai = bot.ai_for(guild)
    if not ai.on:
        return "ИИ не подключён — сквозной разбор не сделать. `/ии ключ`."
    context, sources = await collect(bot, guild, question, ai, focus)
    fam = bot.family(guild)
    limit = 1700
    system = SYSTEM.format(family=fam.get("name"), server=fam.get("server"), limit=limit)
    text = await ai.ask(system + "\n\nДАННЫЕ:\n" + context, f"Вопрос: {question}", max_tokens=1500, smart=True)
    text = (text or "").strip() or "ИИ не дал ответа."
    try:  # самопроверка: статьи, которых нет в базе, помечаются (как у юриста)
        from core.legal import verify
        text, missing = verify(text, bot.kb.docs(guild.id))
        if missing:
            text += "\n⚠️ Не нашёл в базе: " + ", ".join(missing[:5]) + " — сверь с первоисточником."
    except Exception:  # noqa: BLE001
        log.exception("brief: самопроверка")
    text = _cut(text, 1850)
    if sources:
        text += "\n-# База: " + ", ".join(t for t, _ in sources[:4])[:180]
    return text
