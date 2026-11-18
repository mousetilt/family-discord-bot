# -*- coding: utf-8 -*-
"""Обращаются ли к Швейцару. Без ИИ: имя с опечатками и в падежах («швецар», «швейцр», «швейцару», «швейцарик»,
«shveitsar»), «бот,» в начале фразы. «Швейцария», «швейцарский» — не обращение.
followup — продолжение разговора без имени: человек только что говорил с ботом в этом канале, и дешёвая модель
решает по смыслу, ему ли новое сообщение (light, ≤60 токенов)."""
import re

NAME = "швейцар"
FOLLOW_SEC = 90  # сколько после ответа бота следующая фраза того же человека может быть ему без обращения
_WORD = re.compile(r"[a-zа-яё]+", re.I)
_LAT = re.compile(r"^(?:sh|s)v[ae][iyj]?(?:ts|tz|c|z)ar\w*$", re.I)
_NOT = re.compile(r"^(?:швейцари(?:я|и|ю|ей|ях|ям|ями)|швейцарск\w*)$")  # страна и прилагательное
_SUFFIX = ("ушка", "ушке", "ушку", "ушки", "ичек", "чику", "чика", "чик", "ику", "ика", "ик", "ом", "ам", "ов", "у",
           "а", "е", "ы", "ь")
_START_BOT = re.compile(r"^\s*(?:бот|ботик|ботяра|помощник)\s*[,!:—-]", re.I)
_START = re.compile(r"^\s*[a-zа-яё]+\s*[,!:.\-—]?\s*", re.I)


def _dist(a, b):
    """Расстояние Левенштейна (короткие слова — быстро и без библиотек)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_name(word):
    """Слово — это имя бота (с опечаткой/в падеже)?"""
    w = str(word or "").lower().replace("ё", "е")
    if len(w) < 5 or _NOT.match(w):
        return False
    if _LAT.match(w):
        return True
    if not w.startswith(("ш", "щ")):
        return False
    stems = {w}
    for s in _SUFFIX:
        if w.endswith(s) and len(w) - len(s) >= 5:
            stems.add(w[:-len(s)])
    return any(_dist(s, NAME) <= (1 if len(s) <= 7 else 2) for s in stems)


def called(text):
    """Обратились к боту по имени (где угодно во фразе) или «бот, …» в начале."""
    text = str(text or "")
    if _START_BOT.match(text):
        return True
    return any(is_name(w) for w in _WORD.findall(text[:400]))


def strip_call(text):
    """Убрать обращение в начале («Швецар, включи…» → «включи…»)."""
    text = str(text or "")
    m = _START_BOT.match(text)
    if m:
        return text[m.end():].strip()
    m = _START.match(text)
    if m and is_name(_WORD.match(text.strip()).group(0)):
        return text[m.end():].strip()
    return text.strip()


FOLLOW_SYSTEM = """Ты решаешь, обращается ли человек к боту-помощнику «Швейцар» в общем чате Discord. Ниже — его
недавний разговор с ботом и новое сообщение ЭТОГО ЖЕ человека (без обращения по имени). to_bot=true, если это
продолжение разговора с ботом: ответ на его реплику, уточнение просьбы («а теперь громче», «нет, в балкон», «и Петю
тоже»), новая просьба к нему или вопрос ему. to_bot=false, если человек говорит с другими людьми, о своём, реагирует
на чужое сообщение или просто ставит «ок/ахах/спс» (на «спасибо» отвечать не нужно). Сомневаешься — false.
Верни JSON: {"to_bot": true|false}"""


def follow_prompt(turns, chat, text):
    hist = "\n".join(f"Человек: {q}\nБот: {a}" for q, a in turns[-3:])
    return (f"{chat}Разговор с ботом:\n{hist}\n\nНовое сообщение человека: {str(text)[:300]}")
