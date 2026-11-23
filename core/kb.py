# -*- coding: utf-8 -*-
"""База знаний для ИИ-юриста: тексты правил и законов, импорт с форума, поиск по ним."""
import asyncio
import hashlib
import io
import logging
import math
import random
import re
from array import array
from urllib.parse import urlsplit
from collections import Counter

import aiohttp
import discord
from bs4 import BeautifulSoup

from . import config
from .family import brand, text as family_text

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
WORD = re.compile(r"[0-9]+(?:\.[0-9]+)+|[a-zа-яё0-9]+", re.I)
VOWELS = set("аеёиоуыэюяьй")
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот
от меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе
под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были
куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая
много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно
всю между это ли ах ох эх the a of to is""".split())
log = logging.getLogger("family-bot.kb")
VEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_vec(guild_id INTEGER NOT NULL, hash TEXT NOT NULL, vec BLOB NOT NULL,
                                  PRIMARY KEY(guild_id, hash));
"""
INDEX_BATCH = 300   # за один вопрос доиндексируем не больше стольких кусков (остальное — со следующими вопросами)
SMART_CHARS = 7000  # смысловой поиск точнее — ИИ хватает в 2–3 раза меньше текста, чем при поиске по словам
SMART_CHARS_CLAUDE = 18000  # у Claude длинный контекст: больше найденных фрагментов — полнее ответ юриста
WARM_BATCH = 400    # фоновая доиндексация за один заход (эмбеддинги Gemini бесплатны, но с лимитом в минуту)


def chunk_hash(ch):
    return hashlib.md5((ch["title"] + "\n" + ch["text"]).encode("utf-8")).hexdigest()


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def pack(v):
    return array("f", unit(v)).tobytes()


def unpack(b):
    a = array("f")
    a.frombytes(b)
    return a


HEADING = re.compile(r"^\s*(статья|глава|раздел|часть|пункт|§)\s*[0-9IVXLC]+|^\s*[0-9]{1,2}\.\s+\S|^\s*[0-9]{1,2}\.[0-9]+\.?\s",
                     re.I)


def stem(w):
    w = w.lower().replace("ё", "е")
    if any(c.isdigit() for c in w) or len(w) <= 3:
        return w
    w = w[:6]
    while len(w) > 3 and w[-1] in VOWELS:
        w = w[:-1]
    return w


def tokens(text):
    return [stem(t) for t in WORD.findall(text or "") if t.lower() not in STOP]


def chunk_text(text, size=1400):
    """Режем документ на куски, стараясь начинать новый кусок со статьи/пункта."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    chunks, cur = [], []
    cur_len = 0
    for ln in lines:
        if (HEADING.match(ln) and cur_len > 350) or cur_len + len(ln) > size:
            if cur:
                chunks.append("\n".join(cur).strip())
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


class KnowledgeBase:
    # Если вся база меньше — отдаём ИИ целиком (точнее). Больше — только подходящие куски: 60 тыс. знаков в каждом
    # вопросе (~20 тыс. токенов) съедали лимит, а резервный Groq (8 тыс. токенов/мин) отвечал ошибкой 413.
    FULL_LIMIT = 20000

    def __init__(self, db):
        self.db = db
        self._chunks = {}  # guild_id -> (chunks, df, avg)
        self._vec = {}     # (guild_id куска, hash) -> вектор (array): кэш смыслового поиска
        self._pruned = set()  # guild_id, у которых после пересборки уже почищены векторы удалённых кусков
        try:
            db.c.executescript(VEC_SCHEMA)
            db.c.commit()
        except Exception:  # noqa: BLE001 — база только для чтения в тестах и т.п.: просто без векторов
            log.exception("kb_vec: таблица не создана")

    # ---------- документы ----------
    def family_doc(self, gid):
        # свои правила и «о семье» из /настройки → «Тексты», иначе стандартные (под семью)
        rules = family_text(self.db, gid, "rules") or brand(self.db, gid, config.RULES_TEXT)
        welcome = family_text(self.db, gid, "welcome") or brand(self.db, gid, config.WELCOME_TEXT)
        text = re.sub(r"\*\*|`", "", rules + "\n\n" + welcome)
        return {"id": 0, "title": brand(self.db, gid, f"Правила семьи {config.FAMILY}"), "url": None, "category": "family",
                "content": text}

    def docs(self, gid, cats=None):
        """Документы семьи + общие для всех (guild_id=0, правила проекта)."""
        rows = [dict(r) for r in self.db.all("SELECT * FROM kb_docs WHERE guild_id IN (0, ?) ORDER BY id", gid)]
        rows.append({**self.family_doc(gid), "guild_id": gid})
        if cats:
            rows = [r for r in rows if r["category"] in cats]
        return rows

    def add(self, gid, title, url, category, content, by):
        now = discord.utils.utcnow().isoformat()
        if url:
            old = self.db.one("SELECT id FROM kb_docs WHERE guild_id=? AND url=?", gid, url)
            if old:
                self.db.q("UPDATE kb_docs SET title=?, category=?, content=?, updated_at=? WHERE id=?",
                          title, category, content, now, old["id"])
                self.invalidate()
                return old["id"], False
        cur = self.db.q("INSERT INTO kb_docs(guild_id,title,url,category,content,added_by,added_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", gid, title, url, category, content, by, now, now)
        self.invalidate()
        return cur.lastrowid, True

    def delete(self, gid, doc_id):
        self.db.q("DELETE FROM kb_docs WHERE guild_id=? AND id=?", gid, doc_id)
        self.invalidate()

    def invalidate(self):
        self._chunks = {}  # guild_id -> (chunks, df, avg)
        self._pruned = set()  # база поменялась — при следующем индексе почистить векторы исчезнувших кусков

    def total_chars(self, gid, cats=None):
        return sum(len(d["content"] or "") for d in self.docs(gid, cats))

    # ---------- поиск ----------
    def _build(self, gid):
        chunks = []
        for d in self.docs(gid):
            for i, c in enumerate(chunk_text(d["content"])):
                toks = tokens(d["title"] + " " + c)
                ch = {"doc_id": d["id"], "title": d["title"], "url": d["url"], "category": d["category"],
                      "text": c, "tf": Counter(toks), "len": len(toks) or 1, "n": i, "gid": d.get("guild_id", gid)}
                ch["hash"] = chunk_hash(ch)
                chunks.append(ch)
        df = Counter()
        for ch in chunks:
            df.update(set(ch["tf"]))
        avg = sum(c["len"] for c in chunks) / max(len(chunks), 1)
        self._chunks[gid] = (chunks, df, avg)
        return self._chunks[gid]

    def search(self, gid, query, k=10, cats=None):
        from .legal import asked_refs, exists, expand_query, title_fits
        chunks, df, avg = self._chunks.get(gid) or self._build(gid)
        refs = asked_refs(query)  # «ст. 12.3 УК» — кусок с этой статьёй должен быть первым
        query = expand_query(query)  # «УК», «ДМ», «ВС» → слова, которые есть в документах
        q = tokens(query)
        if not q or not chunks:
            return []
        N = len(chunks)
        nums = set(re.findall(r"\b\d+(?:\.\d+)*\b", query))
        scored = []
        for ch in chunks:
            if cats and ch["category"] not in cats:
                continue
            s = 0.0
            for t in q:
                f = ch["tf"].get(t)
                if not f:
                    continue
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * ch["len"] / avg))
            for n in nums:
                if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", ch["text"]):
                    s += 3
            for r in refs:
                if title_fits(r["code"], ch["title"]) and exists({**r, "code": ""}, [{"content": ch["text"]}]):
                    s += 12
            if s > 0:
                scored.append((s, ch))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]]

    def context(self, gid, query, cats=None, max_chars=18000):
        """Текст для ИИ + список источников."""
        docs = self.docs(gid, cats)
        total = sum(len(d["content"] or "") for d in docs)
        parts, sources = [], []
        if total <= self.FULL_LIMIT:
            for d in docs:
                parts.append(f"=== ДОКУМЕНТ: {d['title']} [{config.KB_CATEGORIES.get(d['category'], d['category'])}]"
                             f"{' — ' + d['url'] if d['url'] else ''} ===\n{d['content']}")
                sources.append((d["title"], d["url"]))
            return "\n\n".join(parts), sources, True
        used = 0
        for ch in self.search(gid, query, k=40, cats=cats):
            block = f"=== {ch['title']} [{config.KB_CATEGORIES.get(ch['category'], ch['category'])}] ===\n{ch['text']}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
            if (ch["title"], ch["url"]) not in sources:
                sources.append((ch["title"], ch["url"]))
        return "\n\n".join(parts), sources, False

    # ---------- смысловой поиск (эмбеддинги Gemini) ----------
    def _load_vecs(self, chunks):
        need = [c for c in chunks if (c["gid"], c["hash"]) not in self._vec]
        for gid in {c["gid"] for c in need}:
            for r in self.db.all("SELECT hash, vec FROM kb_vec WHERE guild_id=?", gid):
                self._vec[(gid, r["hash"])] = unpack(r["vec"])

    def prune(self, gid):
        """Удалить векторы кусков, которых больше нет (документ обновили/удалили). Вектор привязан к хэшу текста
        куска, так что изменённый кусок просто получит новый вектор, а старый тут уйдёт. → сколько удалено."""
        chunks = (self._chunks.get(gid) or self._build(gid))[0]
        removed = 0
        for g in {gid, 0}:  # общие документы (guild_id=0) входят в базу любой семьи целиком
            alive = {c["hash"] for c in chunks if c["gid"] == g}
            for r in self.db.all("SELECT hash FROM kb_vec WHERE guild_id=?", g):
                if r["hash"] not in alive:
                    self.db.q("DELETE FROM kb_vec WHERE guild_id=? AND hash=?", g, r["hash"])
                    self._vec.pop((g, r["hash"]), None)
                    removed += 1
        self._pruned.add(gid)
        return removed

    def missing(self, gid):
        """Сколько кусков базы семьи ещё без вектора."""
        chunks = (self._chunks.get(gid) or self._build(gid))[0]
        self._load_vecs(chunks)
        return sum(1 for c in chunks if (c["gid"], c["hash"]) not in self._vec)

    async def warm(self, gid, ai, limit=WARM_BATCH):
        """Фоновая доиндексация (цикл юриста): почистить старое, досчитать до limit новых векторов.
        Без ключа Gemini — ничего (поиск по словам). → сколько кусков проиндексировано."""
        if ai is None or not getattr(ai, "keys", {}).get("gemini"):
            return 0
        if gid not in self._pruned:
            self.prune(gid)
        chunks = (self._chunks.get(gid) or self._build(gid))[0]
        before = self.missing(gid)
        done = 0
        while done < limit:
            n = await self._index(chunks, ai, batch=min(INDEX_BATCH, limit - done))
            if not n:
                break
            done += n
        if before and done:
            log.info("База знаний %s: проиндексировано %d из %d кусков", gid, done, before)
        return done

    async def _index(self, chunks, ai, batch=INDEX_BATCH):
        """Досчитать векторы кускам, у которых их ещё нет (порциями, чтобы не встать надолго). → сколько посчитано."""
        self._load_vecs(chunks)
        todo = [c for c in chunks if (c["gid"], c["hash"]) not in self._vec][:batch]
        if not todo:
            return 0
        vecs = await ai.embed([f"{c['title']}\n{c['text']}" for c in todo])
        for c, v in zip(todo, vecs):
            if not v:
                continue
            b = pack(v)
            self.db.q("INSERT OR REPLACE INTO kb_vec(guild_id, hash, vec) VALUES(?,?,?)", c["gid"], c["hash"], b)
            self._vec[(c["gid"], c["hash"])] = unpack(b)
        return len(todo)

    async def semantic(self, gid, query, ai, k=12, cats=None):
        """Ближайшие по смыслу куски. Нет ключа Gemini / ошибка — None (тогда работает поиск по словам)."""
        if ai is None or not getattr(ai, "keys", {}).get("gemini"):
            return None
        chunks = (self._chunks.get(gid) or self._build(gid))[0]
        if cats:
            chunks = [c for c in chunks if c["category"] in cats]
        if not chunks:
            return None
        try:
            await self._index(chunks, ai)
            q = unit((await ai.embed([query], query=True))[0])
        except Exception as e:  # noqa: BLE001 — лимит, сеть: молча откатываемся на поиск по словам
            log.info("Смысловой поиск недоступен: %s", str(e)[:150])
            return None
        scored = []
        for c in chunks:
            v = self._vec.get((c["gid"], c["hash"]))
            if v is not None and len(v) == len(q):
                scored.append((sum(a * b for a, b in zip(q, v)), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]] or None

    async def context_smart(self, gid, query, ai, cats=None):
        """Как context(), но большая база — гибридом: смысл (эмбеддинги) + слова, объединённые по рангу (RRF).
        Текста ИИ уходит меньше (SMART_CHARS), а нужный пункт находится, даже если сказано другими словами."""
        docs = self.docs(gid, cats)
        if sum(len(d["content"] or "") for d in docs) <= self.FULL_LIMIT:
            return self.context(gid, query, cats)
        big = getattr(ai, "is_claude", False)
        sem = await self.semantic(gid, query, ai, k=24 if big else 12, cats=cats)
        if not sem:
            return self.context(gid, query, cats)
        words = self.search(gid, query, k=24 if big else 12, cats=cats)
        rank = {}
        for lst in (sem, words):
            for i, c in enumerate(lst):
                key = (c["doc_id"], c["n"])
                rank.setdefault(key, [c, 0.0])[1] += 1.0 / (60 + i)
        merged = [c for c, _ in sorted(rank.values(), key=lambda x: x[1], reverse=True)]
        parts, sources, used = [], [], 0
        cap = SMART_CHARS_CLAUDE if getattr(ai, "is_claude", False) else SMART_CHARS
        for ch in merged:
            block = f"=== {ch['title']} [{config.KB_CATEGORIES.get(ch['category'], ch['category'])}] ===\n{ch['text']}"
            if used + len(block) > cap:
                break
            parts.append(block)
            used += len(block)
            if (ch["title"], ch["url"]) not in sources:
                sources.append((ch["title"], ch["url"]))
        return "\n\n".join(parts), sources, False

    def sample(self, gid, cats=None, max_chars=20000):
        """Кусок базы для генерации вопросов экзамена."""
        import random
        built = self._chunks.get(gid) or self._build(gid)
        chunks = [c for c in built[0] if not cats or c["category"] in cats]
        random.shuffle(chunks)
        out, used = [], 0
        for c in chunks:
            if used + len(c["text"]) > max_chars:
                continue
            out.append(f"[{c['title']}]\n{c['text']}")
            used += len(c["text"])
        return "\n\n".join(out)


# ---------- импорт ----------
def _clean_block(el):
    for bad in el.select("script, style, noscript, .bbCodeBlock-expandLink, .bbCodeBlock-shrinkLink, "
                         ".js-unfurl, .bbImageWrapper, img, .message-lastEdit"):
        bad.decompose()
    for br in el.find_all("br"):
        br.replace_with("\n")
    for blk in el.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "table", "ul", "ol", "blockquote"]):
        blk.insert_after("\n")
    text = el.get_text()
    text = re.sub(r"[ \t ​]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_thread(html, author=None):
    """Разбор страницы темы XenForo: заголовок, автор, посты автора, число страниц."""
    soup = BeautifulSoup(html, "html.parser")
    t = soup.select_one("h1.p-title-value")
    title = t.get_text(" ", strip=True) if t else (soup.title.get_text(strip=True) if soup.title else "Документ")
    articles = soup.select("article.message--post") or soup.select("article.message")
    posts = []
    if articles:
        author = author or articles[0].get("data-author")
        for art in articles:
            if author and art.get("data-author") != author:
                continue
            body = art.select_one(".message-body .bbWrapper") or art.select_one(".bbWrapper")
            if body:
                txt = _clean_block(body)
                if txt:
                    posts.append(txt)
    pages = 1
    for a in soup.select(".pageNav-main a"):
        txt = a.get_text(strip=True)
        if txt.isdigit():
            pages = max(pages, int(txt))
    return title, author, posts, pages


def parse_generic(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "Документ"
    main = soup.select_one("main, article, #content, .content") or soup.body or soup
    return title, _clean_block(main)


LAWS_SECTION = "https://forum.russia.online/forums/zakonodatel-naya-baza.1471/"  # Referer для форума
RETRY_CODES = (403, 429)       # временный анти-флуд форума — пробуем ещё раз с паузой
RETRY_DELAYS = (3, 6, 12)      # 4 попытки: сразу, +3 с, +6 с, +12 с (плюс джиттер)
PAGE_PAUSE = 1.5               # пауза между страницами одной темы


class FetchBlocked(ValueError):
    """Сайт так и не пустил (403/429) после всех повторов — временно, стоит попробовать позже."""

    def __init__(self, status):
        super().__init__(f"Сайт ответил кодом {status} (анти-флуд) — временно недоступен, попробуй позже")
        self.status = status


def _headers(url):
    parts = urlsplit(url)
    referer = LAWS_SECTION if parts.netloc == "forum.russia.online" else f"{parts.scheme}://{parts.netloc}/"
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "gzip, deflate",  # br не просим — для него нужна отдельная библиотека
        "Connection": "keep-alive",
        "Referer": referer,
    }


class LoginRequired(ValueError):
    """Форум отдал страницу входа: раздел только для зарегистрированных (повторы не помогут)."""


def _is_login_wall(html):
    """403 от XenForo со страницей входа — это не анти-флуд, повторять бессмысленно."""
    head = html[:5000]
    return 'data-template="login"' in head or bool(re.search(r"<title>\s*(Вход|Log in)\b", head, re.I))


def new_session():
    """Сессия для скачивания документов: одна на прогон — общие куки и keep-alive."""
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))


async def _get(session, url):
    status = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            await asyncio.sleep(RETRY_DELAYS[attempt - 1] + random.uniform(0, 1.5))
        async with session.get(url, allow_redirects=True, headers=_headers(url)) as r:
            if r.status == 200:
                return await r.text(errors="replace")
            status = r.status
            if status not in RETRY_CODES:
                raise ValueError(f"Сайт ответил кодом {status}")
            if status == 403 and _is_login_wall(await r.text(errors="replace")):
                raise LoginRequired("Тема закрыта для гостей (форум просит войти) — владелец сервера может "
                                    "подключить общий аккаунт (/форум вход), или загрузи текст файлом: /база файл")
            retry_after = r.headers.get("Retry-After", "")
        if retry_after.isdigit() and attempt < len(RETRY_DELAYS):
            await asyncio.sleep(min(int(retry_after), 30))
    raise FetchBlocked(status)


async def _fetch(s, url, max_pages):
    m = re.match(r"(https?://[^/]+/threads/[^/?#]+?)/?(?:page-\d+)?/?(?:\?.*)?$", url)
    if m:
        base = m.group(1) + "/"
        html = await _get(s, base)
        title, author, posts, pages = parse_thread(html)
        for p in range(2, min(pages, max_pages) + 1):
            await asyncio.sleep(PAGE_PAUSE)
            try:
                _, _, more, _ = parse_thread(await _get(s, f"{base}page-{p}"), author)
                posts += more
            except FetchBlocked:
                raise  # не сохраняем полдокумента вместо целого — пусть останется старая версия
            except Exception:
                break
        content = "\n\n".join(posts).strip()
        if not content:
            raise ValueError("Не нашёл текст в теме — возможно, она закрыта для гостей.")
        return title, content, base
    html = await _get(s, url)
    title, content = parse_generic(html)
    if len(content) < 50:
        raise ValueError("На странице почти нет текста.")
    return title, content[:300000], url


async def check_public_url(url):
    """Только http(s) и только внешние адреса: ссылкой из /база нельзя заставить бота лезть во внутреннюю сеть
    хостинга (localhost, 10.x, 192.168.x, метаданные облака)."""
    import ipaddress
    import socket
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("нужна обычная ссылка http(s)://…")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("такой сайт не найден") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("это внутренний адрес — такие ссылки не скачиваю")


async def fetch_url(url, max_pages=10, session=None):
    """Скачивает тему форума (все посты автора, до max_pages страниц) или обычную страницу.
    session — общая сессия прогона (куки/keep-alive); без неё создаётся своя."""
    url = url.strip().split("#")[0]
    await check_public_url(url)
    if session is not None:
        return await _fetch(session, url, max_pages)
    async with new_session() as s:
        return await _fetch(s, url, max_pages)


def read_file(filename, data: bytes):
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if name.endswith((".html", ".htm")):
        return parse_generic(data.decode("utf-8", errors="replace"))[1]
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()
