import asyncio, sys
sys.path.insert(0, ".")
from core import config
from core.db import DB
from core.kb import KnowledgeBase, fetch_url
B = "https://forum.russia.online/threads/"
IDS = """kodeks-etiki-i-sluzhebnogo-povedeniya-gosudarstvennykh-sluzhashchikh-ro.39495 trudovoi-kodeks-ro.39496
kodeks-ob-administrativnykh-pravonarusheniyakh-ro.39488 federal-nyi-konstitutsionnyi-zakon-o-sudebnoi-sisteme-ro.36980
ugolovno-protsessual-nyi-kodeks-ro.39485 ugolovnyi-kodeks-ro.39487 konstitutsiya-ro.39472 pravila-dorozhnogo-dvizheniya-ro.39492
federal-nyi-zakon-o-statuse-territorii-i-osobykh-ob-yektakh-ro.41967 federal-nyi-zakon-o-sredstvakh-massovoi-informatsii.41960
federal-nyi-zakon-o-sledstvennom-komitete-ro.41966 federal-nyi-zakon-o-politsii.41965
federal-nyi-zakon-ob-advokat-skoi-deyatel-nosti-i-advokature-v-ro.41964 federal-nyi-zakon-o-predprinimatel-skoi-deyatel-nosti.41963
federal-nyi-zakon-o-statuse-meditsinskikh-uchrezhdenii.41962 federal-nyi-konstitutsionnyi-zakon-o-chrezvychainom-i-voyennom-polozhenii.41961
zakon-ob-oborote-oruzhiya-i-gosudarstvennykh-spetsial-nykh-sredstv-ro.40125 federal-nyi-zakon-o-vooruzhennykh-silakh-ro.40138
federal-nyi-zakon-o-federal-noi-sluzhbe-okhrany-ro.40135 federal-nyi-zakon-o-federal-noi-sluzhbe-bezopasnosti.40142
federal-nyi-zakon-o-general-noi-prokurature-ro.40094 federal-nyi-zakon-o-statuse-neprikosnovennosti-dolzhnostnykh-lits.40112
zakon-o-gosudarstvennykh-dokumentakh.40132 federal-nyi-konstitutsionnyi-zakon-o-pravitel-stve-ro.40082""".split()
async def main():
    kb = KnowledgeBase(DB(config.DB_PATH)); ok = 0
    for i in IDS:
        try:
            t, c, u = await fetch_url(B + i + "/"); kb.add(t, u, "laws", c, 0); ok += 1; print("OK ", t[:60], len(c))
        except Exception as e:
            print("ERR", i, e)
    print(f"ЗАКОНЫ: {ok}/{len(IDS)}, всего знаков в базе: {kb.total_chars()}")
asyncio.run(main())
