# -*- coding: utf-8 -*-
"""Нечёткое сравнение имён и названий для помощника.
• similarity(a, b) — 0..1. Есть rapidfuzz (C, быстрый) — через него, иначе difflib (как раньше).
• forms(word) — слово и его начальные формы: «Пете» → {«пете», «петя»}, «Димону» → {«димону», «димон»}.
  Есть pymorphy3 — по словарю русского языка; нет — простые окончания падежей (лучше, чем ничего).
Обе библиотеки необязательные: не встали на хосте — бот работает как раньше."""
import difflib
import functools

try:  # pragma: no cover — зависит от окружения
    from rapidfuzz import fuzz as _fuzz
except Exception:  # noqa: BLE001
    _fuzz = None

try:  # pragma: no cover
    import pymorphy3 as _pm
    _MORPH = _pm.MorphAnalyzer()
except Exception:  # noqa: BLE001 — нет пакета или словаря
    _MORPH = None

# окончания падежей имён: «Пете/Петю/Пети/Петей» → «Петя», «Димону/Димона/Димоном» → «Димон»
_ENDINGS = [("ей", "я"), ("ою", "а"), ("ой", "а"), ("ом", ""), ("ем", "й"), ("ую", "а"), ("ю", "я"), ("е", "я"),
            ("е", "а"), ("у", "а"), ("у", ""), ("и", "я"), ("ы", "а"), ("а", ""), ("я", "й")]


def similarity(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _fuzz is not None:
        return _fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@functools.lru_cache(maxsize=4096)
def forms(word):
    """Слово (в нижнем регистре) + его возможные начальные формы."""
    w = str(word or "").lower().replace("ё", "е").strip()
    out = {w} if w else set()
    if len(w) < 3 or not any("а" <= ch <= "я" for ch in w):
        return frozenset(out)
    if _MORPH is not None:
        try:
            for p in _MORPH.parse(w)[:3]:
                out.add(p.normal_form.replace("ё", "е"))
        except Exception:  # noqa: BLE001
            pass
        return frozenset(out)
    for end, repl in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 2:
            out.add(w[: len(w) - len(end)] + repl)
    return frozenset(out)


def available():
    return {"rapidfuzz": _fuzz is not None, "pymorphy3": _MORPH is not None}
