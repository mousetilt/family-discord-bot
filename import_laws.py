import asyncio, re, sys
sys.path.insert(0, ".")
import aiohttp
from bs4 import BeautifulSoup
from core import config
from core.db import DB
from core.kb import KnowledgeBase, fetch_url, UA

SECTION = "https://forum.russia.online/forums/zakonodatel-naya-baza.1471/"


async def section_threads(url):
    out = []
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
        page, pages = 1, 1
        while page <= pages:
            async with s.get(url + (f"page-{page}" if page > 1 else "")) as r:
                soup = BeautifulSoup(await r.text(), "html.parser")
            for a in soup.select(".structItem-title a[href*='/threads/']"):
                href = "https://forum.russia.online" + a["href"] if a["href"].startswith("/") else a["href"]
                href = re.sub(r"(/threads/[^/]+/).*", r"\1", href)
                if href not in out:
                    out.append(href)
            for a in soup.select(".pageNav-main a"):
                if a.get_text(strip=True).isdigit():
                    pages = max(pages, int(a.get_text(strip=True)))
            page += 1
    return out


async def main():
    kb = KnowledgeBase(DB(config.DB_PATH))
    jobs = [(u, "laws") for u in await section_threads(SECTION)] + [(u, "ooc") for u in config.PROJECT_RULE_URLS]
    ok = 0
    for url, cat in jobs:
        try:
            title, content, u2 = await fetch_url(url)
            kb.add(title, u2, cat, content, 0)
            ok += 1
            print("OK ", cat, title[:60], len(content))
        except Exception as e:
            print("ERR", url, e)
    print(f"ГОТОВО: {ok}/{len(jobs)} документов, всего знаков: {kb.total_chars()}")

asyncio.run(main())
