# -*- coding: utf-8 -*-
"""Общие разборщики человеческих фраз: суммы («50к», «1.5кк») и время («завтра 18:30», «через 2 часа»).
Раньше у казны и рынка, у сборов и отложенных объявлений были свои версии, понимавшие разное."""
import datetime as dt
import re

from . import config

MAX_AMOUNT = 10 ** 12
_MULT = [("ккк", 10 ** 9), ("kkk", 10 ** 9), ("млрд", 10 ** 9), ("кк", 10 ** 6), ("kk", 10 ** 6), ("млн", 10 ** 6),
         ("лям", 10 ** 6), ("м", 10 ** 6), ("m", 10 ** 6), ("к", 10 ** 3), ("k", 10 ** 3), ("тыс", 10 ** 3)]
MSK = dt.timezone(dt.timedelta(hours=config.GATHER_UTC_OFFSET))  # часовой пояс семьи (по умолчанию Москва)


# ─────────────────────────────── суммы ───────────────────────────────
def parse_amount(s):
    """«50к» → 50000, «1.2кк» / «1,2 ляма» → 1200000, «2м» → 2000000, «1 200 000» → 1200000.
    Мусор, ≤0 и неоднозначное («1.5» без «к/кк» — 1,5 вирта или 15?) → None: лучше переспросить."""
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        n = int(s)
        return n if 0 < n <= MAX_AMOUNT else None
    t = str(s or "").lower().replace("ё", "е")
    t = re.sub(r"(вирт\w*|руб\w*|\$|₽)", "", t)
    t = t.replace(" ", "").replace(" ", "").replace("_", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)*)([a-zа-я]*)", t)
    if not m:
        return None
    num, suf = m.groups()
    mult = 1
    if suf:
        # короткие суффиксы (к, кк, м…) — точно; слова (ляма, тысяч, млн…) — по началу. «5кг» — не 5000.
        for name, k in _MULT:
            if suf == name or (len(name) >= 3 and suf.startswith(name)):
                mult = k
                break
        else:
            return None
        try:
            val = float(num.replace(",", "."))
        except ValueError:
            return None
    else:  # без суффикса точки/запятые — разделители разрядов («1.500.000»)
        if any(len(g) != 3 for g in re.split(r"[.,]", num)[1:]):
            return None
        val = float(num.replace(",", "").replace(".", ""))
    n = int(round(val * mult))
    return n if 0 < n <= MAX_AMOUNT else None


def find_amount(text):
    """Первая сумма в свободном тексте: «1.5кк, торг» → 1500000, «договорная» → None (для цены на рынке)."""
    for m in re.finditer(r"\d[\d\s., ]*\s*(?:ккк|kkk|млрд|кк|kk|млн|лям\w*|тыс|к|k|м|m)?(?![а-яa-z])",
                         str(text or "").lower()):
        n = parse_amount(m.group(0).strip(" .,"))
        if n:
            return n
    return None


def fmt_amount(n):
    return f"{int(n):,}".replace(",", " ") + " вирт"


# ─────────────────────────────── время ───────────────────────────────
WEEKDAYS = [  # основа (любой падеж) → номер дня недели, пн=0 … вс=6; «ё» уже заменена на «е»
    (r"понедельник\w*|пн", 0), (r"вторник\w*|вт", 1), (r"сред[аеуы]|ср", 2), (r"четверг\w*|чт", 3),
    (r"пятниц\w*|пт", 4), (r"суббот\w*|сб", 5), (r"воскресень\w*|вс", 6),
]
WEEKDAY_RX = [(re.compile(rf"(?<!\w)(?:{p})(?!\w)"), n) for p, n in WEEKDAYS]
NEXT_WEEK = re.compile(r"следующ\w*\s+(?:\w+\s+)?(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень|"
                       r"пн|вт|ср|чт|пт|сб|вс)|через\s+неделю")


def _weekday(s):
    for rx, n in WEEKDAY_RX:
        if rx.search(s):
            return n
    return None


def parse_when(text, base=None, max_days=60, grace_min=0):
    """«20:00», «20.30», «в 9», «завтра 18:30», «послезавтра в 7», «25.11 19:00», «через 40 минут / 2 часа / 3 дня»,
    «суббота 21:00», «в пятницу 20:00», «следующий понедельник 20:00»
    (время семьи, по умолчанию Москва) → datetime в UTC или None.
    grace_min — сколько минут «в прошлом» ещё считать этим временем (сбор, который только что начался);
    уже прошедшее сегодня время без «сегодня» — это завтра, прошедшая дата без года — следующий год."""
    s = " ".join(str(text or "").lower().replace("ё", "е").split())
    base = (base or dt.datetime.now(dt.timezone.utc)).astimezone(MSK)
    grace = dt.timedelta(minutes=grace_min)
    m = re.search(r"через\s+(\d+)?\s*(мин|час|ден|дня|дней|сут)", s)
    if m:
        n = int(m.group(1) or 1)
        unit = {"мин": "minutes", "час": "hours"}.get(m.group(2), "days")
        t = base + dt.timedelta(**{unit: n})
    else:
        tm = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", s)
        date = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)", s)
        if date and not tm and not date.group(3) and int(date.group(1)) <= 23 and int(date.group(2)) <= 59 \
                and len(date.group(2)) == 2:
            tm, date = date, None  # «20.30» без другого времени — это время, а не дата
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
        else:
            h = re.search(r"\bв\s+(\d{1,2})\b", s)
            if not h:
                return None
            hh, mm = int(h.group(1)), 0
        if hh > 23 or mm > 59:
            return None
        try:
            if date:
                y = date.group(3)
                y = (int(y) + 2000 if len(y) == 2 else int(y)) if y else base.year
                t = dt.datetime(y, int(date.group(2)), int(date.group(1)), hh, mm, tzinfo=MSK)
                if not date.group(3) and t < base - grace:
                    t = t.replace(year=t.year + 1)
            elif (wd := _weekday(s)) is not None:
                delta = (wd - base.weekday()) % 7
                t = (base + dt.timedelta(days=delta)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if delta == 0 and t < base - grace:
                    t += dt.timedelta(days=7)  # «во вторник 9:00», а сегодня вторник и уже 21:00
                if NEXT_WEEK.search(s):
                    t += dt.timedelta(days=7)  # «следующую субботу», «через неделю в субботу»
            else:
                day = 2 if "послезавтра" in s else 1 if "завтра" in s else 0
                t = (base + dt.timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if day == 0 and t < base - grace and "сегодня" not in s:
                    t += dt.timedelta(days=1)  # «в 20:00», а уже 21:00 — значит завтра
        except ValueError:
            return None
    if t < base - grace or t - base > dt.timedelta(days=max_days):
        return None
    return t.astimezone(dt.timezone.utc)
