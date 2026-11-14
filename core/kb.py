# -*- coding: utf-8 -*-
"""База знаний для ИИ-юриста: тексты правил и законов, импорт с форума, поиск по ним."""
import io
import math
import re
from collections import Counter

import aiohttp
import discord
from bs4 import BeautifulSoup

from . import config
from .family import brand

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
    FULL_LIMIT = 60000  # если вся база меньше — отдаём ИИ целиком, это точнее

    def __init__(self, db):
        self.db = db
        self._chunks = {}  # guild_id -> (chunks, df, avg)

    # ---------- документы ----------
    def family_doc(self, gid):
        text = re.sub(r"\*\*|`", "", config.RULES_TEXT + "\n\n" + config.WELCOME_TEXT)
        text = brand(self.db, gid, text)
        return {"id": 0, "title": brand(self.db, gid, f"Правила семьи {config.FAMILY}"), "url": None, "category": "family",
                "content": text}

    def docs(self, gid, cats=None):
        """Документы семьи + общие для всех (guild_id=0, правила проекта)."""
        rows = [dict(r) for r in self.db.all("SELECT * FROM kb_docs WHERE guild_id IN (0, ?) ORDER BY id", gid)]
        rows.append(self.family_doc(gid))
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

    def total_chars(self, gid, cats=None):
        return sum(len(d["content"] or "") for d in self.docs(gid, cats))

    # ---------- поиск ----------
    def _build(self, gid):
        chunks = []
        for d in self.docs(gid):
            for i, c in enumerate(chunk_text(d["content"])):
                toks = tokens(d["title"] + " " + c)
                chunks.append({"doc_id": d["id"], "title": d["title"], "url": d["url"], "category": d["category"],
                               "text": c, "tf": Counter(toks), "len": len(toks) or 1, "n": i})
        df = Counter()
        for ch in chunks:
            df.update(set(ch["tf"]))
        avg = sum(c["len"] for c in chunks) / max(len(chunks), 1)
        self._chunks[gid] = (chunks, df, avg)
        return self._chunks[gid]

    def search(self, gid, query, k=10, cats=None):
        chunks, df, avg = self._chunks.get(gid) or self._build(gid)
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


async def _get(session, url):
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200:
            raise ValueError(f"Сайт ответил кодом {r.status}")
        return await r.text(errors="replace")


async def fetch_url(url, max_pages=10):
    """Скачивает тему форума (все посты автора, до max_pages страниц) или обычную страницу."""
    url = url.strip().split("#")[0]
    m = re.match(r"(https?://[^/]+/threads/[^/?#]+?)/?(?:page-\d+)?/?(?:\?.*)?$", url)
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        if m:
            base = m.group(1) + "/"
            html = await _get(s, base)
            title, author, posts, pages = parse_thread(html)
            for p in range(2, min(pages, max_pages) + 1):
                try:
                    _, _, more, _ = parse_thread(await _get(s, f"{base}page-{p}"), author)
                    posts += more
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
