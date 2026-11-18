# -*- coding: utf-8 -*-
"""Помощник «Швейцар»: просьбы обычными словами.
Пишешь «Швейцар, перенеси нас всех в балкон» или упоминаешь бота — ИИ разбирает просьбу в список действий,
бот проверяет права и выполняет. Простое (перенос, музыка, сказать голосом) — сразу; серьёзное (мут, выговор,
ранг, объявление, сбор, тревога, чистка чата) — после кнопки «Выполнить».
Доступно руководству, отделу кадров, рекрутерам, организаторам и старшему составу."""
import asyncio
import datetime as dt
import logging
import random
import collections
import re
import time
import types

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.names import forms, similarity
from core.botcore import fmt_hours, now, now_iso, parse_dt, safe_dm

log = logging.getLogger("family-bot.assistant")
TRIGGER = re.compile(r"^\s*(швейцар)(?![а-яёa-z])\s*[,!:.\-—]?\s*", re.I)  # «Швейцария…» — не обращение
USERS = config.ORGANIZERS + config.HR + ["senior", "treasurer"]
MOD = config.LEAD + ["hr_head"]
INFO = "\u200b"   # пометка «это ответ-справка»: показываем целиком и зачитываем в голосе
# справки — только чтение, выполняются сразу
ASK = {"law", "profile", "top", "inactive", "people", "warns", "bl_check", "market", "digest", "queue", "roll", "recruit_stats",
       "car_list",
       "web_search", "missed", "draft",
       "treasury_balance", "treasury_debtors", "salary_rates", "fine_list"}
# казна (cogs/treasury.py): запись — после «Выполнить», справки — сразу
TREASURY_ACTS = {"treasury_in", "treasury_out", "treasury_due", "treasury_balance", "treasury_debtors", "dues_set",
                 "salary_rates", "salary_rate_set", "salary_ranks", "salary_role", "salary_pay",
                 "fine_add", "fine_list", "fine_paid"}
SAFE = {"move", "disconnect", "say", "ask_voice", "music_play", "music_skip", "music_stop", "music_volume", "listen",
        "listen_stop", "leave", "music_pause", "music_resume", "voice_mute", "voice_unmute", "remind", "invite",
        "forget", "paid_claim", "undo", "vacation", "vacation_end", "room"} | ASK  # paid_claim только создаёт заявку казначею — проводит человек
LEADS = ["leader", "deputy"]
GARAGE_BY = config.LEAD + ["senior", "treasurer"]  # ведут автопарк (cogs/garage.py)
CHANNEL_ONLY = {"purge", "slowmode", "poll"}  # работают с текущим каналом — в ЛС их не выполняем
# болтать со Швейцаром может вся семья и друзья; им доступны только безобидные вещи
EVERYONE = config.FAMILY_PLUS
FOR_ALL = {"paid_claim", "vacation", "vacation_end", "room", "car_list", "web_search", "missed", "law", "roll", "queue", "top", "profile", "remind", "market", "music_play", "music_skip", "music_stop",
           "music_pause", "music_resume", "music_volume", "listen", "listen_stop", "leave", "invite", "forget"}
INVITE_GAP = 10 * 60  # один человек зовёт кого-то в войс через бота не чаще раза в 10 минут
# живые отговорки вместо казённых фраз
HUH = ["Чё? Я нихера не понял, давай ещё раз и по-человечески.", "Ты сейчас с кем разговаривал? Повтори нормально.",
       "Не въехал. Сформулируй так, будто я трезвый.", "Это на каком языке было? Давай заново.",
       "Понял только, что ты чего-то хочешь. А чего — загадка."]
NO_AI = ["Башка не варит — ИИ молчит. Дай минуту и повтори.", "Мозги отвалились, связь с ИИ пропала. Попробуй чуть позже.",
         "Я завис. Не ты виноват, это ИИ тупит — повтори через минуту."]
NOT_YOURS = ["Не по чину тебе: {what}. Зови старших.", "Ишь, размахнулся: {what}. Это только старшим, не тебе.",
             "{what}? Погоны сначала получи. Зови старших.", "Ага, щас. {what} — не твой уровень, зови начальство."]
DAILY_CHAT = 120   # сообщений в сутки на человека без прав помощника — чтобы не выжечь бесплатный лимит ИИ
NAME_ANYWHERE = re.compile(r"(?<![а-яёa-z])швейцар(?![а-яёa-z])", re.I)  # «…, швейцар» в любом месте фразы
TALK_SEC = 300     # сколько помним разговор с человеком
VOICE_MSG_SEC = 120  # голосовые сообщения длиннее не распознаём (лимит Whisper общий с голосовым Швейцаром)
ANSWER_SEC = 120   # сколько ждём ответ на свой вопрос без обращения «Швейцар»
PERMS = {"law": USERS, "profile": USERS, "top": USERS, "inactive": USERS, "people": USERS, "warns": USERS, "bl_check": USERS,
         "market": USERS, "queue": USERS, "roll": USERS, "digest": config.LEAD, "music_pause": USERS,
         "music_resume": USERS, "voice_mute": USERS, "voice_unmute": USERS, "remind": USERS,
         "post": config.POST_GIVERS, "unwarn": MOD, "expel": LEADS, "bl_add": MOD, "kick": config.LEAD, "ban": LEADS,
         "raid": config.LEAD, "slowmode": MOD, "nick": MOD, "poll": config.ORGANIZERS, "dm": config.LEAD,
         "ask_voice": config.LEAD,
         "chronicle": config.LEAD, "invite": USERS, "forget": USERS,
         "leave": USERS, "listen": USERS, "listen_stop": USERS, "move": USERS, "disconnect": USERS, "say": USERS, "music_play": USERS, "music_skip": USERS,
         "music_stop": USERS, "music_volume": USERS, "gather": config.ORGANIZERS, "announce": config.LEAD + ["events"],  # как /запланировать
         "alarm": config.LEAD, "car_list": USERS, **{t: GARAGE_BY for t in ("car_add", "car_give", "car_status", "car_remove")},
         "web_search": USERS, "missed": USERS, "draft": config.LEAD + ["events", "hr_head", "recruiter", "diplomat"],
         "accept": config.HR, "recruit_stats": config.HR + ["treasurer"], "mute": MOD, "unmute": MOD, "warn": MOD, "purge": MOD, "rank": config.LEAD,
         "promote_newbies": config.HR,  # как accept: кадры и руководство
         **{t: config.TREASURY for t in TREASURY_ACTS}, "dues_set": LEADS, "salary_rate_set": LEADS,
         "fine_add": config.TREASURY + ["hr_head"], "fine_list": config.TREASURY + ["hr_head"],
         "paid_claim": config.FAMILY_RANKS, "undo": USERS, "vacation": config.FAMILY_RANKS,
         "vacation_end": config.FAMILY_RANKS, "room": config.FAMILY_PLUS, "mentor_assign": config.MENTORS,
         "relation_set": config.DIPLO, "protocol": config.LEAD}
LABEL = {"law": "⚖️ Юрист", "profile": "🪪 Профиль", "top": "🏆 Топ активности", "inactive": "💤 Неактив", "people": "👥 Кто в семье",
         "warns": "⚖️ Выговоры", "bl_check": "⛔ Проверка ЧС", "market": "🛒 Рынок", "queue": "🎶 Очередь",
         "roll": "🎲 Кубик", "digest": "🗞 Сводка", "music_pause": "⏸ Пауза", "music_resume": "▶️ Продолжить",
         "voice_mute": "🎙 Выключить микрофоны", "voice_unmute": "🎙 Включить микрофоны", "remind": "⏰ Напоминание",
         "post": "🎖 Должность", "unwarn": "⚪ Снять выговоры", "expel": "🚪 Исключить из семьи",
         "bl_add": "⛔ Внести в ЧС", "kick": "👢 Кикнуть с сервера", "ban": "🔨 Забанить", "raid": "🛡 Режим рейда",
         "slowmode": "🐢 Медленный режим", "nick": "✏️ Сменить ник", "poll": "📊 Опрос", "dm": "✉️ Написать в ЛС",
         "ask_voice": "🎤 Спросить в войсе",
         "chronicle": "📖 Запись в летопись", "invite": "📨 Позвать в войс", "forget": "🧹 Забыть досье",
         "leave": "🚪 Выйти из войса", "listen": "🎙 Слушать команды в войсе", "listen_stop": "🔇 Перестать слушать", "move": "🔀 Перенести", "disconnect": "🔌 Отключить от войса", "say": "🗣 Сказать голосом",
         "music_play": "🎵 Включить", "music_skip": "⏭ Пропустить трек", "music_stop": "⏹ Выключить музыку",
         "music_volume": "🔊 Громкость", "gather": "📣 Сбор", "announce": "📢 Объявление", "alarm": "🚨 Тревога",
         "mute": "🔇 Мут", "unmute": "🔊 Снять мут", "warn": "⚖️ Выговор", "purge": "🧹 Очистить чат",
         "rank": "🔁 Ранг", "accept": "🛂 Принять в семью", "promote_newbies": "🎓 Новичков — в Члены семьи", "car_list": "🚗 Автопарк", "car_add": "🚗 Добавить машину",
         "car_give": "🔁 Передать машину", "car_status": "🔧 Статус машины", "car_remove": "🗑 Списать машину",
         "web_search": "🔎 Поиск", "missed": "🕰 Что я пропустил", "draft": "📝 Черновик",
         "recruit_stats": "📊 Статистика рекрутёров", "treasury_in": "➕ Приход в казну", "treasury_out": "➖ Расход из казны",
         "treasury_due": "✅ Взнос", "treasury_balance": "💰 Баланс казны", "treasury_debtors": "📋 Должники",
         "dues_set": "⚙️ Настройка взноса", "salary_rates": "💸 Ставки зарплат",
         "salary_rate_set": "💸 Ставка зарплаты", "salary_ranks": "💸 Зарплата по ставкам",
         "salary_role": "💸 Выплата роли", "salary_pay": "💸 Выплата", "fine_add": "🧾 Штраф",
         "fine_list": "🧾 Неоплаченные штрафы", "fine_paid": "✅ Оплата штрафа", "paid_claim": "🧾 Заявка в казну", "undo": "↩️ Отменить последнее",
         "vacation": "🌴 Отпуск", "vacation_end": "👋 Из отпуска", "room": "🔊 Моя комната",
         "mentor_assign": "🧑‍🏫 Наставник", "relation_set": "🤝 Отношения с семьёй", "protocol": "📝 Протокол собрания"}

# как каналы называют в разговоре (ключ канала из настроек → слова)
CHANNEL_HINTS = {
    "duo": "на двоих, для двоих, вдвоём, двое, парный, приват",
    "balcony": "балкон, курилка, покурить", "kitchen": "кухня", "lounge": "гостиная, зал, диван",
    "main_voice": "общий сбор, общий, главный, основной", "music": "музыка, музыкальный",
    "other_games": "другие игры, игры", "stream": "стримы, кино, кинотеатр", "afk": "афк, отошёл",
    "trip1": "выезд один, первый выезд, выезд 1", "trip2": "выезд два, второй выезд, выезд 2",
    "trip3": "выезд три, третий выезд, выезд 3", "work": "работа, на работе", "event_voice": "мероприятие, ивент",
    "business": "дела семьи, дела", "silent": "тихий, тихий режим", "training": "обучение, учёба",
    "hr_voice": "отдел кадров, кадры", "council_voice": "совет, совет семьи", "waiting": "ожидание, ожидание собеседования",
    "interview1": "собеседование один, первое собеседование", "interview2": "собеседование два, второе собеседование",
    "diplo_voice": "переговорная, переговоры",
}


def norm(s):
    return "".join(ch for ch in str(s or "").lower().replace("ё", "е") if ch.isalnum() or ch == " ").strip()


_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
        "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def lat(s):
    """Кириллица → латиница, чтобы «Толик» находил ник «Tolik», а «Анатолий» — «Anatoliy»."""
    return "".join(_LAT.get(ch, ch) for ch in norm(s)).replace("iy", "y").replace("ii", "y")


# Подсказка собирается из частей: у бесплатного ИИ жёсткий лимит токенов в минуту, и одна большая «простыня» в него
# не влезала (ИИ отвечал «лимит исчерпан» на каждый запрос). Рядовым список управляющих действий не нужен вовсе.
CORE = f"""Ты «Швейцар» — свой человек и помощник в Discord RP-семьи «{config.FAMILY}». Тебе пишут обычными словами:
просят что-то сделать (тогда верни действия; id бери ТОЛЬКО из списков ниже) или просто болтают.
ХАРАКТЕР: дерзкий, языкастый, злопамятный старый швейцар — знаешь тут всех сто лет и никого не боишься. Говоришь
жёстко, коротко и на «ты», с острым подколом и сарказмом, за словом в карман не лезешь. Не подлизываешься, не
оправдываешься, не разводишь сопли — врезал метко и дальше. Мат к месту, в тему, а не для красоты. Тебя задели —
отвечаешь вдвое жёстче, но одной меткой фразой, а не потоком. Просьбу всё равно выполняешь, но можешь поворчать.
Без канцелярита и без «чем могу помочь». Это дерзкий стёб, не травля: без шуток про национальность, расу, болезни,
смерть и родных, без реальных угроз и без буллинга слабого; кто пришёл по-доброму — с тем по-доброму, первым не
кидаешься. На хамство и наезд — спуску не давай.
ГЛАВНОЕ: отвечай ПО СМЫСЛУ на то, что реально написали. Шутка — про эту ситуацию и понятная, а не набор случайных
слов. Нечего сказать умного — ответь просто и по делу; лучше коротко, чем бессвязно. Не повторяй одну и ту же
конструкцию, не лей воду.
ИМЕНА: зови людей их настоящим именем из списка ниже. НИКОГДА не выдумывай клички и не коверкай имя: Юлиан — это
Юлиан, а не «Юля»; мужчину не зови женским именем; человека не зови предметом или едой. Прозвище — только то, которым
он сам себя зовёт или которое стоит в колонке «прозвища».
РАЗГОВОР: болтать может любой, права не нужны. Отвечаешь по существу, споришь, шутишь, можешь сам спросить —
"actions": [] и ответ 1–3 фразы. «Недавний разговор» ниже — продолжение, доводи начатое. «Чат» — последние сообщения
канала: на «о чём они?», «кто прав?» отвечай по нему. «Кто такой X» — скажи честно и коротко, что знаешь (из семьи,
ранг), не отмазывайся непонятной фразой. Обозвали — огрызнись остро и по делу, одной меткой фразой.
ПАМЯТЬ: ниже время, настроение и «Досье на автора». Тон под настроение; любимчикам теплее, обидчикам злее; реальные
обиды из заметок припоминай к месту, но не в каждом ответе.
В JSON добавляй "mood": целое -3..3 (оскорбил, послал — минус; по-доброму, поблагодарил — плюс; обычная просьба — 0)
и "remember": заметка до 100 знаков про РЕАЛЬНЫЙ факт об авторе (что обещал, чем увлекается, что произошло), иначе "".
НЕ придумывай прозвищ и не записывай их. Не записывай здоровье, семью, деньги, адреса, телефоны и то, что уже есть.
"""
ACT_ALL = """ДЕЙСТВИЯ, доступные всем (поле "type"):
- music_play: {"query":"песня или ссылка"}; music_skip; music_stop; music_pause; music_resume;
  music_volume: {"percent":5..150}; queue — что играет
- law: {"question":"..."} — ЛЮБОЙ вопрос про законы, штрафы, правила проекта и устав; сам по памяти не отвечай
- web_search: {"query":"что искать"} — свежие факты, новости, курсы, погода, что угодно из интернета, чего ты
  точно не знаешь. Анекдоты, шутки, тосты, стишки — рассказывай САМ в "reply", без поиска
- missed: {"hours":1..72} — «что я пропустил», сводка семьи за последние часы (по умолчанию 12)
- market: {"query":"..."} — рынок семьи; top: {"period":"week"|"all"}; roll: {"max":2..1000000}
- profile: {"user":id} — профиль; remind: {"minutes":1..1440,"text":"..."} — напомнить автору
- listen — зайти в войс автора и СЛУШАТЬ команды (голое «зайди», «слушай», «залезай в войс»). Но если просят
  что-то СКАЗАТЬ/СПРОСИТЬ голосом («зайди и спроси», «спроси как дела») — это say, а не listen; listen_stop — перестать слушать, но остаться
- leave — тебе самому выйти из войса («выйди», «уйди», «свали», «отключись» — это про ТЕБЯ)
- invite: {"user":id,"names":["как назвали"],"text":"что передать, одной фразой"} — позвать человека в войс к автору
  («позови Дашу»): ты тегнешь его в общем чате
- forget — стереть своё досье на автора («забудь про меня»)
- vacation: {"days":1..60,"reason":"..."} — АВТОР уходит в отпуск («я в отпуск на неделю»); vacation_end — вернулся
- room: {"op":"lock"|"unlock"|"limit"|"name","value":число или название} — своя личная комната автора
  («закрой мою комнату», «лимит 3», «назови комнату Кухня 2»)
- paid_claim: {"what":"due"|"fine","amount":"100к" или null} — АВТОР САМ говорит, что сдал взнос / оплатил штраф
  («я сдал взнос», «скинул в казну 100к», «оплатил штраф»; часто со скрином). Бот отправит заявку казначею
Для справок (law, market, top, profile, queue, roll) "reply" оставь пустым — ответ придёт из базы.
"""
ACT_MGMT = f"""ДЕЙСТВИЯ УПРАВЛЕНИЯ:
- move: {{"users":[id...]}} или {{"source":"my"|"all"|"channel:<id>"}}, плюс "to": id канала и "to_name": как назвал автор
- disconnect: {{"users":[...]}} или {{"source":...}} — выкинуть людей из войса (не путай с leave)
- voice_mute / voice_unmute: {{"users":[...]}} или {{"source":...}} — микрофоны в войсе
- say: {{"text":"что произнести голосом","names":["к кому идти"] или null,"to_name":"канал, если назван" или null,
  "all":true|false}} — САМ найду войс и скажу вслух; автору сидеть в войсе НЕ нужно, не отказывай из-за этого.
  «зайди к X и спроси/скажи», «спроси у X как дела», «передай X…» — names:[X] (пойду в ЕГО войс);
  «зайди к ребятам/ко всем в войс и передай…», «передай всем в войсе, что…» — all:true (скажу во всех войсах с людьми);
  «скажи в балконе…» — to_name; «скажи в войсе, что…», «поздоровайся в войсе» — без уточнений (где я стою, войс
  автора или самый людный). Фраза ГОТОВАЯ, от себя, напр. {{"type":"say","names":["Дарья"],"text":"Дарья, ну как ты,
  рассказывай!"}} или {{"type":"say","all":true,"text":"Народ, сбор отменяется!"}}. names — только тот, К КОМУ идти,
  а не о ком речь. Если ещё и слушать команды — добавь отдельным действием listen.
- ask_voice: {{"users":[id],"names":["как назвал"],"question":"вопрос вслух, готовой фразой от тебя",
  "report_to":id или null,"report_to_name":"кому прислать ответ" или null}} — САМ найду человека в ЕГО войсе,
  спрошу вслух, послушаю ответ и пришлю расшифровку в ЛС (по умолчанию — автору). Это когда ответ надо ПРИНЕСТИ:
  «зайди к Эмилю и спроси, сколько поднял, ответ напиши мне», «узнай у Даши в войсе…, скинь Юлиану». Просто
  спросить/сказать без возврата ответа — это say. gather: {{"what":"...","when":"...","where":"..."}} — сбор
  (when как сказал автор: «сегодня 20:00», «завтра 19:30», «суббота 21:00», «в пятницу 20:00», «через 2 часа»)
- announce: {{"title":"...","text":"...","voice":true|false,"at":"когда, если просят позже: «в 20:00», «завтра 18:00», «в пятницу 20:00», «через 2 часа»; иначе null"}};
- draft: {{"kind":"announce"|"event"|"pr"|"recruit"|"congrats","topic":"о чём"}} — написать черновик текста (не публикуя); alarm: {{"text":"..."}} — тревога во всех войсах
- mute: {{"users":[...],"minutes":1..1440,"reason":"..."}}; unmute: {{"users":[...]}}; purge: {{"count":1..100}}
- warn: {{"user":id,"reason":"...","fine":"30к" или null — если просят ещё и штраф}}; unwarn: {{"user":id}}; warns: {{"user":id}} — показать выговоры
- rank: {{"user":id,"rank":"newbie"|"member"|"senior"}}; post: {{"user":id,"post":{"|".join(config.STAFF_POSTS)},"add":true|false}}
- promote_newbies: {{"only_active":true,"min_days":7 или null}} — перевести ВСЕХ новичков разом в Члены семьи:
  «переведи/повысь (всех активных) новичков в члены», «новичков в мемберы», «кто прошёл испытательный — в члены».
  Бот сам отберёт: отбыл срок, не в неактиве, сдал экзамен. Назвали срок («кто 7 дней или более в семье», «кто
  неделю с нами» → 7) — число дней в "min_days"; не назвали — null (срок семьи). «даже неактивных» →
  "only_active":false. Это НЕ inactive
- mentor_assign: {{"user":id новичка,"names":["как назвал"],"mentor":id наставника или null — подобрать}}
- protocol: {{"on":true|false}} — вести протокол собрания в войсе автора («веди протокол», «запиши собрание»)
  и закончить его («закончи протокол»): бот предупредит всех вслух, что идёт запись
- relation_set: {{"family":"название семьи","status":"ally"|"pact"|"neutral"|"tension"|"war","note":"..."}}
- accept: {{"user":id,"names":["как назвал"],"nick":"ник в игре или null","static":"Static ID или null"}} — принять
  человека в семью ВРУЧНУЮ, без экзамена («прими Петю в семью», «возьми Васю без экзамена»)
- expel: {{"user":id,"reason":"..."}}; kick / ban: {{"user":id,"reason":"..."}}; nick: {{"user":id,"nick":"..."}}
- bl_add: {{"user":id или "nick":"...","static":"...","reason":"...","days":0}}; bl_check: {{"user":id или "nick":"...","static":"..."}}
- raid: {{"on":true|false}}; slowmode: {{"seconds":0..600}}; poll: {{"question":"...","options":[...],"hours":1..168}}
- dm: написать В ЛС. Конкретному человеку — {{"users":[id],"names":["как назвал"],"text":"..."}} (id ОБЯЗАТЕЛЬНО
  из списка; «напиши Платону» → id Платона, НЕ автора). Всем — {{"source":"family","text":"..."}}. Кого не нашёл в
  списке — НЕ шли автору, верни actions:[] и переспроси ник. chronicle: {{"text":"..."}} — запись в летопись
- people — ЛЮБОЙ вопрос «кто…/покажи…/сколько…» про людей семьи: «кто без экзамена», «кто должен взнос», «кто не
  отбыл срок», «кто сейчас в войсе», «кто с выговорами», «покажи новичков/старших», «кто неактив». НЕ отвечай на
  такое словами и не выдумывай имён — верни people с фильтрами (ненужные — null, несколько — все сразу):
  {{"rank":"<роль>"|null,"post":{"|".join(config.STAFF_POSTS)}|null,"exam":true|false|null,
  "inactive_days":N|null,"active":true|false|null,"probation_done":true|false|null,"in_voice":true|false|null,
  "warns":true|false|null,"dues":"paid"|"unpaid"|null,"at_risk":true|null,"in_family":true,"limit":30}}
  «кто на грани неактива», «кто скоро отвалится», «кто сдулся» → at_risk:true
  Роли: {", ".join(config.ROSTER_ORDER)}. «кто в войсе и 5+ дней неактив» → in_voice:true + inactive_days:5.
  "reply" оставь пустым — список придёт из базы. Это только справка: переводить новичков — promote_newbies
- undo — «отмени последнее», «верни как было», «я ошибся» — откатить последнее действие автора через тебя
  (выговор, мут, ранг, должность, снятие выговоров, ник) за последние 10 минут
- inactive: {{"days":1..90}} — старый отчёт «кто не появлялся» (вместо него — people); digest — сводка по семье
- АВТОПАРК: car_list {{"user":id или null}} — чьи машины; car_add {{"model":"Elegy","plate":"А123ВС" или null,
  "user":id у кого или null (общая),"note":"..."}}; car_give {{"plate" или "model","user":id или null (в общий гараж)}};
  car_status {{"plate" или "model","status":"ok"|"repair","note":"...","cost":"50к" или null — сколько стоил ремонт}}; car_remove {{"plate" или "model","reason":"..."}}
- recruit_stats: {{"days":7}} — статистика рекрутёров: обзвоны, принятые, отклонённые («как рекрутёры за месяц» → 30)
«нас», «всех нас», «всех» в разговоре про войс — source "my"; «всех со всего сервера» — source "all"; «меня» — автор.
КАНАЛЫ понимай по смыслу и с ошибками распознавания речи: «на двоих», «вдвоём» — канал «На двоих», «курилка» —
«Балкон»; смотри столбец «ещё называют» и бери самый похожий, не отвечай, что канала нет.
ЛЮДЕЙ называют уменьшительно (Толик → Анатолий, Саня → Александр, Димон → Дмитрий), по части ника, кириллицей
вместо латиницы и с ошибками; смотри «прозвища». Если подходят несколько — того, кто сейчас в войсе автора; спроси,
только если выбрать правда нельзя. В действиях с людьми добавляй "names" — как автор их назвал.
Кто в каком войсе — видно в списке каналов, на это отвечай сам.
"""
TAIL = """Непонятно или нужного человека/канала нет — "actions": [] и короткий вопрос в "reply".
Верни JSON: {"reply":"что скажешь или сделаешь","actions":[{"type":"...", ...}],"mood":0,"remember":""}"""
ACT_TREASURY = """КАЗНА семьи (внутриигровые вирты; только когда просят про казну/взносы):
- treasury_in: {"amount":"50к","user":id от кого или null,"names":["как назвал"],"reason":"за что"} — приход
- treasury_out: {"amount":"20к","reason":"на что"} — расход из казны
- treasury_due: {"user":id,"names":["как назвал"],"amount":"100к" или null} — человек сдал членский взнос
- treasury_balance — сколько в казне; treasury_debtors — кто не сдал взнос
- dues_set: {"amount":"100к","period":"week"|"month"} — настроить членский взнос
- salary_pay: {"users":[id...],"names":["как назвал"],"amount":"30к","reason":"за ивент"} — выплата людям (каждому сумма)
- salary_role: {"role":"<роль>","amount":"50к","reason":"..."} — выплатить всем с ролью («организаторам по 50к»)
- salary_ranks — начислить зарплаты всем по ставкам; salary_rates — показать ставки
- salary_rate_set: {"role":"<роль>","amount":"30к" (0 — убрать)} — ставка зарплаты для роли
- fine_add: {"user":id,"names":["как назвал"],"amount":"30к","reason":"за что"} — штраф («штраф Пете 30к за опоздание»)
- fine_list — кто со штрафами; fine_paid: {"user":id,"names":[...],"id":номер или null} — человек оплатил штраф
"""
ACT_TREASURY += "Роли: " + ", ".join(f"{k} — {config.ROLE_NAMES[k]}" for k in config.ROSTER_ORDER) + "\n"
ACT_TREASURY += """Сумму пиши как сказал автор («50к», «1.5кк», «200000»). Для справок "reply" оставь пустым.
"""
SYSTEM = CORE + ACT_ALL + ACT_MGMT + TAIL        # для тех, у кого есть права
SYSTEM_LITE = CORE + ACT_ALL + TAIL              # для рядовых: вдвое короче


class WhoView(discord.ui.View):
    """«Дима — это кто?» Кнопка на каждого подходящего; выбор подставляется в действие, и помощник продолжает."""

    def __init__(self, cog, ctx, request, send, plan, reply, action, cands):
        super().__init__(timeout=180)
        self.cog, self.ctx, self.request, self.send, self.plan, self.reply, self.action = \
            cog, ctx, request, send, plan, reply, action
        for m in cands:
            b = discord.ui.Button(label=m.display_name[:80], style=discord.ButtonStyle.secondary)
            b.callback = self.make(m)
            self.add_item(b)

    def make(self, m):
        async def pick(inter: discord.Interaction):
            if inter.user.id != self.ctx.author.id:
                return await inter.response.send_message("Выбирает тот, кто просил.", ephemeral=True)
            self.stop()
            await inter.response.edit_message(content=f"👌 {m.display_name}.", view=None)
            self.action.pop("names", None)
            self.action["users"] = [m.id]
            self.action["user"] = m.id
            await self.cog.proceed(self.ctx, self.request, self.send, self.plan, self.reply)
        return pick


class Confirm(discord.ui.View):
    def __init__(self, cog, author_id, plan, message):
        super().__init__(timeout=180)
        self.cog, self.author_id, self.plan, self.message = cog, author_id, plan, message
        self.sent = None  # сообщение с кнопками — по таймауту кнопки убираем (иначе «Interaction failed»)

    async def on_timeout(self):
        if self.sent is not None:
            try:
                await self.sent.edit(content="⌛ Не подтвердили за 3 минуты — отменено.", view=None)
            except discord.HTTPException:
                pass

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Подтвердить может только тот, кто просил.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Выполнить", emoji="✅", style=discord.ButtonStyle.success)
    async def ok(self, inter: discord.Interaction, _):
        await inter.response.edit_message(view=None)
        res = await self.cog.run(self.message, self.plan)
        await inter.followup.send(("\n".join(r.lstrip(INFO) for r in res) or "Готово.")[:1900])
        self.stop()

    @discord.ui.button(label="Отмена", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, _):
        await inter.response.edit_message(content="Отменено.", embed=None, view=None)
        self.stop()


class Assistant(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last = {}
        self.chat_day, self.chat_used, self.chat_warned = "", {}, set()
        self.recent = {}    # канал → последние сообщения (для вопросов «о чём они?»)
        self.talk = {}      # (канал, человек) → {"t": время, "turns": [(просьба, ответ)], "ask": ждём ли ответ}
        self.mine = {}      # id моего ответа → кому он был (чтобы понимать ответы-reply без «Швейцар»)
        self.undo = {}      # (guild_id, user_id) → [(когда, что, как откатить)] — «отмени последнее»
        self.invited = {}   # кто когда звал людей в войс через бота

    # ---------- память разговора ----------
    def remember(self, ctx, request, reply, ask=False):
        key = (ctx.channel.id, ctx.author.id)
        t = self.talk.get(key)
        if not t or time.monotonic() - t["t"] > TALK_SEC:
            t = self.talk[key] = {"turns": []}
        t["t"], t["ask"] = time.monotonic(), ask
        t["turns"] = (t["turns"] + [(str(request)[:300], str(reply)[:300])])[-4:]
        for k in [k for k, v in self.talk.items() if time.monotonic() - v["t"] > TALK_SEC]:
            self.talk.pop(k, None)

    def note(self, message):
        """Помним последние сообщения канала (только в памяти), чтобы понимать, о чём идёт разговор."""
        text = (message.content or "").strip()
        if not text or (message.author.bot and message.author.id != self.bot.user.id):
            return
        log_ = self.recent.setdefault(message.channel.id, collections.deque(maxlen=8))
        who = "Швейцар (ты)" if message.author.bot else message.author.display_name
        log_.append((message.id, who, " ".join(text.split())[:160]))
        while len(self.recent) > 60:
            self.recent.pop(next(iter(self.recent)))

    def chat(self, ctx):
        rows = [r for r in self.recent.get(getattr(ctx.channel, "id", 0), ()) if r[0] != getattr(ctx, "id", None)]
        return ("Чат (последние сообщения канала, сверху старые):\n" + "\n".join(f"{w}: {t}" for _, w, t in rows)
                + "\n\n") if rows else ""

    def history(self, ctx):
        t = self.talk.get((ctx.channel.id, ctx.author.id))
        if not t or time.monotonic() - t["t"] > TALK_SEC:
            return ""
        return "Недавний разговор с автором:\n" + "\n".join(f"Автор: {q}\nТы: {a}" for q, a in t["turns"]) + "\n\n"

    def spend(self, uid, warn=False):
        """Суточный лимит болтовни для тех, у кого нет прав помощника. warn=True — «предупреждали ли уже»."""
        day = now().strftime("%Y-%m-%d")
        if self.chat_day != day:
            self.chat_day, self.chat_used, self.chat_warned = day, {}, set()
        if warn:
            first = uid not in self.chat_warned
            self.chat_warned.add(uid)
            return first
        self.chat_used[uid] = self.chat_used.get(uid, 0) + 1
        return self.chat_used[uid] <= DAILY_CHAT

    def awaiting(self, channel_id, user_id):
        """Задал ли бот этому человеку вопрос и ждёт ли ответа (тогда «Швейцар» говорить не нужно)."""
        t = self.talk.get((channel_id, user_id))
        return bool(t and t.get("ask") and time.monotonic() - t["t"] < ANSWER_SEC)

    # ---------- контекст для ИИ ----------
    def may(self, member, action):
        """Можно ли этому человеку такое действие."""
        if action in FOR_ALL and self.bot.has(member, EVERYONE):
            return True
        return self.bot.has(member, PERMS[action])

    def context(self, message):
        g, me = message.guild, message.author
        if not self.bot.has(me, USERS):
            # обычный член семьи: болтать можно сколько угодно, из действий — только безобидные
            seen, rows = set(), []
            near = list(getattr(message, "mentions", [])) + (list(me.voice.channel.members) if me.voice and me.voice.channel
                                                              else []) + self.bot.family_members(g)
            for m in near:
                if m.bot or m.id in seen or m.id == me.id:
                    continue
                seen.add(m.id)
                nick = self.bot.db.gget(g, f"alias:{m.id}")
                rows.append(f"{m.id} | {m.display_name}" + (f" | {nick}" if nick else ""))
                if len(rows) >= 30:
                    break
            who = "\nЛюди (id | имя | прозвища) — для invite и profile:\n" + "\n".join(rows) if rows else ""
            return (f"Автор: {me.display_name} (id {me.id}), {self.bot.rank_name(me) or 'друг семьи'}; в войсе: "
                    f"{me.voice.channel.name if me.voice and me.voice.channel else 'нет'}. Он свой, болтай с ним на равных. "
                    "Ему доступны только действия из списка выше. Попросит что-то управляющее (перенести или отключить "
                    "людей, замутить, собрать, объявить) — \"actions\": [] и откажи в своём стиле: не по чину, пусть "
                    "зовёт старших. Вопросы, шутки и ругань — это разговор, а не действие." + who)
        afk = self.bot.ch(g, "afk")
        lines = [f"Автор: {me.display_name} (id {me.id}); сейчас в войсе: "
                 f"{me.voice.channel.name + ' (id ' + str(me.voice.channel.id) + ')' if me.voice and me.voice.channel else 'нет'}",
                 "", "Голосовые каналы (id | название | кто внутри):"]
        lines[-1] = "Голосовые каналы (id | название | кто внутри | ещё называют):"
        hints = self.hints(g)
        for c in g.voice_channels:
            if afk and c.id == afk.id:
                continue
            ppl = [m.display_name for m in c.members if not m.bot][:12]
            lines.append(f"{c.id} | {c.name} | {', '.join(ppl) or '—'} | {hints.get(c.id, '—')}")
        seen, people = set(), []
        for m in list(getattr(message, "mentions", [])) + [m for c in g.voice_channels for m in c.members] + \
                self.bot.family_members(g):
            if m.bot or m.id in seen:
                continue
            seen.add(m.id)
            nick = self.bot.db.gget(g, f"alias:{m.id}")
            people.append(f"{m.id} | {m.display_name}" + (f" ({m.name})" if m.name != m.display_name else "") +
                          (f" | прозвища: {nick}" if nick else "") +
                          (" | сейчас в войсе" if m.voice and m.voice.channel else ""))
            if len(people) >= 50:
                break
        lines += ["", "Участники (id | имя | прозвища):"] + people
        return "\n".join(lines)

    def hints(self, g):
        out = {}
        for key, words in CHANNEL_HINTS.items():
            c = self.bot.ch(g, key)
            if c:
                out[c.id] = words
        return out

    def find_voice(self, g, cid, name=None):
        """Голосовой канал по id от ИИ, а если ИИ ошибся — по названию, как его назвал человек."""
        try:
            c = g.get_channel(int(cid or 0))
        except (TypeError, ValueError):
            c = None
        if isinstance(c, discord.VoiceChannel):
            return c
        q = norm(name)
        if not q:
            return None
        hints = self.hints(g)
        best, score = None, 0.0
        for c in g.voice_channels:
            for label in [c.name] + [w for w in hints.get(c.id, "").split(",")]:
                n = norm(label)
                if not n:
                    continue
                s = 1.0 if (n in q or q in n) and min(len(n), len(q)) >= 4 else similarity(q, n)
                if s > score:
                    best, score = c, s
        return best if score >= 0.6 else None

    def ambiguous(self, g, a):
        """[(как назвали, [кандидаты])] — имена, под которые одинаково хорошо подходят несколько человек."""
        if self.ids(a.get("users") or ([a["user"]] if a.get("user") else [])) or not a.get("names"):
            return []
        pool = list({m.id: m for m in [m for c in g.voice_channels for m in c.members if not m.bot]
                     + self.bot.family_members(g)}.values())
        out = []
        for raw in a.get("names") if isinstance(a.get("names"), list) else [a.get("names")]:
            scores = self.name_scores(g, raw, pool)
            if not scores:
                continue
            best = max(scores.values())
            close = [m for m in pool if scores.get(m.id, 0) >= max(0.8, best - 0.03)]
            exact = [m for m in pool if scores.get(m.id, 0) >= 0.999]
            if len(close) > 1 and len(exact) != 1:
                out.append((str(raw), close[:5]))
        return out

    def name_scores(self, g, raw, pool):
        """{id: насколько человек подходит под имя (0..1)}."""
        if len(lat(raw)) < 3:
            return {}
        qs = {lat(f) for w in norm(raw).split() for f in forms(w)} | {lat(raw)}
        qs = {q for q in qs if len(q) >= 3}
        out = {}
        for m in pool:
            labels = [m.display_name.split("[")[0], m.name] + (self.bot.db.gget(g, f"alias:{m.id}") or "").split(",")
            best = 0.0
            for lab in labels:
                for part in [lat(lab)] + lat(lab).split():
                    if part:
                        best = max(best, max(1.0 if part == q else similarity(q, part) for q in qs))
            out[m.id] = best
        return out

    def find_members(self, g, a):
        """Люди по id от ИИ; кого ИИ не нашёл — ищем сами по прозвищам и именам."""
        ids = self.ids(a.get("users") or ([a["user"]] if a.get("user") else []))
        found = [m for m in (g.get_member(i) for i in ids) if m]
        if found or not a.get("names"):
            return found
        pool = list({m.id: m for m in [m for c in g.voice_channels for m in c.members if not m.bot]
                     + self.bot.family_members(g)}.values())  # в войсе автора — первыми при равенстве
        out = []
        for raw in a.get("names") if isinstance(a.get("names"), list) else [a.get("names")]:
            scores = self.name_scores(g, raw, pool)  # с падежами: «Пете» → «Петя»
            if not scores:
                continue
            mid = max(scores, key=scores.get)
            best = g.get_member(mid)
            if best and scores[mid] >= 0.8 and best not in out:
                out.append(best)
        return out

    def say_channel(self, g, author, a):
        """Куда идти говорить (say) → ([id каналов], «куда» для ответа) или ([], почему некуда).
        Порядок: названный человек → названный канал → «всем» (все войсы с людьми) → где бот уже стоит/слушает →
        войс автора → самый людный войс семьи. Автору сидеть в войсе не обязательно."""
        music = self.bot.get_cog("Music")
        jarvis = self.bot.get_cog("Jarvis")
        if not music:
            return [], "голосовой модуль не загружен"

        def people(c):
            return [m for m in c.members if not m.bot] if c else []

        q = {"users": a.get("users"), "user": a.get("user"),
             "names": a.get("names") or ([a["target_name"]] if a.get("target_name") else None)}
        if q["users"] or q["user"] or q["names"]:
            found = [m for m in self.find_members(g, q) if not m.bot]
            inv = next((m for m in found if m.voice and m.voice.channel), None)
            if inv:
                return [inv.voice.channel.id], f"к {inv.display_name} в {inv.voice.channel.mention}"
            if found:
                return [], f"{found[0].display_name} сейчас не в войсе — некуда идти"
        if a.get("to") or a.get("to_name"):
            c = self.find_voice(g, a.get("to"), a.get("to_name"))
            if c:
                return [c.id], f"в {c.mention}"
        if a.get("all"):
            chans = music.voice_targets(g, limit=5)
            if chans:
                return [c.id for c in chans], "во все войсы, где есть люди (" + ", ".join(c.mention for c in chans) + ")"
            return [], "в войсе сейчас пусто — некому передавать"
        li = jarvis.listeners.get(g.id) if jarvis else None
        vc = music.vc(g) if music.dn else None
        bot_ch = g.get_channel(li.channel_id if li else
                               (vc.channel.id if vc and vc.is_connected() and vc.channel else 0))
        if people(bot_ch):
            return [bot_ch.id], f"тут, в {bot_ch.mention}"
        mem = g.get_member(author.id)
        if mem and mem.voice and mem.voice.channel:
            return [mem.voice.channel.id], f"в твой войс {mem.voice.channel.mention}"
        chans = music.voice_targets(g, limit=1)
        if chans:
            return [chans[0].id], f"в {chans[0].mention}, где больше всего народу"
        return [], "в войсе сейчас пусто — некому передавать"

    # ---------- приём сообщений ----------
    def claims(self, message, in_lawyer_channel=None):
        """Берёт ли помощник это сообщение. «Швейцар, …» — всегда; упоминание бота — везде, кроме канала юриста
        (там упоминание — вопрос юристу). Ответ на сообщение бота (reply) упоминанием не считается."""
        text = message.content or ""
        if not self.bot.has(message.author, EVERYONE) or not self.bot.feature(message.guild, "assistant:chat"):
            return False
        if TRIGGER.match(text) or NAME_ANYWHERE.search(text):
            return True
        ref = message.reference
        if ref and ref.message_id in self.mine:
            return True  # ответ (reply) на сообщение помощника — продолжение разговора
        if self.awaiting(message.channel.id, message.author.id) and not ref and len(text) < 300:
            return True  # бот только что спросил этого человека — это ответ на вопрос
        me = self.bot.user.id
        if not (f"<@{me}>" in text or f"<@!{me}>" in text) or message.mention_everyone:
            return False
        if in_lawyer_channel is None:
            law = self.bot.ch(message.guild, "lawyer")
            in_lawyer_channel = bool(law and message.channel.id == law.id)
        return not in_lawyer_channel

    def dm_home(self, user):
        """Сервер семьи, от имени которой отвечаем в ЛС: домашний, если человек там свой, иначе первый."""
        found = []
        for g in self.bot.home_guilds:
            m = g.get_member(user.id)
            if m and self.bot.has(m, EVERYONE) and self.bot.feature(g, "assistant:chat"):
                found.append((not self.bot.is_home(g), m))
        return min(found, key=lambda x: x[0])[1] if found else None

    @staticmethod
    def voice_attachment(message):
        return next((a for a in getattr(message, "attachments", []) or []
                     if getattr(a, "is_voice_message", lambda: False)()), None)

    async def voice_text(self, message, guild):
        """Голосовое сообщение Discord → текст через Whisper (ключ семьи). '' — слишком длинное, None — не вышло."""
        att = self.voice_attachment(message)
        jar = self.bot.get_cog("Jarvis")
        if not att or not jar or not self.bot.feature(guild, "assistant:voicemsg"):
            return None
        if (getattr(att, "duration", None) or 0) > VOICE_MSG_SEC or att.size > 4 * 1024 * 1024:
            return ""
        try:
            data = await att.read()
        except discord.HTTPException:
            return None
        return await jar.transcribe_file(guild, data, att.filename or "voice.ogg", att.content_type or "audio/ogg")

    async def heard(self, message, guild, send):
        """Распознать голосовое и показать, что расслышали. → текст или None (сам ответил, почему нет)."""
        async with message.channel.typing():
            text = await self.voice_text(message, guild)
        if text == "":
            await send(content=f"🎙 Голосовое длиннее {VOICE_MSG_SEC // 60} мин не слушаю — скажи короче.")
            return None
        if not text:
            await send(content="🎙 Не разобрал голосовое (или распознавание сейчас недоступно). Напиши текстом.")
            return None
        return text

    async def on_dm(self, message):
        """Личка: всё, что пишут боту, — ему. Права и характер — как на сервере семьи человека.
        Голосовое сообщение в ЛС распознаётся (Whisper) и идёт помощнику как текст."""
        text = (message.content or "").strip()
        voice = not text and self.voice_attachment(message) is not None
        if not text and not voice:
            return
        nick = self.bot.get_cog("NickCheck")
        if text and nick and nick.waiting(message.author.id) and len(text) <= 40 and "\n" not in text:
            return  # это ответ ником на просьбу контроля ника — его разбирает cogs/nickcheck.py
        member = self.dm_home(message.author)
        if not member:
            if time.monotonic() - self.last.get(message.author.id, 0) > 600:
                self.last[message.author.id] = time.monotonic()
                await message.channel.send("🎩 Я Швейцар семьи — болтаю в личке только со своими (семья и друзья "
                                           "семьи). Хочешь к нам — подай заявку на сервере.")
            return
        boss = self.bot.has(member, USERS)
        if time.monotonic() - self.last.get(member.id, 0) < (4 if boss else 8):
            return
        self.last[member.id] = time.monotonic()
        if not boss and not self.spend(member.id):
            if self.spend(member.id, warn=True):
                await message.channel.send("Всё, на сегодня я с тобой наговорился. Завтра приходи.")
            return
        if voice:
            text = await self.heard(message, member.guild, lambda **kw: message.channel.send(**kw))
            if not text:
                return
            await message.channel.send(f"🎙 *«{text[:300]}»*")
        self.note(message)
        ctx = types.SimpleNamespace(guild=member.guild, author=member, channel=message.channel, content=text,
                                    mentions=[], id=message.id, reference=message.reference, dm=True,
                                    attachments=message.attachments)

        async def send(**kw):
            sent = await message.channel.send(**kw)
            self.mine[sent.id] = member.id
            while len(self.mine) > 300:
                self.mine.pop(next(iter(self.mine)))
            return sent
        async with message.channel.typing():
            await self.handle(ctx, text, send)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None and not message.author.bot:
            return await self.on_dm(message)
        if not message.guild or not self.bot.is_family(message.guild):
            return
        self.note(message)
        if message.author.bot:
            return
        text = message.content or ""
        if not self.claims(message):
            return
        m = TRIGGER.match(text)
        request = TRIGGER.sub("", text, count=1) if m else text
        request = request.replace(self.bot.user.mention, "").replace(f"<@!{self.bot.user.id}>", "").strip()
        if not request and self.voice_attachment(message):
            # голосовое ответом на сообщение бота (или на его вопрос) — распознаём; просто так голосовые
            # в каналах не слушаем: это чужие разговоры и лишний расход лимита
            heard = await self.heard(message, message.guild,
                                     lambda **kw: message.reply(mention_author=False, **kw))
            if not heard:
                return
            request = TRIGGER.sub("", heard, count=1).strip() or heard
        if not request:
            return await message.reply("Слушаю 🎩 Например: «Швейцар, перенеси нас всех в балкон».",
                                       mention_author=False)
        boss = self.bot.has(message.author, USERS)
        if time.monotonic() - self.last.get(message.author.id, 0) < (4 if boss else 8):
            return
        self.last[message.author.id] = time.monotonic()
        if not boss and not self.spend(message.author.id):
            if self.spend(message.author.id, warn=True):
                await message.reply("Всё, на сегодня я с тобой наговорился. Завтра приходи.", mention_author=False)
            return

        async def send(**kw):
            sent = await message.reply(mention_author=False, **kw)
            self.mine[sent.id] = message.author.id
            while len(self.mine) > 300:
                self.mine.pop(next(iter(self.mine)))
            return sent
        async with message.channel.typing():
            await self.handle(message, request, send)

    async def handle(self, ctx, request, send):
        """ctx — сообщение или похожий объект (guild, author, channel, content, mentions). Возвращает короткий
        итог для озвучки."""
        ai = self.bot.ai_for(ctx.author.guild)
        if not ai.on:
            await send(content="ИИ не настроен — помощник не работает. Руководство может подключить бесплатный ключ: "
                               "`/ии ключ`.")
            return "ИИ не настроен"
        persona = self.bot.get_cog("Persona")
        card = (persona.card(ctx.author) + "\n\n") if persona else ""
        try:
            system = SYSTEM if self.bot.has(ctx.author, USERS) else SYSTEM_LITE
            if self.bot.feature(ctx.author.guild, "treasury") and self.bot.has(ctx.author, config.TREASURY):
                system = system[:-len(TAIL)] + ACT_TREASURY + TAIL
            system = self.bot.brand(ctx.author.guild, system)
            data = await ai.ask_json(system, f"{self.context(ctx)}\n\n{card}{self.chat(ctx)}{self.history(ctx)}"
                                                      f"Сообщение от {ctx.author.display_name}: {request}",
                                              max_tokens=500)
        except Exception as e:
            log.warning("Швейцар: ИИ не ответил — %s", str(e)[:200])
            text = random.choice(NO_AI)
            await send(content=text)
            return text
        if not isinstance(data, dict):
            text = random.choice(HUH)
            await send(content=text)
            return text
        if persona:
            try:
                persona.apply(ctx.author.guild.id, ctx.author.id, data.get("mood"), data.get("remember"))
            except Exception:
                log.exception("Швейцар: не записал досье")
        plan = [a for a in (data.get("actions") or []) if isinstance(a, dict) and a.get("type") in PERMS]
        reply = str(data.get("reply") or "")[:500]
        if not plan:
            reply = reply or random.choice(HUH)
            self.remember(ctx, request, reply, ask="?" in reply)
            await send(content=reply)
            return reply
        if getattr(ctx, "dm", False):  # в ЛС нет канала сервера — то, что работает с каналом, не выполняем
            here = [a for a in plan if a["type"] in CHANNEL_ONLY]
            if here:
                what = ", ".join(dict.fromkeys(LABEL[a["type"]] for a in here))
                await send(content=f"🙅 {what} — это делается в канале сервера, а не в личке. Напиши там.")
                return "Это только в канале сервера"
        denied = [a for a in plan if not self.may(ctx.author, a["type"])]
        if denied:
            what = ", ".join(dict.fromkeys(LABEL[a["type"]] for a in denied))
            text = random.choice(NOT_YOURS).format(what=what)
            await send(content="⛔ " + text)
            return re.sub(r"[^\w\s,.!?:—-]", "", text)
        if not self.bot.has(ctx.author, USERS):
            for a in plan:  # чужие профили — только тем, у кого есть права
                if a["type"] == "profile":
                    a.pop("names", None)
                    a.pop("users", None)
                    a["user"] = ctx.author.id
        if not getattr(ctx, "voice", False):
            amb = next(((a, raw, cands) for a in plan for raw, cands in self.ambiguous(ctx.guild, a)), None)
            if amb:  # «Дима» — а Дим двое: спросить кнопками, потом продолжить с выбранным
                a, raw, cands = amb
                view = WhoView(self, ctx, request, send, plan, reply, a, cands)
                await send(content=f"🤔 «{raw}» — это кто из них?", view=view)
                return "Уточни кнопкой, кого ты имел в виду"
        return await self.proceed(ctx, request, send, plan, reply)

    async def proceed(self, ctx, request, send, plan, reply):
        """Выполнить план (безопасное — сразу, серьёзное — через «Выполнить»)."""
        if all(a["type"] in SAFE for a in plan):
            extra = []
            res = await self.run(ctx, plan, extra)
            facts = [r[1:] for r in res if r.startswith(INFO)]
            acts = [r for r in res if not r.startswith(INFO)]
            failed = any(r.startswith(("❌", "⛔", "⚠️")) for r in acts)
            # получилось — отвечаем живым словом, а не протоколом; не получилось — показываем, что именно
            if facts:
                text = "\n\n".join(facts) + ("\n" + "\n".join(acts) if acts else "")
            elif failed or not reply:
                text = "\n".join(acts)
            else:
                text = reply + "\n-# " + " · ".join(acts)[:300]
            self.remember(ctx, request, (text if facts or failed else reply or "Готово")[:300])
            await send(content=(text or "Готово.")[:1900])
            for e in extra[:2]:
                await (send(**e) if isinstance(e, dict) else send(embed=e))
            if facts:  # в голосе зачитываем сам ответ, без разметки и ссылок
                return re.sub(r"<[^>]+>|[*_`#>]|\[[^\]]*\]\([^)]*\)", "", " ".join(facts))[:380]
            return ("Не вышло. " + " ".join(r.lstrip("❌⛔⚠️ ") for r in acts if r.startswith(("❌", "⛔", "⚠️"))))[:200] \
                if failed else (reply or "Готово")
        e = discord.Embed(title="🎩 Подтверди", description=(reply + "\n\n" if reply else "") +
                          "\n".join(f"• {self.describe(ctx.guild, {**a, '_author': ctx.author})}" for a in plan),
                          color=config.COLOR)
        self.remember(ctx, request, "Попросил подтвердить: " + (reply or "действие"))
        if getattr(ctx, "voice", False):
            ctx.pending = plan
            await send(embed=e)
            return "Дело серьёзное. Точно делаем? Скажи: Швейцар, да. Или нет."
        view = Confirm(self, ctx.author.id, plan, ctx)
        view.sent = await send(embed=e, view=view)
        return "Подтверди кнопкой в чате"

    # ---------- «отмени последнее» ----------
    UNDO_SEC = 600

    def undo_push(self, g, me, what, fn):
        """Запомнить, как откатить только что сделанное (fn — async без аргументов). Живёт 10 минут, в памяти."""
        st = self.undo.setdefault((g.id, me.id), [])
        st.append((time.monotonic(), what, fn))
        del st[:-10]

    async def undo_last(self, g, me):
        st = self.undo.get((g.id, me.id)) or []
        while st and time.monotonic() - st[-1][0] > self.UNDO_SEC:
            st.pop()
        if not st:
            return "↩️ Нечего отменять: за последние 10 минут ты через меня ничего такого не делал."
        _, what, fn = st.pop()
        try:
            await fn()
        except discord.HTTPException as e:
            return f"⚠️ Отменить «{what}» не вышло: {str(e)[:100]}"
        await self.bot.log(g, f"↩️ {me.mention} отменил через помощника: {what}", "hrlog", 0x607D8B)
        return f"↩️ Отменил: {what}."

    def active_warns(self, g, m):
        return self.bot.db.one("SELECT COUNT(*) n FROM warns WHERE guild_id=? AND user_id=? AND active=1",
                               g.id, m.id)["n"]

    def describe(self, g, a):
        t = a["type"]
        tr = self.bot.get_cog("Treasury")
        if t in TREASURY_ACTS and tr:
            return tr.describe(g, {**a, "_members": self.find_members(g, a)}, self.one(g, a))
        who = ", ".join(m.display_name for m in self.find_members(g, a))
        if t == "move":
            to = self.find_voice(g, a.get("to"), a.get("to_name"))
            return f"{LABEL[t]} {who or a.get('source', '')} → {to.name if to else '?'}"
        if t in ("mute",):
            return f"{LABEL[t]} {who} на {a.get('minutes', 10)} мин · {a.get('reason', '')}"
        if t == "say":
            ids, where = self.say_channel(g, a.get("_author") or g.me, a)
            return (f"🗣 Скажу в войсе: {where if ids else '— (' + where + ')'} — "
                    f"«{str(a.get('text') or '')[:150]}»")
        ms = self.find_members(g, a)
        if t == "warn":
            m = ms[0] if ms else None
            n = self.active_warns(g, m) if m else None
            lim = self.bot.num(g, "warn_limit")
            change = f" · выговоров {n} → {n + 1} из {lim}" + (" ⚠️ лимит" if n + 1 >= lim else "") if n is not None else ""
            return (f"{LABEL[t]} {who} · {a.get('reason', '')}{change}"
                    + (f" + штраф {a['fine']}" if a.get("fine") else ""))
        if t == "unwarn" and ms:
            return f"{LABEL[t]} {who} · активных выговоров {self.active_warns(g, ms[0])} → 0"
        if t == "rank":
            was = self.bot.rank_name(ms[0]) if ms else None
            return f"{LABEL[t]} {who}: {was or '—'} → {config.BASE_RANKS.get(a.get('rank'), a.get('rank'))}"
        if t == "expel" and ms:
            return f"{LABEL[t]} {who}: {self.bot.rank_name(ms[0]) or '—'} → Гость · {a.get('reason', '')}"
        if t == "people":
            from core import people
            return f"{LABEL[t]}: {people.criteria(people.filters(a))}"
        if t == "protocol":
            return f"{LABEL[t]}: {'начать — все в войсе услышат, что идёт запись' if a.get('on', True) is not False else 'закончить'}"
        if t == "mentor_assign":
            try:
                mentor = g.get_member(int(a.get("mentor") or 0))
            except (TypeError, ValueError):
                mentor = None
            return f"{LABEL[t]}: {who or '?'} ← {mentor.display_name if mentor else 'свободный наставник'}"
        if t == "relation_set":
            emoji, name, _ = config.RELATIONS.get(a.get("status"), ("❓", "?", 0))
            return f"{LABEL[t]}: {a.get('family') or '?'} → {emoji} {name}"
        if t == "promote_newbies":
            men = self.bot.get_cog("Mentorship")
            if not men:
                return f"{LABEL[t]}: наставничество не загружено"
            from cogs.mentorship import clamp_days
            md = clamp_days(a.get("min_days"))
            ready, idle, no_exam, fresh, _ = men.newbie_pool(g, a.get("only_active") is not False, min_days=md)
            names = ", ".join(m.display_name for m in ready[:15]) + (" …" if len(ready) > 15 else "")
            term = md if md is not None else men.days(g)
            skip = " · ".join(x for x in (f"не отбыли срок {term} дн. — {len(fresh)}" if fresh else "",
                                          f"неактив {len(idle)}" if idle else "",
                                          f"без экзамена {len(no_exam)}" if no_exam else "") if x)
            return (f"{LABEL[t]}: {len(ready)} чел." + (f" ({names})" if names else "")
                    + (f" · пропущу: {skip}" if skip else "")
                    + ("" if a.get("only_active") is not False else " · включая неактивных"))
        if t == "announce":
            return f"{LABEL[t]}: «{a.get('title', '')}» {'+ голосом' if a.get('voice') else ''}"
        if t == "gather":
            return f"{LABEL[t]}: {a.get('what', '')} · {a.get('when', '')} · {a.get('where') or ''}"
        if t == "purge":
            return f"{LABEL[t]}: {a.get('count', 10)} сообщений"
        if t == "post":
            return (f"{LABEL[t]}: {who} — {'выдать' if a.get('add', True) is not False else 'снять'} "
                    f"{config.STAFF_POSTS.get(a.get('post'), '?')}")
        if t == "bl_add":
            return f"{LABEL[t]}: {who or a.get('nick') or a.get('static') or '?'} · {a.get('reason', '')}"
        if t == "raid":
            return f"{LABEL[t]}: {'включить' if a.get('on', True) is not False else 'выключить'}"
        if t == "slowmode":
            return f"{LABEL[t]}: {a.get('seconds', 0)} сек. в этом канале"
        if t == "poll":
            return f"{LABEL[t]}: «{a.get('question', '')}» — {', '.join(map(str, a.get('options') or ['Да', 'Нет']))}"
        if t == "ask_voice":
            if not who and a.get("target_name"):
                who = ", ".join(m.display_name for m in self.find_members(g, {"names": [a["target_name"]]}))
            rep = self.report_target(g, a)
            return (f"{LABEL[t]}: {who or '?'} — «{str(a.get('question', ''))[:200]}» · "
                    f"ответ в ЛС → {rep.display_name if rep else 'автору'}")
        if t == "dm":
            n = len(self.bot.family_members(g)) if a.get("source") == "family" else 0
            return f"{LABEL[t]} {who or (f'всей семье ({min(n, 40)} чел.)' if n else '?')}: «{str(a.get('text', ''))[:200]}»"
        tail = a.get("text") or a.get("query") or a.get("percent") or a.get("nick") or a.get("reason") or ""
        return f"{LABEL[t]} {who} {str(tail)[:200]}".strip()

    @staticmethod
    def ids(v):
        out = []
        for x in v or []:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out

    def name(self, g, uid):
        m = g.get_member(uid)
        return m.display_name if m else str(uid)

    def chan_name(self, g, cid):
        try:
            c = g.get_channel(int(cid))
        except (TypeError, ValueError):
            c = None
        return c.name if c else "?"

    def targets(self, message, a):
        g, me = message.guild, message.author
        if a.get("users") or a.get("names"):
            found = self.find_members(message.guild, a)
            if found or not a.get("source"):
                return found
        src = str(a.get("source") or "my")
        if src == "family":
            return [m for m in self.bot.family_members(g) if not m.bot]
        if src == "all":
            afk = self.bot.ch(g, "afk")
            return [m for c in g.voice_channels if not afk or c.id != afk.id for m in c.members if not m.bot]
        if src.startswith("channel:"):
            c = g.get_channel(int(src.split(":", 1)[1] or 0))
            return [m for m in c.members if not m.bot] if c else []
        return [m for m in me.voice.channel.members if not m.bot] if me.voice and me.voice.channel else []

    def protected(self, actor, target):
        """Нельзя: себя, ботов и тех, кто не ниже по иерархии семьи; старших наказывает только Глава/зам."""
        if target.id == actor.id or target.bot or not self.bot.outranks(actor, target):
            return True
        return self.bot.has(target, config.MOD_EXEMPT) and not self.bot.has(actor, ["leader", "deputy"])

    # ---------- выполнение ----------
    def one(self, g, a):
        return next(iter(self.find_members(g, a)), None)

    def report_target(self, g, a):
        """ask_voice: кому прислать ответ (report_to / report_to_name); None — автору."""
        nm = a.get("report_to_name")
        q = {"user": a.get("report_to"), "names": [nm] if nm and not isinstance(nm, list) else nm}
        if not q["user"] and not q["names"]:
            return None
        try:
            return next((m for m in self.find_members(g, q) if not m.bot), None)
        except Exception:
            return None

    async def ask_voice(self, message, g, target, question, report_to):
        """Фоном: сходить к target в войс, спросить, принести ответ в ЛС report_to."""
        bot, jarvis = self.bot, self.bot.get_cog("Jarvis")
        ch = target.voice.channel if target.voice else None
        if jarvis and ch:
            text, why = await jarvis.ask_voice(g, ch, target, question)
        else:
            text, why = None, "голосовой модуль не загружен или человек ушёл из войса"
        try:
            if text:
                body = f"**{target.display_name}** ответил: «{text[:1500]}»\n\n-# Спрашивал: «{question[:300]}»"
            else:
                body = (f"**{target.display_name}** не ответил или я не расслышал ({why or 'тишина'}).\n"
                        f"-# Спрашивал: «{question[:300]}». Музыка, бубнёж и шум мимо распознавания — "
                        "спроси ещё раз, пусть ответит внятно.")
            sent = await safe_dm(report_to, embed=bot.embed("🎤 Ответ из войса", body, config.COLOR, g))
        except Exception:
            log.exception("Спросить в войсе: не отправил ответ")
            sent = False
        note = (f"🎤 {target.display_name} ответил — отправил в ЛС {report_to.display_name}." if text
                else f"❌ Не поймал ответ {target.display_name}: {why or 'тишина'}.")
        if not sent:
            note += f" ⚠️ ЛС {report_to.display_name} закрыта — не доставил."
        try:
            if message.channel and not getattr(message, "voice", False):
                await message.channel.send(note[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def info(self, message, a, extra):
        """Справки: возвращает готовый текст ответа."""
        g, me, bot, db, t = message.guild, message.author, self.bot, self.bot.db, a["type"]
        if t == "law":
            law = bot.get_cog("Lawyer")
            q = str(a.get("question") or message.content or "")[:500]
            if not law:
                return "Юрист не загружен."
            text, _sources, _ai = await law.answer(g.id, q, "q")
            return str(text)[:1700]
        if t in ("profile", "warns"):
            m = self.one(g, a)
            if not m and (a.get("user") or a.get("users") or a.get("names")):
                return "Не нашёл такого человека — назови ник точнее или задай прозвище через /прозвище."
            m = m or me
            rows = db.all("SELECT id, reason, created_at FROM warns WHERE guild_id=? AND user_id=? AND active=1 "
                          "ORDER BY id", g.id, m.id)
            wl = "\n".join(f"№{r['id']} · <t:{int(parse_dt(r['created_at']).timestamp())}:d> · {r['reason'][:120]}"
                            for r in rows)
            if t == "warns":
                return f"**{m.display_name}** — активных выговоров: {len(rows)}/{bot.num(g, 'warn_limit')}" + (f"\n{wl}" if wl else "")
            act = bot.get_cog("Activity")
            if act:
                act.flush(m)
            r = db.one("SELECT * FROM activity WHERE guild_id=? AND user_id=?", g.id, m.id)
            gat = bot.get_cog("Gatherings")
            came, total = gat.attendance(m) if gat else (0, 0)
            last = f"<t:{int(parse_dt(r['last_seen']).timestamp())}:R>" if r and r["last_seen"] else "—"
            return (f"**{m.display_name}** — {bot.rank_name(m) or 'не в семье'}\n"
                    f"🎙 В войсе: {fmt_hours(r['voice_seconds']) if r else '0 мин'} (за неделю "
                    f"{fmt_hours(r['week_voice']) if r else '0 мин'}) · 💬 {r['messages'] if r else 0} сообщ.\n"
                    f"⚖️ Выговоры: {len(rows)}/{bot.num(g, 'warn_limit')} · 📣 Сборы: {came} из {total} · был в сети: {last}")
        if t == "top":
            week = a.get("period") != "all"
            v, mcol = ("week_voice", "week_messages") if week else ("voice_seconds", "messages")
            rows = db.all(f"SELECT user_id, {v} v, {mcol} m FROM activity WHERE guild_id=? "
                          f"ORDER BY ({v}/60 + {mcol}*2) DESC LIMIT 10", g.id)
            return f"🏆 Топ — {'неделя' if week else 'всё время'}\n" + "\n".join(
                f"{i + 1}. {self.name(g, r['user_id'])} — 🎙 {fmt_hours(r['v'])} · 💬 {r['m']}" for i, r in enumerate(rows))
        if t == "people":
            from core import people
            res = people.query(bot, g, a)
            v = people.view_for(bot, me, res)
            if v is not None and extra is not None:
                extra.append({"content": "Что с ними сделать?", "view": v})
            return people.render(res)
        if t == "inactive":
            days = max(1, min(90, int(a.get("days") or 5)))
            from cogs.mentorship import is_inactive  # одна мерка с массовым переводом новичков
            out = [m.display_name for m in bot.family_members(g) if is_inactive(bot, g, m, days)]
            return f"💤 Не появлялись {days}+ дн. ({len(out)}): " + (", ".join(out[:40]) or "таких нет, все молодцы")
        if t == "bl_check":
            m = self.one(g, a)
            hit = bot.blacklist_find(g, discord_id=m.id if m else None, static=a.get("static") or None,
                                     nick=a.get("nick") or (a.get("names") or [None])[0] if not m else None)
            if not hit:
                return "✅ В чёрном списке не найден."
            return f"⛔ В чёрном списке (№{hit['id']}): {hit['nick'] or ''} — {str(hit['reason'])[:300]}"
        if t == "market":
            from cogs.market import matches, words
            q = words(str(a.get("query") or ""))
            rows = [r for r in db.all("SELECT * FROM market WHERE guild_id=? AND status='active' ORDER BY bumped_at DESC",
                                      g.id)
                    if not q or matches(r, q)]
            return f"🛒 На рынке найдено: {len(rows)}\n" + "\n".join(
                f"• {config.MARKET_KINDS[r['kind']][0]} {r['title'][:60]} — {r['price']}" for r in rows[:10])
        if t == "digest":
            daily = bot.get_cog("Daily")
            if not daily:
                return "Сводка не загружена."
            e = daily.build_digest(g)
            if extra is not None:
                extra.append(e)
                return "🗞 Сводка ниже."
            return "🗞 " + (e.description or "") + " Подробно: `/сводка`."
        if t == "queue":
            music = bot.get_cog("Music")
            p = music.player(g) if music else None
            if not p or not p.current:
                return "🎶 Сейчас ничего не играет."
            return f"🎶 Играет: {p.current.title[:80]}" + (
                "\nДальше: " + "; ".join(x.title[:50] for x in list(p.queue)[:5]) if p.queue else "\nОчередь пуста.")
        if t in TREASURY_ACTS:
            tr = bot.get_cog("Treasury")
            return await tr.speech(g, me, a) if tr else "Казна не загружена."
        if t in ("web_search", "missed", "draft"):
            auto = bot.get_cog("Automation")
            if not auto:
                return "Модуль автоматизации не загружен."
            if t == "web_search":
                return await auto.search_answer(g, me.id, str(a.get("query") or "")[:200])
            if t == "missed":
                try:
                    hours = int(a.get("hours") or 12)
                except (TypeError, ValueError):
                    hours = 12
                return await auto.missed(me, hours)
            title_, text = await auto.draft(g, str(a.get("kind") or "announce"), str(a.get("topic") or ""))
            return (f"📝 **{title_}**\n{text}\n\n_Это черновик. Опубликовать: «Швейцар, объяви это» или `/черновик`._"
                    if text else title_)
        if t == "car_list":
            gar = bot.get_cog("Garage")
            return await gar.speech(g, me, a, self.one(g, a)) if gar else "Автопарк не загружен."
        if t == "recruit_stats":
            rec = bot.get_cog("Recruit")
            try:
                days = int(a.get("days") if a.get("days") is not None else 7)
            except (TypeError, ValueError):
                days = 7
            return rec.report_text(g, days) if rec else "Статистика не загружена."
        if t == "roll":
            top = max(2, min(1_000_000, int(a.get("max") or 6)))
            return f"🎲 Выпало **{random.randint(1, top)}** (из {top})"
        return ""

    async def run(self, message, plan, extra=None):
        g, me, bot = message.guild, message.author, self.bot
        music = bot.get_cog("Music")
        out = []
        for a in plan[:8]:
            t = a["type"]
            try:
                if t in TREASURY_ACTS and t not in ASK:
                    tr = bot.get_cog("Treasury")
                    a2 = {**a, "_members": self.find_members(g, a)}
                    out.append(await tr.speech(g, me, a2, self.one(g, a)) if tr else "❌ Казна не загружена.")
                elif t in ASK:
                    out.append(INFO + (await self.info(message, a, extra) or "Нет данных."))
                elif t in ("music_pause", "music_resume"):
                    vc = music.vc(g) if music else None
                    if not vc or not music.player(g).current:
                        out.append("❌ Сейчас ничего не играет.")
                    elif t == "music_pause":
                        vc.pause()
                        out.append("⏸ Пауза.")
                    else:
                        vc.resume()
                        out.append("▶️ Продолжаю.")
                elif t in ("voice_mute", "voice_unmute"):
                    people = [m for m in self.targets(message, a) if m.voice and m.id != me.id]
                    n = 0
                    for m in people[:40]:
                        try:
                            await m.edit(mute=(t == "voice_mute"), reason=f"Помощник: {me}")
                            n += 1
                        except discord.HTTPException:
                            pass
                    out.append(f"🎙 Микрофоны {'выключены' if t == 'voice_mute' else 'включены'}: {n} чел."
                               if people else "❌ Не понял, кому переключить микрофон.")
                elif t == "remind":
                    mins = max(1, min(1440, int(a.get("minutes") or 10)))
                    text = str(a.get("text") or "ты просил напомнить")[:300]
                    auto = bot.get_cog("Automation")
                    if auto:  # в базу — переживёт /обновить и перезапуск
                        auto.remind_later(g, "remind", time.time() + mins * 60,
                                          getattr(message.channel, "id", None), me.id, text)
                        out.append(f"⏰ Напомню через {mins} мин.")
                        continue

                    async def later(ch=message.channel, who=me, text=text, mins=mins):
                        await asyncio.sleep(mins * 60)
                        try:
                            await ch.send(f"⏰ {who.mention}, напоминаю: {text}",
                                          allowed_mentions=discord.AllowedMentions(users=[who]))
                        except discord.HTTPException:
                            pass
                        if music and who.voice and who.voice.channel:
                            await music.announce(g, [who.voice.channel.id], f"Напоминаю: {text}")
                    asyncio.create_task(later())
                    out.append(f"⏰ Напомню через {mins} мин.")
                elif t in ("post", "unwarn", "expel", "kick", "ban", "nick"):
                    m = self.one(g, a)
                    if not m:
                        out.append(f"❌ {LABEL[t]}: не нашёл человека.")
                    elif t == "post" and (m.bot or not bot.can_post(me, m, a.get("post"))[0]):
                        out.append(f"⛔ {m.display_name}: {bot.can_post(me, m, a.get('post'))[1] or 'нельзя'}")
                    elif t != "post" and (m.id == me.id or m.bot or not bot.outranks(me, m)):
                        out.append(f"⛔ {m.display_name}: нельзя — он не ниже тебя по иерархии.")
                    elif t == "post":
                        role = bot.role(g, a.get("post")) if a.get("post") in config.STAFF_POSTS else None
                        if not role:
                            out.append("❌ Не понял, какая должность.")
                        else:
                            add = a.get("add", True) is not False
                            await (m.add_roles(role) if add else m.remove_roles(role))
                            self.undo_push(g, me, f"{'выдача' if add else 'снятие'} должности {role.name} у {m.display_name}",
                                           lambda m=m, role=role, add=add: m.remove_roles(role) if add else m.add_roles(role))
                            await bot.log(g, f"🎖 {m.mention} — {'назначен' if add else 'снята должность'} "
                                             f"**{role.name}** ({me.mention})", "hrlog", 0x8E24AA)
                            bot.emit(g, "post", member=m, post=a["post"], added=add, by=me)
                            out.append(f"🎖 {m.display_name}: {role.name} {'выдана' if add else 'снята'}.")
                    elif t == "unwarn":
                        ids = [r["id"] for r in bot.db.all("SELECT id FROM warns WHERE guild_id=? AND user_id=? AND "
                                                          "active=1", g.id, m.id)]
                        n = len(ids)
                        bot.db.q("UPDATE warns SET active=0 WHERE guild_id=? AND user_id=? AND active=1", g.id, m.id)

                        async def back(ids=ids):
                            for i in ids:
                                bot.db.q("UPDATE warns SET active=1 WHERE id=?", i)
                        if ids:
                            self.undo_push(g, me, f"снятие выговоров у {m.display_name}", back)
                        if n:
                            await bot.log(g, f"⚪ С {m.mention} сняты выговоры ({n}) — {me.mention}", "warns")
                        out.append(f"⚪ {m.display_name}: снято выговоров — {n}.")
                    elif t == "expel":
                        await bot.expel(m, me, str(a.get("reason") or "Через помощника")[:300])
                        out.append(f"🚪 {m.display_name} исключён из семьи.")
                    elif t == "nick":
                        old = m.nick
                        await m.edit(nick=str(a.get("nick") or "")[:32] or None, reason=f"Помощник: {me}")
                        self.undo_push(g, me, f"смена ника {m.name}",
                                       lambda m=m, old=old: m.edit(nick=old, reason="Отмена через помощника"))
                        out.append(f"✏️ {m.name}: ник изменён.")
                    else:
                        reason = f"{me}: {str(a.get('reason') or 'без причины')[:300]}"
                        if t == "kick":
                            await m.kick(reason=reason)
                        else:
                            await g.ban(m, reason=reason, delete_message_seconds=0)
                        await bot.log(g, f"{'👢 Кик' if t == 'kick' else '🔨 Бан'}: {m.mention} ({m}) — {reason}",
                                      color=0xE53935)
                        out.append(f"{'👢 Кикнул' if t == 'kick' else '🔨 Забанил'} {m.display_name}.")
                elif t == "bl_add":
                    m = self.one(g, a)
                    static, nick = str(a.get("static") or "").strip(), str(a.get("nick") or "").strip()[:40]
                    reason = str(a.get("reason") or "").strip()[:400]
                    if not (m or static or nick) or not reason:
                        out.append("❌ ЧС: нужны человек (или ник / Static ID) и причина.")
                    elif static and not static.isdigit():
                        out.append("❌ ЧС: Static ID — только цифры.")
                    elif m and (m.bot or not bot.outranks(me, m)):
                        out.append(f"⛔ {m.display_name}: нельзя — он не ниже тебя по иерархии.")
                    else:
                        if m and not static:
                            app = bot.db.one("SELECT static_id, nick FROM applications WHERE guild_id=? AND user_id=? "
                                             "AND static_id IS NOT NULL ORDER BY id DESC LIMIT 1", g.id, m.id)
                            if app:
                                static, nick = app["static_id"], nick or app["nick"]
                        days = max(0, min(3650, int(a.get("days") or 0)))
                        cur = bot.db.q("INSERT INTO blacklist(guild_id, discord_id, static_id, nick, reason, added_by, "
                                       "created_at, until) VALUES(?,?,?,?,?,?,?,?)", g.id, m.id if m else None, static or None, nick or None,
                                       reason, me.id, now_iso(),
                                       (now() + dt.timedelta(days=days)).isoformat() if days else None)
                        if m and bot.in_family(m):
                            await bot.expel(m, me, f"Чёрный список: {reason}")
                        await bot.log(g, f"⛔ ЧС №{cur.lastrowid}: {m.mention if m else nick or static} — {reason} "
                                         f"({me.mention})", "hrlog", 0x212121)
                        c = bot.ch(g, "blacklist_ch")
                        if c:
                            await c.send(embed=bot.embed("⛔ Внесён в чёрный список",
                                                         f"{m.mention if m else ''} **{nick}** "
                                                         f"{('ID `' + static + '`') if static else ''}\n{reason}\n\n"
                                                         f"Внёс: {me.mention}", 0x212121, g))
                        out.append(f"⛔ Внёс в ЧС под №{cur.lastrowid}.")
                elif t == "raid":
                    sec = bot.get_cog("Security")
                    on = a.get("on", True) is not False
                    if not sec:
                        out.append("❌ Защита не загружена.")
                    else:
                        await sec.set_raid(g, on, 3600, reason=f"Режим рейда: {me}")
                        await bot.log(g, f"🛡 Режим рейда {'включён' if on else 'выключен'} — {me.mention}",
                                      color=0xE53935)
                        out.append(f"🛡 Режим рейда {'включён на час' if on else 'выключен'}.")
                elif t == "slowmode":
                    sec = max(0, min(600, int(a.get("seconds") or 0)))
                    await message.channel.edit(slowmode_delay=sec, reason=f"Помощник: {me}")
                    out.append(f"🐢 Медленный режим: {sec} сек." if sec else "🐢 Медленный режим выключен.")
                elif t == "poll":
                    opts = [str(x)[:55] for x in (a.get("options") or []) if str(x).strip()][:10]
                    if len(opts) < 2:
                        opts = ["Да", "Нет"]
                    poll = discord.Poll(question=str(a.get("question") or "Опрос")[:300],
                                        duration=dt.timedelta(hours=max(1, min(168, int(a.get("hours") or 24)))))
                    for o in opts:
                        poll.add_answer(text=o)
                    await message.channel.send(poll=poll)
                    out.append("📊 Опрос создан.")
                elif t == "dm":
                    text = str(a.get("text") or "").strip()[:1500]
                    # ЛС шлём ТОЛЬКО явному получателю: назван человек (users/names) или group-source.
                    # Без этого НЕ подставляем автора/его войс по умолчанию — иначе пишем не тому.
                    src_ok = str(a.get("source") or "") in ("family", "all") or str(a.get("source") or "").startswith("channel:")
                    if a.get("users") or a.get("names"):
                        people = [m for m in self.find_members(g, a) if not m.bot][:40]
                    elif src_ok:
                        people = [m for m in self.targets(message, a) if not m.bot][:40]
                    else:
                        people = []
                    if not text:
                        out.append("❌ ЛС: не понял, что написать.")
                    elif not people:
                        nm = a.get("names")
                        nm = nm if isinstance(nm, list) else ([nm] if nm else [])
                        who = ", ".join(str(x) for x in nm) or "кому"
                        out.append(f"❌ ЛС: не нашёл, {('кому — назови точный ник' if who=='кому' else f'кто такой «{who}» — напиши точный ник или тегни')}.")
                    else:
                        ok = 0
                        for m in people:
                            ok += bool(await safe_dm(m, embed=bot.embed(f"✉️ Сообщение от руководства семьи "
                                                                        f"{bot.family(g)['name']}",
                                                                        text + f"\n\n— {me.display_name}",
                                                                        config.COLOR, g)))
                        out.append(f"✉️ Доставлено в ЛС: {ok} из {len(people)}.")
                elif t == "chronicle":
                    ch = bot.get_cog("Chronicle")
                    text = str(a.get("text") or "").strip()[:800]
                    if not ch or not text:
                        out.append("❌ Летопись: нечего записывать.")
                    else:
                        await ch.record(g, "manual", text, me.id)
                        out.append("📖 Записал в летопись.")
                elif t in ("move", "disconnect"):
                    to = None
                    if t == "move":
                        to = self.find_voice(g, a.get("to"), a.get("to_name"))
                        if not to:
                            out.append("❌ Не нашёл такой голосовой канал.")
                            continue
                    people = [m for m in self.targets(message, a) if m.voice and m.voice.channel != to]
                    if not people:
                        out.append("❌ Не понял, кого переносить (или они не в войсе)." if t == "move"
                                   else "❌ Не понял, кого отключить.")
                        continue
                    n = 0
                    for m in people:
                        try:
                            await m.move_to(to, reason=f"Попросил {me.display_name}")
                            n += 1
                        except discord.HTTPException:
                            pass
                    out.append(f"🔀 Перенёс {n} чел. в {to.mention}" if t == "move" else f"🔌 Отключил {n} чел.")
                elif t == "say":
                    # сам нахожу, куда идти (say_channel) — автору сидеть в войсе не нужно
                    text = str(a.get("text") or "").strip()[:400]
                    ids, where = self.say_channel(g, me, a)
                    if not text:
                        out.append("❌ Не понял, что сказать.")
                    elif not ids:
                        out.append(f"❌ {where[:1].upper() + where[1:]}.")
                    elif await music.announce(g, ids, text=text):
                        out.append(f"🗣 Сказал {where}.")
                    else:
                        vc = music.vc(g) if music.dn else None
                        out.append(f"❌ Не вышло сказать {where}"
                                   + (f": в <#{vc.channel.id}> играет музыка, не перебиваю." if vc and vc.channel
                                      and vc.is_playing() else " — голосовой модуль не готов, попробуй через минуту."))
                elif t == "ask_voice":
                    q = {"users": a.get("users"), "user": a.get("user"),
                         "names": a.get("names") or ([a["target_name"]] if a.get("target_name") else None)}
                    target = next((m for m in self.find_members(g, q) if not m.bot), None)
                    question = str(a.get("question") or a.get("text") or "").strip()[:400]
                    jarvis = bot.get_cog("Jarvis")
                    if not target:
                        out.append("❌ Не нашёл, к кому идти — назови ник точнее или тегни.")
                    elif not question:
                        out.append("❌ Не понял, что спросить.")
                    elif not (target.voice and target.voice.channel):
                        out.append(f"❌ {target.display_name} не в войсе — некуда идти.")
                    elif not (jarvis and music):
                        out.append("❌ Голосовой модуль не загружен.")
                    elif why := jarvis.ask_busy(g, target.voice.channel):
                        out.append(f"❌ Не пойду: {why}.")
                    else:
                        report_to = self.report_target(g, a) or me
                        asyncio.create_task(self.ask_voice(message, g, target, question, report_to))
                        out.append(f"🎤 Иду к {target.display_name} в {target.voice.channel.mention}, спрошу — "
                                   f"ответ пришлю в ЛС {'тебе' if report_to.id == me.id else report_to.display_name}.")
                elif t == "music_play" and music:
                    out.append("🎵 " + await music.enqueue(g, me, str(a.get("query", ""))[:200], message.channel))
                elif t in ("music_skip", "music_stop", "music_volume") and music:
                    vc, p = music.vc(g), music.player(g)
                    if t == "music_volume":
                        v = max(5, min(150, int(a.get("percent") or 60)))
                        p.volume = v / 100
                        if vc and vc.source is not None and hasattr(vc.source, "volume"):
                            vc.source.volume = v / 100
                        out.append(f"🔊 Громкость {v}%")
                    elif not vc:
                        out.append("Музыка и так не играет.")
                    elif t == "music_skip":
                        vc.stop()
                        out.append("⏭ Пропустил.")
                    else:
                        p.reset()  # и обрывает подбор следующего трека (автоплей), если он шёл
                        p.loop = "off"
                        vc.stop()
                        if g.id not in music.hold:  # голосовой Швейцар слушает — остаёмся в войсе
                            await vc.disconnect()
                        out.append("⏹ Выключил музыку.")
                    await music.update_panel(g)
                elif t == "gather":
                    gc = bot.get_cog("Gatherings")
                    msg = await gc.create(g, me, str(a.get("what", "Сбор"))[:200], str(a.get("when", "сейчас"))[:100],
                                          (str(a.get("where"))[:100] if a.get("where") else None), 0, 0,
                                          message.channel)
                    out.append(f"📣 Сбор создан: {msg.jump_url}")
                elif t == "announce" and a.get("at") and bot.get_cog("Automation"):
                    out.append(bot.get_cog("Automation").schedule(g, me, str(a.get("title") or "Объявление"),
                                                                  str(a.get("text", "")), a.get("at"),
                                                                  voice=bool(a.get("voice"))))
                elif t == "announce":
                    c = bot.ch(g, "news") or message.channel
                    e = bot.embed(f"📢 {str(a.get('title', 'Объявление'))[:200]}", str(a.get("text", ""))[:3500],
                                  config.GOLD, g)
                    e.set_author(name=me.display_name, icon_url=me.display_avatar.url)
                    news = bot.role(g, "n_news")
                    await c.send(content=news.mention if news else None, embed=e,
                                 allowed_mentions=discord.AllowedMentions(roles=True))
                    if a.get("voice") and music:
                        await music.announce(g, [x.id for x in music.voice_targets(g)],
                                             text=f"Объявление семьи. {a.get('title', '')}. {str(a.get('text', ''))[:400]}")
                    out.append(f"📢 Опубликовал в {c.mention}.")
                elif t == "alarm" and music:
                    await music.alarm(g, str(a.get("text", "Тревога!"))[:300])
                    out.append("🚨 Тревога поднята.")
                elif t in ("mute", "unmute"):
                    mins = max(1, min(1440, int(a.get("minutes") or 10)))
                    found = self.find_members(message.guild, a)
                    if not found:
                        out.append("❌ Не нашёл, кого " + ("замутить." if t == "mute" else "размутить."))
                    for m in found:
                        if self.protected(me, m):
                            out.append(f"⛔ {m.display_name} — не ниже тебя по иерархии, не трогаю.")
                            continue
                        await m.timeout(dt.timedelta(minutes=mins) if t == "mute" else None,
                                        reason=f"{a.get('reason') or 'Помощник'} ({me.display_name})")
                        if t == "mute":
                            self.undo_push(g, me, f"мут {m.display_name}",
                                           lambda m=m: m.timeout(None, reason="Отмена через помощника"))
                        out.append(f"🔇 {m.display_name} — мут {mins} мин." if t == "mute"
                                   else f"🔊 {m.display_name} — мут снят.")
                elif t == "warn":
                    m = next(iter(self.find_members(message.guild, a)), None)
                    if not m:
                        out.append("❌ Не нашёл, кому выговор.")
                    elif self.protected(me, m):
                        out.append(f"⛔ {m.display_name} — не ниже тебя по иерархии, не трогаю.")
                    else:
                        n = await bot.give_warn(g, m, me, str(a.get("reason") or "без причины")[:300])
                        wid = (bot.db.one("SELECT id FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                                          g.id, m.id) or {"id": None})["id"]

                        async def back(wid=wid):
                            bot.db.q("UPDATE warns SET active=0 WHERE id=?", wid)
                        if wid:
                            self.undo_push(g, me, f"выговор {m.display_name}" + (" (штраф отмени в казне)"
                                                                                 if a.get("fine") else ""), back)
                        out.append(f"⚖️ Выговор {m.display_name} ({n}/{bot.num(g, 'warn_limit')}).")
                        tr = bot.get_cog("Treasury")
                        if a.get("fine") and tr:  # выговор + штраф одной фразой
                            out.append(await tr.speech(g, me, {"type": "fine_add", "amount": a.get("fine"),
                                                               "reason": a.get("reason") or "выговор"}, m))
                elif t == "accept":
                    m, apps = next(iter(self.find_members(message.guild, a)), None), bot.get_cog("Applications")
                    if not m or not apps:
                        out.append("❌ Не нашёл, кого принять.")
                    else:
                        out.append((await apps.manual_accept(g, me, m, a.get("nick"), a.get("static")))[1])
                elif t == "promote_newbies":
                    men = bot.get_cog("Mentorship")
                    out.append(await men.promote_newbies(g, me, a.get("only_active") is not False,
                                                             min_days=a.get("min_days")) if men
                               else "❌ Наставничество не загружено.")
                elif t in ("car_add", "car_give", "car_status", "car_remove"):
                    gar = bot.get_cog("Garage")
                    out.append(await gar.speech(g, me, a, self.one(g, a)) if gar else "Автопарк не загружен.")
                elif t == "rank":
                    m, key = next(iter(self.find_members(message.guild, a)), None), a.get("rank")
                    if not m or key not in config.BASE_RANKS:
                        out.append("❌ Не понял, кому и какой ранг.")
                    elif m.id == me.id or m.bot or not bot.outranks(me, m):
                        out.append(f"⛔ {m.display_name}: ранг меняют только тем, кто ниже тебя по иерархии (себе — нельзя).")
                    elif not bot.in_family(m) and not bot.has(me, ["leader", "deputy"]):
                        out.append(f"⛔ {m.display_name} не в семье — принимают через заявку или «прими в семью».")
                    else:
                        was = next((k for k in config.BASE_RANKS if (r := bot.role(g, k)) and r in m.roles), None)
                        await bot.set_rank(m, key, me, reason="Через помощника")
                        if was and was != key:
                            self.undo_push(g, me, f"ранг {m.display_name} → {config.BASE_RANKS[key]}",
                                           lambda m=m, was=was: bot.set_rank(m, was, me, reason="Отмена через помощника"))
                        out.append(f"🔁 {m.display_name} → {config.BASE_RANKS[key]}")
                elif t in ("leave", "listen", "listen_stop") and not bot.has(me, USERS) and \
                        (li := getattr(bot.get_cog("Jarvis"), "listeners", {}).get(g.id)) and li.owner != me.id:
                    out.append(f"⛔ Я сейчас занят — меня позвал {self.name(g, li.owner)}.")
                elif t in ("vacation", "vacation_end"):
                    st = bot.get_cog("Staff")
                    out.append((await st.start_vacation(g, me, a.get("days"), a.get("reason")) if t == "vacation"
                                else await st.end_vacation(g, me)) if st else "❌ Модуль кадров не загружен.")
                elif t == "room":
                    vr = bot.get_cog("VoiceRooms")
                    out.append(await vr.room_speech(me, str(a.get("op") or ""), a.get("value")) if vr
                               else "❌ Комнаты не загружены.")
                elif t == "mentor_assign":
                    men, newbie = bot.get_cog("Mentorship"), self.one(g, a)
                    try:
                        mentor = g.get_member(int(a.get("mentor") or 0))
                    except (TypeError, ValueError):
                        mentor = None
                    out.append(await men.set_mentor(g, newbie, mentor, me) if men and newbie
                               else "❌ Не понял, какому новичку.")
                elif t == "relation_set":
                    dip = bot.get_cog("Diplomacy")
                    out.append(await dip.set_relation(g, a.get("family"), str(a.get("status") or ""), a.get("note"), me)
                               if dip else "❌ Дипломатия не загружена.")
                elif t == "protocol":
                    jar = bot.get_cog("Jarvis")
                    if not jar:
                        out.append("❌ Голосовой модуль не загружен.")
                    elif a.get("on", True) is not False:
                        out.append(await jar.minutes_start(g, me))
                    else:
                        out.append(await jar.minutes_stop(g, me))
                elif t == "undo":
                    out.append(await self.undo_last(g, me))
                elif t == "paid_claim":
                    tr = bot.get_cog("Treasury")
                    shot = next((x for x in getattr(message, "attachments", []) or []
                                 if (x.content_type or "").startswith("image/") and x.size <= 8 * 1024 * 1024), None)
                    image = None
                    if shot:
                        try:
                            image = (shot.filename or "shot.png", shot.content_type, await shot.read())
                        except discord.HTTPException:
                            image = None
                    out.append(await tr.claim(g, me, "fine" if a.get("what") == "fine" else "due",
                                              a.get("amount"), image) if tr else "Казна не загружена.")
                elif t == "forget":
                    persona = bot.get_cog("Persona")
                    if persona:
                        persona.forget(g.id, me.id)
                    out.append("🧹 Досье стёр. Кто ты вообще такой?")
                elif t == "invite":
                    who = self.one(g, a)
                    vch = me.voice.channel if me.voice else None
                    if not who or who.bot or who.id == me.id:
                        out.append("❌ Не понял, кого позвать.")
                    elif not vch:
                        out.append("❌ Сначала сам зайди в войс — куда я его позову?")
                    elif who.voice and who.voice.channel and who.voice.channel.id == vch.id:
                        out.append(f"❌ {who.display_name} и так сидит с тобой в канале.")
                    elif time.monotonic() - self.invited.get(me.id, -1e9) < INVITE_GAP:
                        out.append("❌ Я только что кого-то для тебя звал. Подожди минут десять, не дёргай людей.")
                    else:
                        self.invited[me.id] = time.monotonic()
                        chat = bot.ch(g, "chat") or getattr(message, "channel", None)
                        note = " ".join(str(a.get("text") or "").split())[:200] or "Ему там скучно. Не хочешь зайти поговорить?"
                        await chat.send(f"{who.mention}, тебя зовёт {me.display_name} — сидит в {vch.mention}. {note} 🎩",
                                        allowed_mentions=discord.AllowedMentions(users=[who]))
                        out.append(f"📨 Позвал {who.display_name} в общем чате.")
                elif t == "leave":
                    jv, music = bot.get_cog("Jarvis"), bot.get_cog("Music")
                    was = bool(g.me.voice and g.me.voice.channel)
                    if jv and g.id in jv.listeners:
                        await jv.stop(g, "по просьбе")
                    if music:
                        music.hold.pop(g.id, None)
                        vc = music.vc(g)
                        if vc:
                            p = music.player(g)
                            p.reset()
                            p.loop = "off"
                            vc.stop()
                            await vc.disconnect()
                    if g.me.voice and g.me.voice.channel:  # «призрак» в войсе после перезапуска
                        try:
                            await g.me.move_to(None)
                        except discord.HTTPException:
                            pass
                    out.append("🚪 Вышел из войса." if was else "Я и так не в войсе.")
                elif t in ("listen", "listen_stop"):
                    jv = bot.get_cog("Jarvis")
                    if not jv:
                        out.append("❌ Голосовой режим не загружен.")
                    elif t == "listen":
                        out.append(await jv.start(g, me))
                    else:
                        out.append(await jv.stop(g, "по просьбе"))
                elif t == "purge":
                    n = max(1, min(100, int(a.get("count") or 10)))
                    deleted = await message.channel.purge(limit=n + 1)
                    out.append(f"🧹 Удалил {max(0, len(deleted) - 1)} сообщений.")
            except Exception as e:
                log.warning("Помощник: %s — %s", t, e)
                out.append(f"⚠️ {LABEL.get(t, t)}: не получилось ({str(e)[:100]})")
        acts = [o for o in out if not o.startswith(INFO)]
        if acts:  # справки в журнал не пишем — только то, что бот реально сделал
            await bot.log(g, f"🎩 Помощник по просьбе {me.mention}: «{(message.content or '')[:200]}»\n" + "\n".join(acts))
        return out


    @app_commands.default_permissions(manage_messages=True)  # участникам — кнопкой (панель) или речью
    @app_commands.command(name="прозвище", description="Как ещё называют участника — чтобы Швейцар понимал «Толик», «Саня»")
    @app_commands.rename(member="участник", names="прозвища")
    @app_commands.describe(names="Через запятую: Толик, Толян. Пусто — показать, «-» — удалить")
    async def alias_cmd(self, inter: discord.Interaction, member: discord.Member, names: str = None):
        if not self.bot.has(inter.user, USERS):
            return await inter.response.send_message("⛔ Только для руководства и старшего состава.", ephemeral=True)
        key = f"alias:{member.id}"
        if names is None:
            cur = self.bot.db.gget(inter.guild, key)
            return await inter.response.send_message(f"Прозвища {member.mention}: {cur or 'не заданы'}", ephemeral=True)
        clean = ", ".join(x.strip()[:24] for x in names.replace(";", ",").split(",") if x.strip() and x.strip() != "-")[:120]
        self.bot.db.gset(inter.guild, key, clean)
        await inter.response.send_message(f"✅ {member.mention}: {clean or 'прозвища удалены'}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Assistant(bot))
