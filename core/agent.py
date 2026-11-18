# -*- coding: utf-8 -*-
"""Агентные цепочки Швейцара (только когда у семьи активен Claude).

Модель может ответить {"continue": true, "actions": [справки…]}: бот выполняет ТОЛЬКО справочные действия (ASK —
чтение базы/форума/законов), возвращает их результаты модели, и она решает следующий шаг или финал. Не больше
MAX_STEPS шагов. Изменяющие действия (наказания, роли, казна, рассылки…) в промежуточных шагах НЕ выполняются —
копятся и в финале уходят в обычные проверки прав и панель подтверждения «Выполнить».
Здесь — чистые помощники без Discord (их проверяют тесты); сам цикл — Assistant.agent()."""
import json
import re

MAX_STEPS = 5          # шагов со справками, считая первый ответ модели
STEP_READS = 4         # справок за один шаг
STEP_CHARS = 4000      # результат одного шага в запросе к модели
BUDGET = 12000         # все результаты шагов в запросе (старые шаги ужимаются)
REPLY_CHARS = 1500     # финальный ответ цепочки (обычный ответ Швейцара — 500)
DELIVER = {"law_doc", "draft"}  # результаты, которые показываем человеку целиком (документ, черновик)

# задача из нескольких частей: «собери … и сделай черновик», «проверь … потом составь …»
_MULTI = re.compile(
    r"(?:\bи\b|,|потом|затем|после\s+чего|а\s+дальше|и\s+сразу)\s+(?:сразу\s+)?"
    r"(?:сделай|составь|напиши|подготовь|набросай|оформи|сравни|проанализируй|разбери|дай\s+позици)"
    r"|черновик\w*\s+(?:иска|жалоб|заявлени|обращени|ходатайств|отзыв)|позици\w*\s+защит", re.I)

PROGRESS = {
    "forum_check": "проверяю форум", "people": "смотрю, кто в семье", "law": "листаю законы",
    "law_doc": "готовлю черновик документа", "profile": "смотрю профиль", "warns": "смотрю выговоры",
    "web_search": "ищу в интернете", "missed": "собираю, что было", "draft": "пишу черновик",
    "top": "считаю активность", "bl_check": "проверяю чёрный список", "digest": "собираю сводку",
    "car_list": "смотрю автопарк", "recruit_stats": "считаю рекрутёров", "treasury_balance": "смотрю казну",
    "treasury_debtors": "смотрю должников", "fine_list": "смотрю штрафы", "market": "смотрю рынок",
    "brief": "собираю всё по делу",
}

PROMPT = """МНОГОШАГОВЫЕ ЗАДАЧИ (до 5 шагов). Если чтобы ответить или сделать, сначала нужны данные (форум, люди, законы,
профиль, казна…) — верни "continue":true и ТОЛЬКО справочные действия этого шага; бот выполнит их и пришлёт
результаты, тогда решай следующий шаг. Пока цепочка идёт, "reply" — пара слов, что делаешь.
Изменяющие действия (наказания, роли, казна, рассылки, объявления, ЛС) до финала НЕ выполняются — верни их в
финальном шаге, бот сам проверит права и попросит подтверждение.
Финал: "continue":false, в "reply" — связный ответ ТОЛЬКО по результатам шагов (до 1500 знаков): имена, статики,
номера дел, итоги — только оттуда. Уже сделанные справки не повторяй. Документ/черновик покажется человеку сам.
- law_doc: {"kind":"иск|жалоба|обращение в прокуратуру|ходатайство|отзыв на иск|…","question":"что составить и по
  кому","facts":"факты из результатов шагов: стороны, статики, номера дел, суть, даты"} — черновик документа юристом
  (по базе законов и форм, с самопроверкой статей; чего нет — [заполнить]).
Пример: «собери дела наших и сделай черновик иска по Ване» → {"continue":true,"actions":[{"type":"forum_check",
"scope":"family","status":"all"}]} → (результаты) → {"continue":true,"actions":[{"type":"law_doc","kind":"иск",
"question":"исковое по делу Вани","facts":"…из шага 1…"}]} → {"continue":false,"reply":"Готово: …"}.
Простую просьбу делай как обычно, без "continue".
"""


def multi_step(text):
    """Просьба явно из нескольких частей — не отдавать быстрому детектору (forum_intent), пусть решает модель."""
    return bool(_MULTI.search(str(text or "")))


def split(actions, read_types):
    """→ (справки, изменяющие) в исходном порядке."""
    reads = [a for a in actions if a.get("type") in read_types]
    return reads, [a for a in actions if a.get("type") not in read_types]


def _key(a):
    return json.dumps({k: v for k, v in a.items() if not str(k).startswith("_")}, ensure_ascii=False, sort_keys=True,
                      default=str)


def merge(pending, new):
    """Добавить действия без повторов (одинаковые по содержимому — один раз)."""
    seen = {_key(a) for a in pending}
    out = list(pending)
    for a in new:
        k = _key(a)
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


def progress_label(t):
    return PROGRESS.get(t, "собираю данные")


def progress_text(done, finished=False):
    """«⏳ проверяю форум → готовлю черновик документа…» — ход цепочки для человека."""
    steps = list(dict.fromkeys(done))
    if not steps:
        return ""
    return ("✅ " if finished else "⏳ ") + " → ".join(steps) + ("" if finished else "…")


class Transcript:
    """Результаты шагов для следующего запроса к модели — в бюджете символов (старые ужимаются первыми)."""

    def __init__(self, budget=BUDGET, step_chars=STEP_CHARS):
        self.budget, self.step_chars, self.steps = budget, step_chars, []

    def add(self, step, actions, text):
        acts = ", ".join(_key(a) for a in actions)[:600]
        self.steps.append([step, acts, str(text or "нет данных")[:self.step_chars]])

    def render(self, last=False):
        cut = " …(сокращено)"
        while sum(len(s[1]) + len(s[2]) for s in self.steps) > self.budget and len(self.steps) > 1:
            old = next((s for s in self.steps[:-1] if not s[2].endswith(cut)), None)
            if old is None:  # все старые уже ужаты — выкидываем самый старый
                self.steps.pop(0)
            else:
                old[2] = old[2][:400] + cut
        body = "\n\n".join(f"ШАГ {n}: {a}\nРЕЗУЛЬТАТ:\n{t}" for n, a, t in self.steps)
        tail = ("ЭТО ПОСЛЕДНИЙ ШАГ — верни финал: \"continue\":false." if last else
                "Реши: следующий шаг (\"continue\":true + справки) или финал (\"continue\":false).")
        return f"\n\nРЕЗУЛЬТАТЫ ШАГОВ (реальные данные бота — опирайся только на них):\n{body}\n\n{tail}"
