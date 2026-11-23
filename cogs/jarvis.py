# -*- coding: utf-8 -*-
"""Голосовой Швейцар (/швейцар): бот заходит в войс и слушает голосовые команды.
Слышит всю семью и друзей, но по очереди: первым — тот, кто позвал, потом руководство, потом остальные
(у остальных — только последняя фраза и не больше WHISPER_RPM − OWNER_RESERVE распознаваний в минуту).
Говоришь «Швейцар, перенеси всех в балкон» → речь распознаёт бесплатный Whisper (Groq) → помощник выполняет →
бот отвечает голосом и пишет в чат войса.

Как это устроено технически (Discord официально не даёт ботам слушать):
• читаем UDP-пакеты голосового соединения disnake;
• снимаем транспортное шифрование (aead_xchacha20_poly1305_rtpsize, ключ из SESSION_DESCRIPTION);
• снимаем сквозное шифрование DAVE ключами из MLS-сессии disnake (dave.Decryptor на каждого собеседника);
• декодируем Opus → PCM, режем на фразы по паузам, отправляем в Whisper.
Чужих (не семья и не друзья семьи) даже не декодируем."""
import array
import asyncio
import ctypes
import io
import logging
import random
import re
import struct
import time
import types
import wave

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.addressing import _dist, is_name
from core.botcore import now

log = logging.getLogger("family-bot.jarvis")
# libdave пишет ERROR на каждый звуковой кадр, пока ключ собеседника не готов, — сотни строк в секунду.
logging.getLogger("dave").setLevel(logging.CRITICAL)

try:
    import dave
    import disnake
    import nacl.secret
    from disnake.opus import Decoder
    READY = True
except Exception:  # pragma: no cover
    READY = False

WAKE = re.compile(r"^\W*(швейц(?!ари[яиюейх]|арск)\w*|швец\w*|швайц\w*|швейцер\w*)\W*", re.I)
# обращение не в начале фразы: человек говорил без паузы или позвал в конце («включи музыку, швейцар»)
WAKE_ANY = re.compile(r"(?<![а-яёa-z])(швейц(?!ари[яиюейх]|арск)\w*|швец\w*|швайц\w*)\W*", re.I)
YES = re.compile(r"\b(да|подтвер\w*|выполн\w*|давай|делай|ок|окей)\b", re.I)
NO = re.compile(r"\b(нет|отмен\w*|не надо|стоп)\b", re.I)
# «не заходи ко мне» — человек не хочет визитов «проведать одинокого» (persona), запоминаем навсегда
NOVISIT = re.compile(r"\bне\s+(?:надо\s+)?(?:заход\w*|приход\w*|беспоко\w*|навеща\w*)|\bбольше\s+не\s+(?:заход|приход)\w*", re.I)
_WORDS = re.compile(r"[a-zа-яё]+", re.I)
# частые «галлюцинации» распознавателя на тишине/шуме (субтитры из обучающих данных) — это не речь
JUNK = re.compile(r"субтитр|продолжение следует|спасибо за просмотр|подписывайтесь|dimatorzok|редактор|"
                  r"корректор|amara\.org", re.I)
STOP = re.compile(r"\b(выйди|выходи|свободен|хватит слушать|перестань слушать|отключись|уходи)\b", re.I)
SILENCE = 1.1          # сек тишины = конец фразы (0.8 резало «Швейцар… включи музыку» на две фразы)
MIN_SEC, MAX_SEC = 0.6, 15
IDLE_STOP = 30 * 60    # нет команд 30 минут — уходим
DAILY_LIMIT = int(getattr(config, "WHISPER_DAILY", 0) or 1800)  # бесплатный Groq даёт 2000 распознаваний в сутки
WHISPER_RPM = int(getattr(config, "WHISPER_RPM", 0) or 18)  # у Groq лимит 20/мин — держим с запасом, чтобы не ловить 429
OWNER_RESERVE = 5      # из минутного лимита столько распознаваний держим для того, кто позвал (остальные их не тратят)
STALE_SEC = 20         # фраза остальных, пролежавшая в очереди дольше, уже неактуальна
# полная модель заметно точнее turbo на русском (лимиты Groq у них одинаковые, тоже бесплатно); turbo — запасная
WHISPER_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
BYTES_PER_SEC = 48000 * 2 * 2  # 48 кГц, стерео, 16 бит


# фразы без ИИ — разные, чтобы не звучал как автоответчик
HELLO = ["Швейцар на месте. Чё надо?", "Я тут. Излагай.", "На связи. Только не орите все сразу.",
         "Зашёл. Кому чего?", "Ну, я пришёл. Удивляйте.", "Здорово, бродяги. Слушаю.", "Явился. Чего изволите?"]
WHAT = ["Ну?", "Чё?", "Говори уже.", "Слушаю, не томи.", "Я весь внимание.", "Да-да?", "Ну давай, удиви."]
THINK = ["Ща.", "Секунду, думаю.", "Погоди, соображаю.", "Так, ща гляну.", "Момент.", "Ща-ща, не гони.", "Думаю, не мешай."]
DONE = ["Сделано.", "Готово, шеф.", "Есть.", "Всё чётко.", "Как просил."]
NOPE = ["Ладно, отбой.", "Как скажешь, отменил.", "Проехали.", "Ну и не надо."]

VOICE_LEVEL = 350      # средняя громкость 20-мс кусочка, с которой считаем его речью (из 32768)
MIN_VOICED = 0.35      # сек речи во фразе; меньше — это щелчок, вздох или шум, в распознавание не отправляем


try:  # детектор речи — необязательный (pip webrtcvad-wheels); нет — считаем по громкости, как раньше
    import webrtcvad as _webrtcvad
    _VAD = _webrtcvad.Vad(2)
except Exception:  # noqa: BLE001
    _VAD = None


def _voice_name(word):
    """Голосом имя искажается сильнее, чем в чате: «Швицар», «Шварцар» — на «шв», до 2 правок от «швейцар»."""
    w = word.lower().replace("ё", "е")
    if len(w) < 5 or not w.startswith("шв") or re.match(r"швейцари|швейцарск", w):
        return False
    return any(_dist(x, "швейцар") <= 2 for x in {w, w.rstrip("ьуаеоы")})


def _vad_speech(frame):
    try:
        return _VAD.is_speech(frame, 16000)
    except Exception:  # noqa: BLE001 — кривой кадр: не мешаем, считаем речью
        return True


def pcm_to_wav16k(pcm):
    """48 кГц стерео → 16 кГц моно (с простым сглаживанием) → (WAV, сколько секунд в нём похоже на речь)."""
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) // 12 * 12])
    out = array.array("h", bytes(len(s) // 6 * 2))
    for k, i in enumerate(range(0, len(s), 6)):
        out[k] = (s[i] + s[i + 1] + s[i + 2] + s[i + 3] + s[i + 4] + s[i + 5]) // 6
    voiced = 0
    raw = out.tobytes()
    for i in range(0, len(out) - 320, 320):
        if sum(map(abs, out[i:i + 320])) // 320 > VOICE_LEVEL:
            # громко — а речь ли это? webrtcvad (если стоит) отсекает кашель, музыку, стук клавиатуры
            if _VAD is None or _vad_speech(raw[i * 2:(i + 320) * 2]):
                voiced += 1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out.tobytes())
    return buf.getvalue(), voiced * 0.02


_BOX = {}
MAX_SAMPLES = 5760  # 120 мс при 48 кГц — максимум одного Opus-пакета


def safe_decode(dec, frame):
    """Свой декодер вместо disnake.Decoder.decode: тот выделяет буфер по числу каналов ПАКЕТА (моно → в 2 раза
    меньше), а пишет всегда стерео → переполнение памяти и падение процесса. Здесь буфер всегда максимальный."""
    if not frame or len(frame) > 1275 * 3:
        return b""
    pcm = (ctypes.c_int16 * (MAX_SAMPLES * 2))()
    try:
        n = disnake.opus._lib.opus_decode(dec._state, frame, len(frame),
                                          ctypes.cast(pcm, disnake.opus.c_int16_ptr), MAX_SAMPLES, 0)
    except Exception:
        return b""
    if n <= 0 or n > MAX_SAMPLES:
        return b""
    return array.array("h", pcm[: n * 2]).tobytes()


def parse_rtp(data, secret_key):
    """→ (ssrc, opus_frame_or_dave_frame) или None."""
    if len(data) < 16 or (data[0] >> 6) != 2:
        return None
    pt = data[1] & 0x7F
    if 72 <= pt <= 76 or pt != 0x78:  # RTCP и всё, что не Opus
        return None
    ssrc = struct.unpack_from(">I", data, 8)[0]
    cc = data[0] & 0x0F
    ext = data[0] & 0x10
    hlen = 12 + 4 * cc + (4 if ext else 0)
    if len(data) < hlen + 4 + 16:
        return None
    nonce = data[-4:] + b"\x00" * 20
    try:
        key = bytes(secret_key)
        box = _BOX.get(key)
        if box is None:
            _BOX.clear()
            box = _BOX[key] = nacl.secret.Aead(key)
        pt_bytes = box.decrypt(bytes(data[hlen:-4]), aad=bytes(data[:hlen]), nonce=nonce)
    except Exception:
        return None
    if data[0] & 0x20 and pt_bytes:  # RTP padding: последний байт — длина выравнивания
        pad = pt_bytes[-1]
        pt_bytes = pt_bytes[:-pad] if 0 < pad <= len(pt_bytes) else pt_bytes
    if ext:
        ext_words = struct.unpack_from(">H", data, 12 + 4 * cc + 2)[0]
        pt_bytes = pt_bytes[ext_words * 4:]
    return ssrc, pt_bytes


class Listener:
    def __init__(self, cog, guild, channel_id, owner, temp=False):
        self.cog, self.bot = cog, cog.bot
        self.temp = temp     # временный слушатель «спросил — жду ответ»: команд не выполняет, ловит одну фразу
        self.capture = None  # (user_id, Future) — ждём ответ этого человека (ask_voice)
        self.guild, self.channel_id, self.owner = guild, channel_id, owner
        self.users = {}      # ssrc -> user_id
        self.dec = {}        # user_id -> dave.Decryptor
        self.ratchet_at = {}
        self.fail_until = {}
        self.ssrc_dec = {}   # ssrc -> [user_id, dave.Decryptor, fails]
        self.try_after = {}  # ssrc -> monotonic: когда снова пробовать опознать
        self.tries = {}      # ssrc -> сколько раз пробовали
        self.opus = {}       # user_id -> opus Decoder
        self.buf = {}        # user_id -> bytearray
        self.last = {}       # user_id -> monotonic
        # Очередь фраз на распознавание: по одной за раз. Первым — тот, кто позвал (и чей ответ ждём),
        # потом руководство, потом остальные. У остальных хранится только последняя фраза, протухшие выкидываются.
        self.todo = []       # [(приоритет, monotonic, user_id, pcm)]
        self.wake = asyncio.Event()
        self.key = None      # ключ голосового соединения: сменился — значит, бот переподключился
        self.chatty_until = 0        # до этого момента хозяину можно отвечать без «Швейцар» (бот сам зашёл поболтать)
        self.idle_stop = IDLE_STOP   # сколько молчания терпим, прежде чем уйти
        self.perm = {}       # user_id -> (allowed, checked_at)
        self.stats = {"packets": 0, "rtp_ok": 0, "unknown": 0, "decoded": 0, "dave_fail": 0, "phrases": 0,
                      "heard": "", "ops": {}}
        self.last_cmd = time.monotonic()
        self.inter = None       # /швейцар — для скрытых ответов (Discord даёт 15 минут)
        self.inter_at = 0
        self.pending = None     # (plan, ctx, monotonic) — ждём «Швейцар, да/нет»
        self.minutes = None     # протокол собрания: [(unix, user_id, текст)] — записываем всех, а не только команды
        self.running = True
        self.tasks = [asyncio.create_task(self.reader()), asyncio.create_task(self.flusher()),
                      asyncio.create_task(self.worker())]

    def reset(self, channel_id=None):
        """Бот сменил канал: у соединения новые ключи и номера голосов — всё опознаём заново."""
        if channel_id:
            self.channel_id = channel_id
        for d in (self.users, self.dec, self.ratchet_at, self.fail_until, self.ssrc_dec, self.try_after, self.tries,
                  self.opus, self.buf, self.last):
            d.clear()
        self.todo.clear()

    def chatting(self, uid):
        """Бот сам зашёл поболтать с хозяином, и они в канале вдвоём — «Швейцар» говорить не нужно."""
        if uid != self.owner or time.monotonic() > self.chatty_until:
            return False
        ch = self.guild.get_channel(self.channel_id)
        return bool(ch and len([m for m in ch.members if not m.bot]) == 1)

    def stop(self):
        self.running = False
        for t in self.tasks:
            t.cancel()

    @property
    def music(self):
        return self.bot.get_cog("Music")

    def vc(self):
        m = self.music
        return m.vc(self.guild) if m else None

    def allowed(self, uid):
        if self.capture and uid == self.capture[0]:
            return True  # ждём ответ именно от него — слушаем, даже если он не из семьи
        if self.temp:
            return False  # временный слушатель: остальных не декодируем и лимит распознавания не тратим
        ok, at = self.perm.get(uid, (False, -1e9))
        if time.monotonic() - at > 30:
            m = self.guild.get_member(uid)
            ok = bool(m and not m.bot and self.bot.has(m, config.FAMILY_PLUS))  # слышим всю семью и друзей
            self.perm[uid] = (ok, time.monotonic())
        return ok

    def hook(self, vc):
        ws = getattr(vc, "ws", None)
        if ws is None or ws is disnake.utils.MISSING:
            return
        if getattr(ws, "_jarvis", False):
            ws._jarvis_li = self  # хук уже стоит — просто направляем сигналы текущему слушателю
            return
        orig = ws._hook

        async def h(w, msg, _orig=orig):
            try:
                li = getattr(w, "_jarvis_li", None) or self
                op = msg.get("op")
                li.stats["ops"][op] = li.stats["ops"].get(op, 0) + 1
                if op == 5:  # SPEAKING: кто за каким ssrc (точнее догадки — перезаписываем)
                    d = msg.get("d") or {}
                    uid, ssrc = int(d["user_id"]), int(d["ssrc"])
                    for k in [k for k, v in li.users.items() if v == uid and k != ssrc]:
                        li.users.pop(k, None)
                    li.users[ssrc] = uid
            except Exception:
                pass
            await _orig(w, msg)

        ws._hook = h
        ws._jarvis = True
        ws._jarvis_li = self

    async def reader(self):
        loop = asyncio.get_running_loop()
        lost = None  # с какого момента нет связи с войсом
        while self.running:
            try:
                vc = self.vc()
                if not vc or not vc.is_connected() or not getattr(vc, "secret_key", None):
                    lost = lost or time.monotonic()
                    if self.temp and time.monotonic() - lost > 5:
                        self.resolve(None)  # временный: связи нет — просто сдаёмся, голосового Швейцара не трогаем
                        self.running = False
                        return
                    if time.monotonic() - lost > 20:
                        # «слушаю», а в войсе меня нет (выкинули, оборвалась связь) — честно выключаемся,
                        # чтобы /швейцар потом включил всё с чистого листа
                        log.warning("Голосовой Швейцар: 20 с нет связи с войсом — выключаю прослушивание")
                        self.bot.spawn(self.cog.stop(self.guild, "потерял связь с голосовым каналом"))
                        return
                    await asyncio.sleep(0.5)
                    continue
                lost = None
                self.hook(vc)
                key = bytes(vc.secret_key)
                if key != self.key:
                    if self.key is not None:
                        self.reset()  # войс переподключился: старые ключи и номера голосов больше не годятся
                    self.key = key
                data = await asyncio.wait_for(loop.sock_recv(vc.socket, 2048), 1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, AttributeError):
                await asyncio.sleep(0.3)
                continue
            except Exception:
                log.exception("Голосовой Швейцар: сбой приёма звука")
                await asyncio.sleep(1)
                continue
            self.stats["packets"] += 1
            try:
                self.packet(vc, data)
            except Exception as e:
                log.debug("пакет: %s", e)

    def packet(self, vc, data):
        r = parse_rtp(data, vc.secret_key)
        if not r:
            return
        ssrc, frame = r
        self.stats["rtp_ok"] += 1
        if frame == b"\xf8\xff\xfe" or len(frame) < 3:
            return
        sess = getattr(getattr(vc, "dave", None), "_session", None)
        if sess is not None and sess.has_established_group():
            if frame[-2:] != b"\xfa\xfa":
                return  # при сквозном шифровании незашифрованных кадров быть не должно — не трогаем
            got = self.dave_frame(sess, ssrc, frame)
            if not got:
                return
            uid, frame = got
        else:
            uid = self.users.get(ssrc) or self.guess(ssrc)
            if not uid:
                return
        if not self.allowed(uid):
            return
        dec = self.opus.get(uid) or self.opus.setdefault(uid, Decoder())
        pcm = safe_decode(dec, frame)
        if not pcm:
            return
        self.stats["decoded"] += 1
        # Конец фразы считаем по ГРОМКОСТИ, а не по приходу пакетов: у кого микрофон включён постоянно, пакеты идут
        # и в тишине — раньше такая фраза не заканчивалась и резалась кусками по 15 секунд.
        a = array.array("h")
        a.frombytes(pcm[: len(pcm) // 2 * 2])
        loud = len(a) and sum(map(abs, a[::16])) * 16 // len(a) > VOICE_LEVEL * 0.6
        buf = self.buf.setdefault(uid, bytearray())
        if not loud and not buf:
            return  # тишину перед фразой не копим
        buf.extend(pcm)
        if loud or uid not in self.last:
            self.last[uid] = time.monotonic()

    def dave_frame(self, sess, ssrc, frame):
        """Снять DAVE. Чей голос — узнаём по ключу: подходит ключ человека → это он (подделать нельзя)."""
        t = time.monotonic()
        ent = self.ssrc_dec.get(ssrc)
        if ent:
            uid, d, _ = ent
            out = d.decrypt(dave.MediaType.audio, frame)
            if out is None and t - self.ratchet_at.get(ssrc, 0) > 1:
                r = sess.get_key_ratchet(str(uid))
                if r is not None:
                    d.transition_to_key_ratchet(r)  # ключи сменились (кто-то зашёл/вышел)
                    self.ratchet_at[ssrc] = t
                    out = d.decrypt(dave.MediaType.audio, frame)
            if out is not None:
                ent[2] = 0
                return uid, out
            ent[2] += 1
            self.stats["dave_fail"] += 1
            if ent[2] > 25:  # полсекунды не подходит — опознаём заново сразу, без паузы
                self.ssrc_dec.pop(ssrc, None)
                self.try_after.pop(ssrc, None)
                self.tries.pop(ssrc, None)
            return None
        if t < self.try_after.get(ssrc, 0):
            return None
        n = self.tries[ssrc] = self.tries.get(ssrc, 0) + 1
        self.try_after[ssrc] = t + min(2 + n // 5, 8)  # пробуем снова и снова, просто реже
        self.stats["unknown"] += 1
        hint = self.users.get(ssrc)
        ch = self.guild.get_channel(self.channel_id)
        cands = [hint] if hint else [m.id for m in (ch.members if ch else []) if not m.bot and self.allowed(m.id)]
        for uid in cands:
            r = sess.get_key_ratchet(str(uid))
            if r is None:
                continue
            d = dave.Decryptor()
            d.transition_to_key_ratchet(r)
            out = d.decrypt(dave.MediaType.audio, frame)
            if out is not None:
                self.ssrc_dec[ssrc] = [uid, d, 0]
                self.ratchet_at[ssrc] = t
                self.users[ssrc] = uid
                return uid, out
        return None

    def guess(self, ssrc):
        """Discord не прислал, чей это голос. Первый незнакомый голос после /швейцар — того, кто вызвал команду.
        Если вызвавший уже привязан, а в канале ровно один человек из руководства без голоса — этот голос его."""
        self.stats["unknown"] += 1
        known = set(self.users.values())
        # наугад голос никому не приписываем: теперь слышим всех, и чужая фраза не должна сойти за команду старшего
        ch = self.guild.get_channel(self.channel_id)
        if not ch:
            return None
        cands = [m.id for m in ch.members if not m.bot and m.id not in known and self.allowed(m.id)]
        if len(cands) == 1:
            self.users[ssrc] = cands[0]
            return cands[0]
        return None

    async def private(self, member, **kw):
        """Скрытый ответ под /швейцар (виден только вызвавшему). Через 15 минут Discord этого не даёт — молчим,
        отвечаем только голосом. В чат и в личку ничего не пишем."""
        if not self.inter or member.id != self.owner or time.monotonic() - self.inter_at > 14 * 60:
            return None
        try:
            return await self.inter.followup.send(ephemeral=True, **kw)
        except discord.HTTPException:
            return None

    async def flusher(self):
        while self.running:
            await asyncio.sleep(0.25)
            try:
                if self.flush_once():
                    return
            except Exception:
                log.exception("Голосовой Швейцар: сбой разбора фраз")

    def flush_once(self):
        if True:
            t = time.monotonic()
            for uid, buf in list(self.buf.items()):
                dur = len(buf) / BYTES_PER_SEC
                if (buf and t - self.last.get(uid, t) > SILENCE) or dur > MAX_SEC:
                    pcm = bytes(buf)
                    self.buf[uid] = bytearray()
                    self.last.pop(uid, None)
                    if dur < MIN_SEC:
                        continue
                    self.enqueue(uid, pcm)
            if not self.temp and t - self.last_cmd > self.idle_stop:
                self.bot.spawn(self.cog.stop(self.guild, "30 минут без команд"))
                return True
            return False

    def priority(self, uid):
        """0 — тот, кто позвал (или чей ответ ждём), 1 — у кого права помощника, 2 — остальные."""
        if uid == self.owner or (self.capture and uid == self.capture[0]):
            return 0
        m = self.guild.get_member(uid)
        return 1 if m and self.bot.has(m, self.cog.users_roles) else 2

    def enqueue(self, uid, pcm):
        prio = self.priority(uid)
        if prio and self.minutes is None:  # у остальных важна только последняя фраза (в протоколе — все)
            self.todo = [x for x in self.todo if x[2] != uid]
        self.todo.append((prio, time.monotonic(), uid, pcm))
        if len(self.todo) > 12:  # шумный канал: сверху очереди — самое важное и свежее
            self.todo.sort(key=lambda x: (x[0], -x[1]))
            del self.todo[12:]
        self.wake.set()

    async def worker(self):
        """Распознаём по одной фразе: позвавший — вне очереди, остальные — по порядку."""
        while self.running:
            await self.wake.wait()
            self.wake.clear()
            while self.todo and self.running:
                self.todo.sort(key=lambda x: (x[0], x[1]))
                prio, t, uid, pcm = self.todo.pop(0)
                if prio and self.minutes is None and time.monotonic() - t > STALE_SEC:
                    continue  # пока разбирали важное, фраза остальных устарела — отвечать на неё поздно
                await self.phrase(uid, pcm, low=prio > 0)

    def resolve(self, text):
        """Отдать пойманный ответ тому, кто ждёт (ask_capture), и перестать ждать."""
        cap, self.capture = self.capture, None
        if cap and not cap[1].done():
            cap[1].set_result(text)

    async def phrase(self, uid, pcm, low=False):
        try:
            self.stats["phrases"] += 1
            text = await self.cog.transcribe(self.guild, pcm, low=low and self.minutes is None)
            self.stats["heard"] = (text or "∅")[:80]
            if not text:
                return
            if self.minutes is not None:
                self.minutes.append((int(time.time()), uid, text[:500]))
                self.last_cmd = time.monotonic()  # пока идёт собрание — не уходим «за молчанием»
            if self.capture and uid == self.capture[0]:
                self.last_cmd = time.monotonic()
                self.resolve(text)  # это ответ на наш вопрос — командой не считаем
                return
            if self.temp:
                return
            m = WAKE.match(text) or self.cog.fuzzy_wake(text, first=True)
            asst = self.cog.bot.get_cog("Assistant")
            cmd = None
            if not m:
                found = list(WAKE_ANY.finditer(text)) or self.cog.fuzzy_wake(text)
                if found:
                    m = found[-1]
                    cmd = text[m.end():].strip(" ,.!?-—")
                    if len(cmd) < 2:  # позвали в конце фразы — просьба стоит перед именем
                        cmd = (re.split(r"[.!?]+", text[:m.start()].strip(" ,.!?-—")) or [""])[-1].strip(" ,-—")
            if not m and not (asst and asst.awaiting(self.channel_id, uid)) and not self.chatting(uid):
                return  # без «Швейцар» слушаем только ответ на свой вопрос или разговор один на один
            if cmd is None:
                cmd = (text[m.end():] if m else text).strip(" ,.!?-—")
            member = self.guild.get_member(uid)
            ch = self.guild.get_channel(self.channel_id)
            self.last_cmd = time.monotonic()
            await self.private(member, content=f"🗣 Услышал: «{text[:300]}»")
            if self.pending and time.monotonic() - self.pending[2] < 45 and self.pending[1].author.id == uid:
                plan, pctx, _ = self.pending
                if YES.search(cmd):
                    self.pending = None
                    asst = self.bot.get_cog("Assistant")
                    res = await asst.run(pctx, plan) if asst else []
                    await self.private(member, content=("\n".join(r.lstrip("\u200b") for r in res) or "Готово.")[:1900])
                    return await self.cog.say(self.guild, random.choice(DONE))
                if NO.search(cmd):
                    self.pending = None
                    return await self.cog.say(self.guild, random.choice(NOPE))
            if not cmd:
                return await self.cog.say(self.guild, random.choice(WHAT))
            if NOVISIT.search(cmd) and len(cmd) < 60:
                persona = self.bot.get_cog("Persona")
                if persona:
                    persona.set_novisit(self.guild, uid, True)
                await self.cog.say(self.guild, "Понял, больше сам не зайду. Передумаешь — команда «помощник визиты».")
                return await self.cog.stop(self.guild, "попросили не заходить")
            if STOP.search(cmd) and len(cmd) < 40:
                if uid != self.owner and not self.bot.has(member, self.cog.users_roles):
                    return await self.cog.say(self.guild, "Не ты меня звал — не тебе и выгонять.")
                return await self.cog.stop(self.guild, "голосовой командой")
            asst = self.bot.get_cog("Assistant")
            if not asst:
                return
            ctx = types.SimpleNamespace(guild=self.guild, author=member, channel=ch, content=text, mentions=[],
                                        voice=True, pending=None)

            async def send(**kw):
                kw.pop("view", None)  # подтверждение — голосом
                return await self.private(member, **kw)
            said = []

            async def filler():  # ИИ думает дольше пары секунд — не молчим
                await asyncio.sleep(2.2)
                said.append(1)
                await self.cog.say(self.guild, random.choice(THINK))
            fill = asyncio.create_task(filler())
            try:
                summary = await asst.handle(ctx, cmd, send)
            finally:
                if not said:
                    fill.cancel()
            if self.chatty_until:
                self.chatty_until = time.monotonic() + 150  # разговор идёт — продолжаем слушать без «Швейцар»
            if ctx.pending:
                self.pending = (ctx.pending, ctx, time.monotonic())
            await self.cog.say(self.guild, re.sub(r"[*_`>#🔀🗣🎵⏭⏹🔊📣📢🚨🔇⚖️🔁🧹⛔❌⚠️✅]", "", summary or random.choice(DONE))[:400])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Голосовой Швейцар: фраза — %s", e)


class Jarvis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.listeners = {}
        self._whisper_by_key = {}  # ключ Groq -> метки последних распознаваний (минутный лимит)
        self.whisper_dead = set()  # модели, которых у Groq больше нет (до перезапуска)
        self.whisper_fail_since, self.whisper_fail_reason = None, ""  # для сторожа (cogs/maintenance.py)
        self.asking = set()  # guild_id — сейчас ходим спрашивать человека в войсе (ask_voice)
        from cogs.assistant import USERS
        self.users_roles = USERS

    def cog_unload(self):
        for li in self.listeners.values():
            li.stop()

    # ---------- «сходи-спроси-принеси ответ» ----------
    def ask_busy(self, guild, channel):
        """Почему сейчас нельзя сходить спросить (или None). голосовой Швейцар слушает в другом канале — не уводим его."""
        if not READY:
            return "на хостинге нет модулей для приёма звука"
        if not self.whisper_key(guild):
            return "нет ключа Groq для распознавания речи (`/ии ключ`)"
        if guild.id in self.asking:
            return "я уже пошёл спрашивать другого — дай закончить"
        li = self.listeners.get(guild.id)
        if li and li.channel_id != channel.id:
            return f"я слушаю команды в <#{li.channel_id}>, оттуда не уйду"
        music = self.bot.get_cog("Music")
        if not music or not music.dn or not music.dn.is_ready():
            return "голосовой модуль ещё запускается"
        vc = music.vc(guild)
        if vc and vc.is_connected() and vc.channel and vc.channel.id != channel.id and vc.is_playing() \
                and music.player(guild).current:
            return f"в <#{vc.channel.id}> играет музыка"
        return None

    async def ask_capture(self, guild, channel, user_id, timeout=20):
        """Одноразово поймать первую распознанную фразу user_id в channel. → текст или None (молчал/не разобрал).
        Бот уже должен стоять в канале. голосовой Швейцар слушает тут же — берём его слушателя, иначе свой временный."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        li = self.listeners.get(guild.id)
        own = None
        if li and li.channel_id == channel.id:
            if li.capture:
                return None
            li.capture = (user_id, fut)
        else:
            own = li = Listener(self, guild, channel.id, user_id, temp=True)
            li.capture = (user_id, fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=max(5, min(60, timeout)))
        except asyncio.TimeoutError:
            return None
        finally:
            if li.capture and li.capture[1] is fut:
                li.capture = None
            if own:
                own.stop()

    async def ask_voice(self, guild, channel, member, question, timeout=20):
        """Зайти в channel, спросить вслух, дождаться ответа member. → (текст|None, причина сбоя|None)."""
        why = self.ask_busy(guild, channel)
        if why:
            return None, why
        music = self.bot.get_cog("Music")
        self.asking.add(guild.id)
        held = guild.id not in music.hold
        try:
            if held:
                music.hold[guild.id] = channel.id  # после вопроса не выходим — надо послушать ответ
            ok = await music.announce(guild, [channel.id], text=question)
            vc = music.vc(guild)
            if not ok or not vc or not vc.is_connected() or not vc.channel or vc.channel.id != channel.id:
                return None, "не смог зайти в войс или озвучить вопрос"
            if not disnake.opus.is_loaded():
                try:
                    disnake.opus._load_default()
                except Exception:
                    pass
            text = await self.ask_capture(guild, channel, member.id, timeout)
            return (text or None), (None if text else "не услышал ответа")
        except Exception as e:
            log.warning("Спросить в войсе: %s", e)
            return None, "что-то сломалось по дороге"
        finally:
            self.asking.discard(guild.id)
            if held and guild.id not in self.listeners and music.hold.get(guild.id) == channel.id:
                music.hold.pop(guild.id, None)
                vc = music.vc(guild)
                if vc and not music.player(guild).current:
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass

    # ---------- распознавание ----------
    def whisper_key(self, guild):
        """Ключ Groq для распознавания: домашний сервер — ключ хоста, остальные семьи — только свой (/ии ключ)."""
        ai = self.bot.ai_for(guild)
        return ai.keys.get("groq", "")

    def whisper_counter(self, guild):
        day = now().strftime("%Y-%m-%d")
        return f"whisper:{day}" if self.bot.is_home(guild) else f"whisper:{day}:{guild.id}"

    async def transcribe(self, guild, pcm, low=False):
        """low=True — фраза не того, кто позвал: она не тратит запас лимита, оставленный позвавшему."""
        key = self.whisper_key(guild)
        if not key:
            return None
        wav, voiced = await asyncio.to_thread(pcm_to_wav16k, pcm)
        if voiced < MIN_VOICED:
            return None  # тишина или шум — не тратим лимит и не даём распознавателю «додумать» команду
        return await self._whisper(guild, key, wav, "cmd.wav", "audio/wav", low)

    async def transcribe_file(self, guild, data, filename="voice.ogg", ctype="audio/ogg"):
        """Голосовое сообщение Discord (ogg/opus) → текст. Те же лимиты и тот же ключ, что у голосового Швейцара."""
        key = self.whisper_key(guild)
        if not key or not data:
            return None
        return await self._whisper(guild, key, data, filename, ctype, False)

    async def _whisper(self, guild, key, payload, filename, ctype, low):
        counter = self.whisper_counter(guild)
        used = int(self.bot.db.get(counter) or 0)
        if used >= (DAILY_LIMIT * 9 // 10 if low else DAILY_LIMIT):
            return None
        # минутный лимит Groq (20/мин на ключ): если за последние 60 сек уже много — пропускаем
        mono = time.monotonic()
        calls = self._whisper_by_key.setdefault(key, [])
        calls[:] = [c for c in calls if mono - c < 60]
        if len(calls) >= (WHISPER_RPM - OWNER_RESERVE if low else WHISPER_RPM):
            return None
        calls.append(mono)
        models = [m for m in WHISPER_MODELS if m not in self.whisper_dead] or list(WHISPER_MODELS[-1:])
        try:
            for model in models:
                text = await self._whisper_call(key, payload, filename, ctype, model, counter)
                if text is not False:
                    return text
            return None
        except Exception as e:
            log.warning("Whisper: %s", e)
            self.whisper_fail_since = self.whisper_fail_since or time.time()
            self.whisper_fail_reason = type(e).__name__
            return None

    def fuzzy_wake(self, text, first=False):
        """Имя бота, которое распознаватель записал криво («Швицар», «Шварцар», «Sveitsar»): совпадения слов.
        first — только если имя первое слово фразы (как WAKE.match)."""
        hits = [m for m in _WORDS.finditer(text[:300]) if is_name(m.group(0)) or _voice_name(m.group(0))]
        if first:
            w = _WORDS.search(text)
            return w if w and hits and hits[0].start() == w.start() else None
        return hits

    async def _whisper_call(self, key, payload, filename, ctype, model, counter):
        """Текст / None (не распознано, лимит) / False (модель недоступна — пробуем следующую)."""
        form = aiohttp.FormData()
        form.add_field("file", payload, filename=filename, content_type=ctype)
        form.add_field("model", model)
        form.add_field("language", "ru")
        form.add_field("response_format", "verbose_json")
        form.add_field("temperature", "0")
        # подсказка только для написания имени. Готовых команд в ней быть не должно: на шуме распознаватель
        # возвращает текст подсказки, и бот «слышал» команду, которую никто не говорил
        form.add_field("prompt", "Разговор в голосовом канале. К боту обращаются по имени: Швейцар.")
        if True:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post("https://api.groq.com/openai/v1/audio/transcriptions", data=form,
                                  headers={"Authorization": f"Bearer {key}"}) as r:
                    if r.status in (400, 404) and "model" in (await r.text()).lower() and \
                            model != WHISPER_MODELS[-1]:
                        log.warning("Whisper: модель %s недоступна (%s) — перехожу на запасную", model, r.status)
                        self.whisper_dead.add(model)
                        return False
                    if r.status != 200:
                        log.warning("Whisper %s: %s", r.status, (await r.text())[:200])
                        if r.status != 429:  # минутный лимит — штатно; остальное — сбой для сторожа
                            self.whisper_fail_since = self.whisper_fail_since or time.time()
                            self.whisper_fail_reason = f"ошибка {r.status}"
                        return None
                    data = await r.json()
                    self.whisper_fail_since = None
                    # в дневной счётчик — только реально распознанное (429 и сбои лимит не съедают)
                    self.bot.db.set(counter, int(self.bot.db.get(counter) or 0) + 1)
                    segs = data.get("segments")
                    if isinstance(segs, list) and segs:
                        # выкидываем куски, где это не речь. Правило самого Whisper: «тишина» = no_speech высокий
                        # И уверенность низкая. Раньше резали по любому из двух — и теряли слова команды
                        good = []
                        for sg in segs:
                            if not isinstance(sg, dict):
                                continue
                            ns, lp = float(sg.get("no_speech_prob") or 0), float(sg.get("avg_logprob") or 0)
                            if (ns > 0.6 and lp < -0.8) or lp < -1.6 or JUNK.search(sg.get("text", "")):
                                continue
                            good.append(sg.get("text", ""))
                        return "".join(good).strip()
                    text = (data.get("text") or "").strip()
                    return "" if JUNK.search(text) else text

    async def say(self, guild, text):
        music = self.bot.get_cog("Music")
        li = self.listeners.get(guild.id)
        if music and li and text:
            await music.announce(guild, [li.channel_id], text=text)

    # ---------- старт / стоп ----------
    async def start(self, guild, member, inter=None, hello=None):
        if not READY:
            return "❌ На хостинге нет нужных модулей для приёма звука."
        music = self.bot.get_cog("Music")
        if not music or not music.dn or not music.dn.is_ready():
            return "❌ Голосовой модуль ещё запускается — попробуй через 10 секунд."
        if not member.voice or not member.voice.channel:
            return "❌ Сначала зайди в голосовой канал."
        if guild.id in self.asking:
            return "❌ Я сейчас пошёл спрашивать человека в войсе — позови через полминуты."
        if not self.whisper_key(guild):
            return "❌ Для голосовых команд нужен бесплатный ключ Groq — руководство может вставить его: `/ии ключ`."
        try:
            if not disnake.opus.is_loaded():
                disnake.opus._load_default()
        except Exception:
            pass
        ch = member.voice.channel
        dch = music.dn.get_channel(ch.id)
        vc = music.vc(guild)
        if vc and not vc.is_connected():
            # соединение-призрак (бота выкинули из войса вручную или оборвалась связь): раньше из-за него бот
            # отвечал «слушаю», а в канал не заходил
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        try:
            if vc and vc.channel.id != ch.id:
                if vc.is_playing() and music.humans(guild, vc.channel.id):
                    return f"❌ Я сейчас играю музыку в <#{vc.channel.id}>."
                await vc.move_to(dch)
            elif not vc:
                await asyncio.wait_for(dch.connect(), timeout=20)
        except Exception as e:
            log.warning("Голосовой Швейцар: не зашёл в войс — %r", e)
            return f"❌ Не могу зайти в войс: {e or type(e).__name__}. Попробуй ещё раз через несколько секунд."
        vc = music.vc(guild)
        if not vc or not vc.is_connected():
            return "❌ Не получилось подключиться к голосовому каналу. Попробуй ещё раз через несколько секунд."
        old = self.listeners.pop(guild.id, None)
        if old:
            old.stop()
        music.hold[guild.id] = ch.id
        li = self.listeners[guild.id] = Listener(self, guild, ch.id, member.id)
        if inter is not None:
            li.inter, li.inter_at = inter, time.monotonic()
        self.bot.spawn(self.say(guild, hello or random.choice(HELLO)))
        return (f"🎙 Слушаю команды в {ch.mention}. Говори: «Швейцар, …». Первые 15 минут что услышал и что сделал "
                "покажу здесь (видишь только ты), дальше — только голосом. В чат ничего не пишу.\n"
                "Серьёзное (мут, выговор…) подтверждай голосом: «Швейцар, да» / «Швейцар, нет». "
                "Выключить: «Швейцар, выйди» или `/швейцар`.")

    # ---------- протокол собрания ----------
    async def minutes_start(self, guild, member):
        """Зайти в войс автора и записывать всё сказанное. Вслух предупреждаем, что идёт запись."""
        li = self.listeners.get(guild.id)
        if not li or not member.voice or li.channel_id != member.voice.channel.id:
            res = await self.start(guild, member, hello="Веду протокол собрания. Всё, что здесь говорят, записывается. "
                                                        "Закончить: «Швейцар, закончи протокол».")
            li = self.listeners.get(guild.id)
            if not li:
                return res
        else:
            self.bot.spawn(self.say(guild, "Веду протокол собрания. Всё, что здесь говорят, записывается."))
        li.minutes = []
        li.idle_stop = max(li.idle_stop, 3 * 3600)
        return ("📝 Веду протокол в <#%d>. Все в канале предупреждены голосом. Закончить: «Швейцар, закончи протокол». "
                "Лимит распознавания — %d фраз в сутки, на час собрания хватает." % (li.channel_id, DAILY_LIMIT))

    async def minutes_stop(self, guild, by):
        li = self.listeners.get(guild.id)
        if not li or li.minutes is None:
            return "Протокол сейчас не ведётся."
        lines, li.minutes = li.minutes, None
        if not lines:
            return "📝 Протокол закрыт — ничего не записалось."
        name = lambda uid: getattr(guild.get_member(uid), "display_name", str(uid))  # noqa: E731
        raw = "\n".join(f"[{time.strftime('%H:%M', time.gmtime(t + 3 * 3600))}] {name(u)}: {x}" for t, u, x in lines)
        ai = self.bot.ai_for(guild)
        text = None
        if ai.on:
            try:
                text = await ai.ask(self.bot.brand(guild, "Ты секретарь собрания RP-семьи. По расшифровке (распознавание "
                                    "речи, возможны ошибки) составь протокол по-русски: 1) кто был, 2) что обсуждали, "
                                    "3) РЕШЕНИЯ, 4) кто что делает и к какому сроку. Только то, что реально сказано — "
                                    "ничего не выдумывай. Коротко, с эмодзи, до 1800 знаков."),
                                    raw[-15000:], max_tokens=1200, smart=True)
            except Exception as e:  # noqa: BLE001
                log.info("Протокол: ИИ не ответил — %s", e)
        c = self.bot.ch(guild, "council_chat") or self.bot.ch(guild, "botcmd")
        if not c:
            return "📝 Протокол готов, но некуда положить (нет канала совета)."
        file = discord.File(io.BytesIO(raw.encode("utf-8")), filename="расшифровка.txt")
        try:
            await c.send(embed=self.bot.embed("📝 Протокол собрания", (text or "ИИ недоступен — ниже полная расшифровка.")[:4000],
                                              config.GOLD, guild).set_footer(text=f"Закрыл: {by.display_name}"),
                         file=file)
        except discord.HTTPException:
            return "❌ Не смог отправить протокол в канал совета."
        return f"📝 Протокол ({len(lines)} фраз) — в {c.mention}."

    async def stop(self, guild, why=""):
        li = self.listeners.pop(guild.id, None)
        music = self.bot.get_cog("Music")
        if music:
            music.hold.pop(guild.id, None)
        if not li:
            vc = music.vc(guild) if music else None
            if vc and (not vc.is_connected() or not music.player(guild).current):  # сидит без дела — выходим
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                return "🚪 Вышел из войса."
            return "Я и так не слушаю."
        li.stop()
        owner = guild.get_member(li.owner)
        if owner and why and not why.startswith("выключил"):
            await li.private(owner, content=f"🔇 Перестал слушать — {why}.")
        vc = music.vc(guild) if music else None
        if vc and (not vc.is_connected() or not music.player(guild).current):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        return "🔇 Перестал слушать."

    async def kicked(self, guild, li):
        """Бот пропал из войса. Даём 8 секунд на переподключение (Discord иногда сам перекидывает на другой сервер);
        не вернулся — выключаем прослушивание и убираем зависшее соединение."""
        await asyncio.sleep(8)
        if self.listeners.get(guild.id) is not li:
            return
        me = guild.me
        if me and me.voice and me.voice.channel:
            return
        await self.stop(guild, "меня отключили от голосового канала")

    async def follow(self, guild, li, channel):
        music = self.bot.get_cog("Music")
        vc = music.vc(guild) if music else None
        if not vc or (vc.channel and vc.channel.id == channel.id):
            return
        p = music.player(guild)
        if (vc.is_playing() and p.current or p.starting) and music.humans(guild, vc.channel.id):
            return  # в старом канале люди слушают музыку — не уводим её
        lock = music.locks.setdefault(guild.id, asyncio.Lock())
        async with lock:  # не дёргаем соединение посреди фразы
            try:
                dch = music.dn.get_channel(channel.id)
                if dch:
                    music.hold[guild.id] = channel.id
                    await vc.move_to(dch)
                    li.reset(channel.id)
            except Exception as e:
                log.warning("Голосовой Швейцар: не смог перейти за хозяином: %s", e)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        li = self.listeners.get(member.guild.id)
        if not li:
            return
        g = member.guild
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                self.bot.spawn(self.kicked(g, li))  # отключили вручную или оборвалась связь
                return
            if after.channel and before.channel != after.channel:
                li.reset(after.channel.id)  # бота перенесли (или он перешёл сам) — слушаем в новом канале
                music = self.bot.get_cog("Music")
                if music:
                    music.hold[g.id] = after.channel.id
            return
        if member.bot:
            return
        if after.channel and after.channel.id == li.channel_id and before.channel != after.channel \
                and member.id != li.owner:
            persona = self.bot.get_cog("Persona")
            if persona:
                self.bot.spawn(persona.greet(g, member))  # иногда встречаем входящих личной фразой
        if member.id == li.owner and after.channel and before.channel != after.channel \
                and after.channel.id != li.channel_id:
            afk = self.bot.ch(g, "afk")
            if not (afk and after.channel.id == afk.id):
                await self.follow(g, li, after.channel)  # хозяин перешёл в другой войс — идём за ним
                return
        ch = member.guild.get_channel(li.channel_id)
        if ch and not [m for m in ch.members if m.id == li.owner]:
            await self.stop(member.guild, "тот, кто меня звал, ушёл из канала")

    @app_commands.command(name="швейцар", description="Швейцар заходит в твой войс и слушает голосовые команды (вкл/выкл)")
    async def jarvis_cmd(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("⛔ Швейцар разговаривает только с семьёй и друзьями семьи.",
                                                      ephemeral=True)
        li = self.listeners.get(inter.guild.id)
        if li and li.owner != inter.user.id and not self.bot.has(inter.user, self.users_roles):
            # занят другим: забрать или выключить его может только тот, у кого есть права помощника
            return await inter.response.send_message(f"⛔ Швейцар сейчас занят — его позвал <@{li.owner}>.",
                                                     ephemeral=True)
        await inter.response.defer(ephemeral=True)
        if inter.guild.id in self.listeners:
            msg = await self.stop(inter.guild, "выключил " + inter.user.display_name)
        else:
            msg = await self.start(inter.guild, inter.user, inter)
        await inter.followup.send(msg, ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)  # служебное: видно только админам
    @app_commands.command(name="швейцар-статус", description="Диагностика голосового режима (приём звука)")
    async def jarvis_status(self, inter: discord.Interaction):
        if not self.bot.has(inter.user, self.users_roles):  # в статусе видно последнюю услышанную фразу
            return await inter.response.send_message("⛔ Только для руководства и старшего состава.", ephemeral=True)
        li = self.listeners.get(inter.guild.id)
        if not li:
            return await inter.response.send_message("Сейчас не слушаю.", ephemeral=True)
        vc = li.vc()
        sess = getattr(getattr(vc, "dave", None), "_session", None)
        st = li.stats
        await inter.response.send_message(
            f"Пакетов получено: **{st['packets']}** · голосовых: **{st['rtp_ok']}** · без владельца: "
            f"**{st['unknown']}** · декодировано: **{st['decoded']}** · ошибок DAVE: **{st['dave_fail']}** · "
            f"фраз в распознавание: **{st['phrases']}**\nПоследнее услышанное: «{st['heard'] or '—'}»\n"
            f"Сигналы Discord: {dict(sorted(st['ops'].items(), key=lambda x: str(x[0])))} · "
            f"Опознано по ключу: {len(li.ssrc_dec)} · Знаю голоса: {len(li.users)} · DAVE-группа: {'да' if sess and sess.has_established_group() else 'нет'} · "
            f"режим: `{getattr(vc, 'mode', '?')}`\n"
            f"Распознаваний сегодня: {self.bot.db.get(self.whisper_counter(inter.guild)) or 0}/{DAILY_LIMIT}",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Jarvis(bot))
