# -*- coding: utf-8 -*-
"""Форум «Россия Онлайн» (XenForo): вход аккаунтом семьи, список тем раздела, посты темы, поиск фамилии семьи.
Бот на форуме ТОЛЬКО читает. Сеть — через core.kb (одна сессия на клиента, браузерные заголовки, повторы при
анти-флуде) плюс своя пауза между любыми двумя запросами. Разбор и поиск фамилии — чистые функции (есть тесты)."""
import asyncio
import random
import re
import time

from bs4 import BeautifulSoup

from .kb import LoginRequired, _clean_block, _get, _headers, new_session, stem

BASE = "https://forum.russia.online"
PAUSE = 2.0  # секунд между запросами к форуму — анти-флуд XenForo режет частые обращения кодом 403

# раздел форума (сервер «Арбатский») → (название, тип дела, статус тем в разделе)
SECTIONS = {
    1634: ("Жалобы на сотрудников госорганизаций", "жалоба", "pending"),
    1635: ("Жалобы на сотрудников госорганизаций · рассмотренные", "жалоба", "reviewed"),
    1513: ("Жалобы на игроков", "жалоба", "pending"),
    1514: ("Жалобы на игроков · рассмотренные", "жалоба", "reviewed"),
    1515: ("Жалобы на игроков · отклонённые", "жалоба", "rejected"),
    1517: ("Жалобы на лидеров фракций", "жалоба", "pending"),
    1518: ("Жалобы на лидеров фракций · рассмотренные", "жалоба", "reviewed"),
    1519: ("Жалобы на администрацию", "жалоба", "pending"),
    1520: ("Жалобы на администрацию · рассмотренные", "жалоба", "reviewed"),
    1490: ("Верховный суд", "иск", "pending"),
    1491: ("Верховный суд · рассмотренные", "иск", "reviewed"),
    1492: ("Апелляционный суд", "апелляция", "pending"),
    1493: ("Апелляционный суд · рассмотренные", "апелляция", "reviewed"),
    1494: ("Конституционный суд", "иск", "pending"),
    1495: ("Конституционный суд · рассмотренные", "иск", "reviewed"),
    1496: ("Районный суд", "иск", "pending"),
    1497: ("Районный суд · рассмотренные", "иск", "reviewed"),
    1486: ("Министерство юстиции / прокуратура", "прокуратура", "pending"),
    1487: ("Министерство юстиции / прокуратура · рассмотренные", "прокуратура", "reviewed"),
}
STATUS = {"pending": "🟡 на рассмотрении", "reviewed": "🟢 рассмотрено", "rejected": "⚫ отклонено",
          "closed": "🔒 закрыто"}
KIND_ICON = {"жалоба": "📝", "иск": "⚖️", "апелляция": "🔁", "прокуратура": "🏛"}


class LoginError(Exception):
    """Не удалось войти на форум (неверные данные, двухфакторка, капча) — без смены данных не повторяем."""


# ---------- разбор страниц ----------
def thread_id(href):
    m = re.search(r"/threads/(?:[^/?#]*\.)?(\d+)(?:/|$|\?|#)", href or "")
    return int(m.group(1)) if m else None


def thread_url(tid):
    return f"{BASE}/threads/{tid}/"


def _ts(el):
    try:
        return int(el.get("data-timestamp")) if el is not None else 0
    except (TypeError, ValueError):
        return 0


def parse_forum_list(html):
    """Страница раздела → [{id, title, url, author, start_ts, last_ts, prefix, locked}]."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.select(".structItem--thread"):
        links = [a for a in it.select(".structItem-title a") if thread_id(a.get("href"))]
        if not links:
            continue
        a = links[-1]  # первая ссылка бывает префиксом («Рассмотрено»), заголовок — последняя
        tid = thread_id(a["href"])
        start = it.select_one(".structItem-startDate time[data-timestamp]") or \
            it.select_one(".structItem-startDate [data-timestamp]")
        latest = it.select_one(".structItem-latestDate[data-timestamp]") or \
            it.select_one(".structItem-latestDate [data-timestamp]")
        out.append({
            "id": tid, "title": a.get_text(" ", strip=True), "url": thread_url(tid),
            "author": it.get("data-author") or "", "start_ts": _ts(start), "last_ts": _ts(latest) or _ts(start),
            "prefix": " ".join(x.get_text(" ", strip=True) for x in it.select(".structItem-title .label")),
            "locked": bool(it.select_one(".structItem-status--locked")),
        })
    return out


def parse_thread_page(html):
    """Страница темы → (meta {title, prefix, forum_id, locked, pages}, [{id, author, ts, text}])."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1.p-title-value")
    prefix = " ".join(x.get_text(" ", strip=True) for x in h1.select(".label")) if h1 else ""
    forum_id = None
    for a in soup.select(".p-breadcrumbs a[href*='/forums/']"):
        m = re.search(r"/forums/(?:[^/]*\.)?(\d+)/", a.get("href") or "")
        if m:
            forum_id = int(m.group(1))  # последняя крошка — раздел, где тема лежит сейчас
    pages = 1
    for a in soup.select(".pageNav-main a"):
        t = a.get_text(strip=True)
        if t.isdigit():
            pages = max(pages, int(t))
    locked = bool(soup.select_one(".blockStatus--locked")) or "Тема закрыта" in soup.get_text(" ", strip=True)[:20000]
    posts = []
    for art in soup.select("article.message--post") or soup.select("article.message"):
        pid = re.search(r"(\d+)", art.get("data-content") or art.get("id") or "")
        body = art.select_one(".message-body .bbWrapper") or art.select_one(".bbWrapper")
        t = art.select_one(".message-attribution-main time[data-timestamp]") or art.select_one("time[data-timestamp]")
        posts.append({"id": int(pid.group(1)) if pid else 0, "author": art.get("data-author") or "",
                      "ts": _ts(t), "text": _clean_block(body) if body else ""})
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    if prefix and title.startswith(prefix):
        title = title[len(prefix):].strip()
    return {"title": title, "prefix": prefix, "forum_id": forum_id, "locked": locked, "pages": pages}, posts


def status_from(forum_id, prefix="", locked=False):
    """Статус дела: раздел «рассмотренные/отклонённые» главнее, потом префикс темы, потом замок."""
    sec = SECTIONS.get(forum_id, ("", "", "pending"))[2]
    if sec != "pending":
        return sec
    p = (prefix or "").lower()
    if "отклон" in p or "отказ" in p:
        return "rejected"
    if any(w in p for w in ("рассмотрено", "решено", "одобрено", "удовлетвор")):
        return "reviewed"
    if locked or "закрыт" in p:
        return "closed"
    return "pending"


# ---------- кого ищем ----------
def norm(s):
    """ё→е, «_»→пробел, лишние пробелы — но строки сохраняем («Ответчик: …» разбирается построчно)."""
    s = str(s or "").replace("ё", "е").replace("Ё", "Е").replace("_", " ")
    return "\n".join(" ".join(ln.split()) for ln in s.splitlines() if ln.strip())


LAT1 = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
        "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "iu", "я": "ia"}


def lat_key(word):
    """Грубый ключ для сравнения имени кириллицей и латиницей: Феликс/Felix → «feli», Ваня/Vanya → «vani»."""
    w = "".join(LAT1.get(ch, ch) for ch in word.lower())
    return w.replace("y", "i").replace("j", "i").replace("ks", "x").replace("kh", "h")[:4]


TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "(?:y|i|j)",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "(?:h|kh)", "ц": "(?:c|ts)", "ч": "ch", "ш": "sh", "щ": "(?:sch|shch)", "ъ": "", "ы": "y",
            "ь": "", "э": "e", "ю": "(?:yu|iu|ju)", "я": "(?:ya|ia|ja)"}


def surname_stem(family_name):
    """«Ульяновы» → «Ульянов» (семья во мн. числе → фамилия)."""
    w = norm(family_name).split()
    w = w[-1] if w else ""
    return re.sub(r"(ы|и)$", "", w) if len(w) > 4 else w


def surname_rx(stem_):
    """Фамилия во всех формах: Ульянов, Ульянова, Ульянову, Ульяновым, Ульяновых, Ульяновы, Ульяновой, Ульянове,
    плюс латиницей (Ulyanov / Ulianov) — на форуме ники бывают и так."""
    s = norm(stem_).lower()
    if len(s) < 4:
        return None
    s = s.replace("\n", " ")
    lat = "".join(TRANSLIT.get(ch, re.escape(ch)) for ch in s)
    lat = lat.replace("l(?:ya|ia|ja)", "l(?:ya|ia|ja|a)")  # Ulyanov / Ulianov / Ulanov
    return re.compile(rf"(?<!\w)(?:{re.escape(s)}(?:а|у|ым|ых|ы|ой|ом|е)?|{lat}(?:a|u|ym|ykh|ih|y|oy|om|e)?)(?!\w)",
                      re.I)


HEAD_TITLES = "Лидер Правительства, Премьер-министр"


def titles_rx(titles):
    """«Лидер Правительства, Премьер-министр» → регулярка с падежами («лидера правительства», «премьер-министру»)."""
    parts = []
    for t in str(titles or "").split(","):
        words = norm(t).lower().split()
        if not words:
            continue
        parts.append(r"\s+".join(r"[\s\-]*".join(re.escape(stem(x)) + r"\w*" for x in w.split("-") if x)
                                 for w in words))
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")", re.I) if parts else None


# метки в строке «Ник нарушителя: …» / «Ответчик: …» и слова перед именем — кто есть кто
DEF_LABEL = re.compile(r"ответчик|обвиняем|нарушител|на кого|в отношении|подозреваем|ник сотрудника|"
                       r"ник игрока, на|ник лидера|ник администратора|обжалу|чьи действия|лицо, на|кого обвиня", re.I)
PLA_LABEL = re.compile(r"истец|заявител|ваш ник|ваш игровой|ваше имя|от кого|податель|потерпевш", re.I)
WIT_LABEL = re.compile(r"свидетел|очевид|третьи лица|присутствовал", re.I)
DEF_WORDS = re.compile(r"(?<!\w)(?:против|vs\.?|в отношении|обвиня\w*|обжал\w*|действия|иск к|сотрудника|игрока|"
                       r"проверить|привлечь|наказать|на)(?!\w)", re.I)
PLA_WORDS = re.compile(r"(?<!\w)(?:от|я,|истец|заявитель)(?!\w)", re.I)
WIT_WORDS = re.compile(r"(?<!\w)(?:свидетел\w*|очевид\w*|присутствовал\w*|вместе с|с ним был\w*)(?!\w)", re.I)


def _nearest(near):
    """Ближайшее к фамилии слово-маркер (не дальше трёх слов) → роль или None."""
    best, pos = None, -1
    for rx, role in ((DEF_WORDS, "defendant"), (PLA_WORDS, "plaintiff"), (WIT_WORDS, "witness")):
        for m in rx.finditer(near):
            if len(near[m.end():].split()) <= 3 and m.end() > pos:
                best, pos = role, m.end()
    return best


def _role_at(text, start, end):
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start:line_end if line_end >= 0 else len(text)]
    before_in_line = text[line_start:start]
    if ":" in before_in_line:
        label = before_in_line.split(":")[0]
        if DEF_LABEL.search(label):
            return "defendant"
        if PLA_LABEL.search(label):
            return "plaintiff"
        if WIT_LABEL.search(label):
            return "witness"
    near = before_in_line[-60:]
    if re.search(r"\bvs\.?\s*$", near, re.I) or (re.search(r"\bvs\b", line, re.I) and
                                                re.search(r"\bvs\b", before_in_line, re.I)):
        return "defendant"  # «Мавроди vs Ульянов» — после vs ответчик
    if re.search(r"\bvs\b", line[len(before_in_line):], re.I):
        return "plaintiff"  # «Ульянов vs Мавроди» — до vs истец
    return _nearest(near) or "mention"


ROLE_RANK = {"defendant": 3, "plaintiff": 2, "witness": 1, "mention": 0}


def find_person(people, text, around=None):
    """Кто из семьи упомянут: по статику в тексте, иначе по имени рядом с фамилией. people — [{name, static, …}]."""
    t = norm(text)
    for p in people:
        st = str(p.get("static") or "").strip()
        if st and re.search(rf"(?<!\d){re.escape(st)}(?!\d)", t):
            return p
    zone = norm(around if around is not None else text).lower()
    raw = re.findall(r"[a-zа-я]+", zone)
    words, lats = {stem(w) for w in raw}, {lat_key(w) for w in raw if len(w) >= 3}
    for p in people:
        first = norm(p.get("name")).lower().split()
        if first and len(first[0]) >= 3 and (stem(first[0]) in words or lat_key(first[0]) in lats):
            return p
    return None


def analyze(title, text, author, rx, people=(), head_rx=None, ignore=()):
    """Есть ли в теме член семьи и в какой роли. → None (нет) или
    {role, who (запись из people или None), name (как написано), head (дело против Главы по должности/имени)}."""
    full = norm(title) + "\n" + norm(text)
    low = full.lower()
    if any(w and norm(w).lower() in low for w in ignore):
        return None
    hits = []  # (начало, конец, по должности Главы, кто — если найден по статику)
    for m in (rx.finditer(full) if rx else []):
        hits.append((m.start(), m.end(), False, None))
    for m in (head_rx.finditer(full) if head_rx else []):
        hits.append((m.start(), m.end(), True, None))
    for p in people:
        st = str(p.get("static") or "").strip()
        if len(st) >= 4:
            for m in re.finditer(rf"(?<!\d){re.escape(st)}(?!\d)", full):
                hits.append((m.start(), m.end(), False, p))
    if not hits:
        return None
    best = None
    for s, e, by_title, preset in hits:
        role = _role_at(full, s, e)
        zone = full[max(0, s - 40):e + 10]
        who = preset or (None if by_title else find_person(people, zone, zone))
        name = full[max(0, s - 25):e].split("\n")[-1].split(":")[-1].strip()
        cand = {"role": role, "who": who, "name": name, "head": by_title or bool(who and who.get("head"))}
        if best is None or ROLE_RANK[role] > ROLE_RANK[best["role"]]:
            best = cand
    if best["role"] != "defendant" and author and rx and rx.search(norm(author)):
        best["role"] = "plaintiff" if best["role"] == "mention" else best["role"]  # тему подал сам член семьи
    if best["who"] is None and not best["head"]:
        best["who"] = find_person(people, full)
        best["head"] = bool(best["who"] and best["who"].get("head"))
    return best


def case_number(title):
    m = re.search(r"№\s*([0-9]+(?:[\-/][0-9]+)?)", title or "")
    return m.group(1) if m else ""


# ---------- сеть ----------
class ForumClient:
    """Сессия одного аккаунта: куки входа живут, пока жив клиент. Логин/пароль — только в памяти и в базе семьи."""

    def __init__(self, login, password):
        self.login, self.password = login, password
        self.session = None
        self.logged_in = False
        self.last = 0.0

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session, self.logged_in = None, False

    async def _pause(self):
        wait = PAUSE - (time.monotonic() - self.last)
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 0.6))
        self.last = time.monotonic()

    def _open(self):
        if self.session is None or self.session.closed:
            self.session, self.logged_in = new_session(), False

    async def sign_in(self):
        self._open()
        await self._pause()
        html = await _get(self.session, f"{BASE}/login/")
        if 'data-logged-in="true"' in html[:3000]:
            self.logged_in = True
            return
        tok = BeautifulSoup(html, "html.parser").select_one("input[name=_xfToken]")
        if not tok or not tok.get("value"):
            raise LoginError("форум не отдал форму входа — попробую позже")
        await self._pause()
        data = {"login": self.login, "password": self.password, "remember": "1", "_xfToken": tok["value"],
                "_xfRedirect": f"{BASE}/"}
        async with self.session.post(f"{BASE}/login/login", data=data, allow_redirects=True,
                                     headers={**_headers(f"{BASE}/login/"), "Origin": BASE}) as r:
            body = await r.text(errors="replace")
            where = str(r.url)
        if 'data-logged-in="true"' in body[:3000]:
            self.logged_in = True
            return
        if "two-step" in where or "two-step" in body[:30000]:
            raise LoginError("на аккаунте включена двухфакторная защита — для аккаунта бота её нужно выключить")
        low = body.lower()
        if "captcha" in low or "turnstile" in low:
            raise LoginError("форум попросил капчу — войди этим аккаунтом в браузере один раз и попробуй снова")
        err = BeautifulSoup(body, "html.parser").select_one(".blockMessage--error, .formRow-explain--error")
        raise LoginError((err.get_text(" ", strip=True)[:200] if err else "") or "неверный логин или пароль")

    async def get(self, url):
        """Страница форума под аккаунтом; вылетела сессия — один раз заходим заново."""
        if not self.logged_in:
            await self.sign_in()
        await self._pause()
        try:
            return await _get(self.session, url)
        except LoginRequired:
            self.logged_in = False
            await self.sign_in()
            await self._pause()
            return await _get(self.session, url)

    async def forum_page(self, fid, page=1):
        return parse_forum_list(await self.get(f"{BASE}/forums/{fid}/" + (f"page-{page}" if page > 1 else "")))

    async def thread(self, tid, latest=False):
        return parse_thread_page(await self.get(f"{BASE}/threads/{tid}/" + ("latest" if latest else "")))
