# -*- coding: utf-8 -*-
"""Музыка: команды на discord.py, а голос — через disnake (его шифрование DAVE на dave-py работает со звуком)."""
import asyncio
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from core import config

try:
    import disnake
except ImportError:
    disnake = None

log = logging.getLogger("family-bot.music")

FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG = "ffmpeg"

YTDL = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True,
        "default_search": "ytsearch", "source_address": "0.0.0.0"}
BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
LOOP_NAMES = {"off": "выкл", "track": "трек", "queue": "очередь"}


# Голоса для озвучки. «Дмитрий» — родной русский, но у Microsoft он часто не отвечает на запросы с хостинга,
# поэтому по умолчанию стоит стабильный мужской голос; сменить — /голос.
VOICES = {
    "ru-RU-DmitryNeural": "Дмитрий — родной русский, мужской",
    "ru-RU-SvetlanaNeural": "Светлана — родная русская, женский",
    "en-US-AndrewMultilingualNeural": "Эндрю — тёплый, спокойный (лёгкий акцент)",
    "en-US-BrianMultilingualNeural": "Брайан — живой, разговорный (лёгкий акцент)",
    "de-DE-FlorianMultilingualNeural": "Флориан — низкий, солидный",
    "fr-FR-RemyMultilingualNeural": "Реми — мягкий",
}
DEFAULT_VOICE = "ru-RU-DmitryNeural"
# в запасе — надёжные голоса на случай, если родной русский разок не ответит
BACKUP_VOICES = ["ru-RU-DmitryNeural", "en-US-AndrewMultilingualNeural", "en-US-BrianMultilingualNeural"]

if disnake:
    class Overlay(disnake.AudioSource):
        """Музыка, поверх которой бот может сказать фразу: на время фразы музыка встаёт на паузу и продолжается."""

        def __init__(self, main):
            self.main, self.over, self.done, self.loop = main, None, None, None

        @property
        def volume(self):
            return self.main.volume

        @volume.setter
        def volume(self, v):
            self.main.volume = v

        def say(self, path, loop):
            self._finish()
            self.loop, self.done = loop, asyncio.Event()
            self.over = disnake.PCMVolumeTransformer(disnake.FFmpegPCMAudio(str(path), executable=FFMPEG), volume=1.0)
            return self.done

        def _finish(self):
            o, self.over = self.over, None
            if o is not None:
                try:
                    o.cleanup()
                except Exception:
                    pass
            if self.done is not None and self.loop is not None:
                self.loop.call_soon_threadsafe(self.done.set)
                self.done = None

        def read(self):
            if self.over is not None:
                try:
                    data = self.over.read()
                except Exception:
                    data = b""
                if data:
                    return data
                self._finish()
            return self.main.read()

        def is_opus(self):
            return False

        def cleanup(self):
            self._finish()
            self.main.cleanup()
else:
    Overlay = None


def _extract(query, playlist=False):
    import yt_dlp
    opts = dict(YTDL)
    if playlist:
        opts.update(noplaylist=False, extract_flat="in_playlist", playlistend=25)
    with yt_dlp.YoutubeDL(opts) as y:
        return y.extract_info(query, download=False)


def fmt_dur(sec):
    if not sec:
        return "∞"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _related(track, exclude):
    """Похожие треки для автоплея (в потоке): рекомендации SoundCloud к треку, иначе поиск по исполнителю.
    Возвращает список (title, url, duration, thumb, artist) без уже звучавших."""
    import yt_dlp
    opts = dict(YTDL, noplaylist=False, extract_flat="in_playlist", playlistend=15)
    queries = []
    if "soundcloud.com/" in (track.url or ""):
        queries.append(track.url.split("?")[0].rstrip("/") + "/recommended")
    artist = (track.artist or "").strip()
    if not artist and " - " in track.title:
        artist = track.title.split(" - ")[0].strip()
    if artist:
        queries.append(f"scsearch15:{artist}")
    queries.append(f"scsearch15:{track.title}")
    found, seen = [], set()
    with yt_dlp.YoutubeDL(opts) as y:
        for q in queries:
            try:
                info = y.extract_info(q, download=False) or {}
            except Exception as e:
                log.info("Автоплей: %s — %s", q, str(e)[:120])
                continue
            for en in info.get("entries") or []:
                url = en and (en.get("webpage_url") or en.get("url"))
                if not url or not url.startswith("http") or url.split("?")[0] in exclude:
                    continue
                name = norm_title(en.get("title"))
                if name and (name in exclude or name in seen):
                    continue  # та же песня у другого загрузчика — не по кругу
                seen.add(name)
                dur = en.get("duration")
                if dur and (dur < 45 or dur > 900):  # без джинглов и часовых миксов
                    continue
                found.append((en.get("title") or "Без названия", url, dur, en.get("thumbnail"),
                               en.get("artist") or en.get("uploader")))
            if found:
                break
    return found


class Track:
    def __init__(self, title, url, requester, duration=None, radio=False, thumb=None, artist=None, auto=False):
        self.title, self.url, self.requester = title, url, requester
        self.duration, self.radio, self.thumb = duration, radio, thumb
        self.artist, self.auto = artist, auto  # auto — подобран автоплеем


def norm_title(title):
    """«Кино - Группа крови (Live)» → «киногруппакрови»: одну песню у разных загрузчиков считаем одной."""
    t = re.sub(r"[\(\[].*?[\)\]]", "", (title or "").lower())
    return re.sub(r"\W+", "", t)


class Player:
    def __init__(self):
        self.queue = deque()
        self.history = deque(maxlen=40)  # ссылки и названия сыгранных — автоплей не повторяется
        self.current = None
        self.volume = 0.6
        self.loop = "off"
        self.text = None
        self.panel = None
        self.leave_task = None
        self.leave_alone = False
        self.starting = None  # play_next уже подбирает/запускает трек — второй параллельный не нужен
        self.again = False  # пока шёл запуск, play_next звали ещё раз
        self.gen = 0  # растёт при «стоп»/уходе/тревоге: долгие поиски, начатые до сброса, свой трек не включают

    def reset(self):
        self.queue.clear()
        self.current = None
        self.gen += 1

    def remember(self, track):
        if track.url and not track.radio:
            self.history.append(track.url.split("?")[0])
            self.history.append(norm_title(track.title))


class MusicControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _go(self, inter, action):
        await inter.client.get_cog("Music").control(inter, action)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="music:toggle")
    async def b_toggle(self, inter, _):
        await self._go(inter, "toggle")

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def b_skip(self, inter, _):
        await self._go(inter, "skip")

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def b_stop(self, inter, _):
        await self._go(inter, "stop")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def b_loop(self, inter, _):
        await self._go(inter, "loop")

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def b_shuffle(self, inter, _):
        await self._go(inter, "shuffle")

    @discord.ui.button(emoji="🎲", style=discord.ButtonStyle.secondary, custom_id="music:auto")
    async def b_auto(self, inter, _):
        await self._go(inter, "auto")


class Music(commands.Cog):
    mus = app_commands.Group(name="музыка", description="Управление музыкой")

    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.locks = {}
        self.hold = {}  # guild_id -> channel_id: «Джарвис» слушает — из войса не выходим
        self.voice_fails, self.voice_down = {}, {}
        self.greeted = {}
        self.dn = None
        if disnake:
            self.dn = disnake.Client(intents=disnake.Intents(guilds=True, voice_states=True))
            self._dn_task = asyncio.create_task(self._run_voice())

    async def _run_voice(self):
        try:
            await self.bot.wait_until_ready()
            await self.dn.start(config.TOKEN)
        except Exception:
            log.exception("Голосовой модуль disnake не запустился")

    async def cog_unload(self):
        if self.dn:
            await self.dn.close()

    def vc(self, guild):
        g = self.dn.get_guild(guild.id) if self.dn and self.dn.is_ready() else None
        return g.voice_client if g else None

    def humans(self, guild, channel_id):
        ch = guild.get_channel(channel_id)
        return [m for m in ch.members if not m.bot] if ch else []

    def player(self, guild):
        return self.players.setdefault(guild.id, Player())

    # ---------- воспроизведение ----------
    async def join(self, inter):
        if not inter.user.voice or not inter.user.voice.channel:
            await inter.followup.send("Сначала зайди в голосовой канал.", ephemeral=True)
            return None
        if not self.dn or not self.dn.is_ready():
            await inter.followup.send("⏳ Голосовой модуль ещё запускается — попробуй через 10 секунд.", ephemeral=True)
            return None
        ch = self.dn.get_channel(inter.user.voice.channel.id)
        vc = self.vc(inter.guild)
        try:
            if vc and vc.channel.id != ch.id:
                if vc.is_playing() and self.humans(inter.guild, vc.channel.id):
                    await inter.followup.send(f"Я уже играю в <#{vc.channel.id}>.", ephemeral=True)
                    return None
                await vc.move_to(ch)
            elif not vc:
                vc = await ch.connect()
        except Exception as e:
            log.exception("Голос")
            await inter.followup.send(f"❌ Не могу подключиться к голосу: {e}\n(Нужны пакеты disnake[voice] — "
                                      "переустанови зависимости через start.bat)", ephemeral=True)
            return None
        return vc

    async def source(self, track, volume):
        url, headers = track.url, None
        if not track.radio:
            info = await asyncio.to_thread(_extract, track.url)
            if "entries" in info:
                info = info["entries"][0]
            url = info["url"]
            headers = info.get("http_headers")
            track.duration = track.duration or info.get("duration")
            track.thumb = track.thumb or info.get("thumbnail")
            track.artist = track.artist or info.get("artist") or info.get("uploader")
        before = BEFORE
        if headers and headers.get("User-Agent"):
            before += f' -user_agent "{headers["User-Agent"]}"'
        src = disnake.FFmpegPCMAudio(url, executable=FFMPEG, before_options=before, options="-vn")
        return Overlay(disnake.PCMVolumeTransformer(src, volume=volume))

    async def play_next(self, guild, failed=0):
        """Следующий трек. Подбор/загрузка идут секундами (автоплей, yt-dlp), поэтому вызов, пришедший в это
        время (/играть, after, ИИ), ничего не делает — идущий сам возьмёт свежую очередь. Иначе два вызова
        дерутся за плеер: второй затирает p.current играющего трека."""
        p = self.player(guild)
        if p.starting:
            p.again = True  # если идущий запуск прервут сбросом (стоп/радио) — он повторит с новой очередью
            return
        token = p.starting = object()
        try:
            while True:
                p.again = False
                await self._play_next(guild, p, failed)
                failed = 0
                vc = self.vc(guild)
                # трек пошёл (starting сброшен) — дальше очередь двигает after; иначе подхватываем отложенный вызов
                if not (p.again and p.starting is token and p.queue and vc and not vc.is_playing()
                        and not vc.is_paused()):
                    break
        finally:
            if p.starting is token:
                p.starting = None

    async def _play_next(self, guild, p, failed):
        gen = p.gen
        while True:
            vc = self.vc(guild)
            if not vc or p.gen != gen:
                return
            if p.loop == "track" and p.current and not failed:
                track = p.current
            else:
                if p.loop == "queue" and p.current and not p.current.radio and not failed:
                    p.queue.append(p.current)
                # автоплей — только если в войсе есть люди: пустой комнате музыка не нужна
                if not p.queue and not failed and p.current and not p.current.radio and self.autoplay_on(guild) \
                        and vc.channel and self.humans(guild, vc.channel.id):
                    await self.add_related(guild, p.current)
                    if p.gen != gen:
                        return
                if not p.queue:
                    p.current = None
                    await self.update_panel(guild)
                    self.schedule_leave(guild, 300)
                    return
                track = p.queue.popleft()
            p.current = track
            p.remember(track)
            try:
                src = await self.source(track, p.volume)
                break
            except Exception as e:
                log.warning("Не удалось воспроизвести %s: %s", track.title, e)
                p.current = None
                if p.gen != gen:
                    return
                if p.text:
                    try:
                        await p.text.send(f"⚠️ Не удалось включить **{track.title}** — пропускаю.", delete_after=30)
                    except discord.HTTPException:
                        pass
                if track.auto and not p.queue and failed < 2:
                    await self.add_related(guild, track)  # автоплей не сдаётся с первой осечки
                if failed >= 3:
                    await self.update_panel(guild)
                    return
                failed += 1

        def after(err):
            if err:
                log.warning("Ошибка плеера: %s", err)
            asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

        # Пока грузили трек, могло случиться: «стоп» (gen), обрыв связи (ждём переподключения до 5 с),
        # чужой звук — объявление/фраза (ждём, пока договорит, до 2 мин).
        waited = 0.0
        while True:
            if p.gen != gen:
                src.cleanup()
                return
            vc = self.vc(guild)
            ok = bool(vc and vc.is_connected())
            lock = self.locks.get(guild.id)  # объявление ходит по войсам — между фразами тишина, но не наша
            if ok and not vc.is_playing() and not vc.is_paused() and not (lock and lock.locked()):
                break
            if waited >= (120 if ok else 5):
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        if p.leave_task and not p.leave_alone:
            p.leave_task.cancel()  # таймер «остался один» не трогаем — пустой комнате музыка не нужна
            p.leave_task = None
        try:
            if not vc or not vc.is_connected():
                raise disnake.ClientException("Not connected to voice.")
            if vc.is_playing() or vc.is_paused():
                src.cleanup()  # чужой звук так и не закончился — трек остаётся первым в очереди
                p.queue.appendleft(track)
                p.current = None
                await self.update_panel(guild)
                return
            vc.play(src, after=after)
            p.starting = None  # трек пошёл: если он сразу оборвётся, его after должен пройти
        except disnake.ClientException as e:
            log.warning("Плеер: голос недоступен (%s) — трек %s вернул в очередь", e, track.title)
            try:
                src.cleanup()  # гасим уже запущенный ffmpeg
            except Exception:
                pass
            p.queue.appendleft(track)
            p.current = None
            if p.text:
                try:
                    await p.text.send("⚠️ Связь с голосовым каналом оборвалась — трек оставил в очереди. "
                                      "Зайди в войс и нажми `/играть` ещё раз.", delete_after=60)
                except discord.HTTPException:
                    pass
            await self.update_panel(guild)
            return
        await self.update_panel(guild)

    def autoplay_on(self, guild):
        return self.bot.db.gget(guild, "music:autoplay") != "0"  # по умолчанию включён

    async def add_related(self, guild, track):
        """Автоплей: кладёт в очередь один похожий трек. False — ничего не нашлось."""
        p = self.player(guild)
        gen = p.gen
        try:
            found = await asyncio.wait_for(asyncio.to_thread(_related, track, set(p.history)), timeout=40)
        except Exception as e:
            log.info("Автоплей не нашёл похожего: %s", e)
            return False
        if not found or p.gen != gen:  # пока искали, нажали «стоп» — в очередь ничего не кладём
            return False
        title, url, dur, thumb, artist = random.choice(found[:5])  # не всегда первый — меньше зацикливания
        p.queue.append(Track(title, url, None, dur, thumb=thumb, artist=artist, auto=True))
        return True

    def schedule_leave(self, guild, delay, alone=False):
        p = self.player(guild)
        if p.leave_task:
            p.leave_task.cancel()
        p.leave_alone = alone

        async def leave():
            await asyncio.sleep(delay)
            vc = self.vc(guild)
            nobody = vc and vc.channel and not self.humans(guild, vc.channel.id)
            if guild.id in self.hold and not (alone and nobody):
                return  # бот слушает команды — остаёмся
            if vc and ((alone and nobody) or not vc.is_playing()):
                p.reset()  # до disconnect: его after и идущий подбор трека не должны ничего включать
                await vc.disconnect()
                await self.update_panel(guild)
        p.leave_task = asyncio.create_task(leave())

    def panel_embed(self, guild):
        p = self.player(guild)
        vc = self.vc(guild)
        if not p.current:
            return self.bot.embed("🎶 Музыка", "Сейчас ничего не играет. `/играть` или `/радио`", config.COLOR, guild)
        t = p.current
        e = self.bot.embed(f"{'📻 Радио' if t.radio else '🎶 Сейчас играет'}",
                           f"**[{t.title}]({t.url})**" if t.url.startswith("http") and not t.radio else f"**{t.title}**",
                           config.COLOR, guild)
        e.add_field(name="Длительность", value=fmt_dur(t.duration), inline=True)
        e.add_field(name="Включил", value="🎲 автоплей" if t.auto else (t.requester.mention if t.requester else "—"),
                    inline=True)
        e.add_field(name="Повтор · громкость", value=f"{LOOP_NAMES[p.loop]} · {int(p.volume * 100)}%", inline=True)
        if not t.radio:
            auto = "вкл — после очереди включу похожее" if self.autoplay_on(guild) else "выкл"
            e.set_footer(text=f"{e.footer.text or ''} · 🎲 Автоплей: {auto}".lstrip(" ·"), icon_url=e.footer.icon_url)
        if p.queue:
            e.add_field(name=f"Дальше ({len(p.queue)})",
                        value="\n".join(f"`{i + 1}.` {x.title[:60]}" for i, x in enumerate(list(p.queue)[:5])), inline=False)
        if vc and vc.is_paused():
            e.title = "⏸ Пауза"
        if t.thumb:
            e.set_thumbnail(url=t.thumb)
        return e

    async def update_panel(self, guild):
        p = self.player(guild)
        if not p.panel:
            return
        try:
            await p.panel.edit(embed=self.panel_embed(guild), view=MusicControls() if p.current else None)
        except discord.HTTPException:
            p.panel = None

    async def show_panel(self, inter):
        p = self.player(inter.guild)
        p.text = inter.channel
        if p.panel:
            try:
                await p.panel.delete()
            except discord.HTTPException:
                pass
        p.panel = await inter.channel.send(embed=self.panel_embed(inter.guild), view=MusicControls())

    def same_voice(self, inter):
        vc = self.vc(inter.guild)
        return (vc and inter.user.voice and inter.user.voice.channel.id == vc.channel.id) or \
            self.bot.has(inter.user, config.LEAD)

    # ---------- команды ----------
    @app_commands.command(name="играть", description="Включить музыку по названию или ссылке")
    @app_commands.rename(query="что")
    @app_commands.describe(query="Название песни или ссылка (YouTube, SoundCloud, плейлист…)")
    async def play(self, inter: discord.Interaction, query: str):
        await inter.response.defer()
        vc = await self.join(inter)
        if not vc:
            return
        p = self.player(inter.guild)
        is_url = re.match(r"https?://", query)
        playlist = bool(is_url and "list=" in query)
        try:
            info = await asyncio.to_thread(_extract, query if is_url else f"scsearch1:{query}", playlist)
        except Exception as e:
            return await inter.followup.send("❌ Не получилось включить. YouTube блокирует хостинги — напиши название песни (ищу на SoundCloud), дай ссылку SoundCloud или включи /радио." if "Sign in" in str(e) else f"❌ Не нашёл: {str(e)[:200]}")
        entries = info.get("entries") if info.get("entries") is not None else [info]
        added = []
        for en in entries:
            if not en:
                continue
            url = en.get("webpage_url") or en.get("url")
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            added.append(Track(en.get("title") or "Без названия", url, inter.user, en.get("duration"),
                               thumb=en.get("thumbnail")))
        if not added:
            return await inter.followup.send("❌ Ничего не нашлось.")
        p.queue.extend(added)
        text = f"➕ В очередь: **{added[0].title}**" if len(added) == 1 else f"➕ В очередь добавлено треков: {len(added)}"
        await inter.followup.send(text)
        if p.current and p.current.radio and vc.is_playing():
            vc.stop()
        elif not vc.is_playing() and not vc.is_paused():
            await self.play_next(inter.guild)
        await self.show_panel(inter)

    async def enqueue(self, guild, member, query, text_channel=None):
        """Для помощника: включить музыку в войсе, где сидит member. Возвращает текст результата."""
        if not member.voice or not member.voice.channel:
            return "сначала зайди в голосовой канал"
        if not self.dn or not self.dn.is_ready():
            return "голосовой модуль ещё запускается"
        ch = self.dn.get_channel(member.voice.channel.id)
        vc = self.vc(guild)
        try:
            if vc and vc.channel.id != ch.id:
                if vc.is_playing() and self.humans(guild, vc.channel.id):
                    return f"я уже играю в <#{vc.channel.id}>"
                await vc.move_to(ch)
            elif not vc:
                vc = await ch.connect()
        except Exception as e:
            return f"не могу подключиться: {e}"
        p = self.player(guild)
        if text_channel:
            p.text = text_channel
        is_url = re.match(r"https?://", query)
        try:
            info = await asyncio.to_thread(_extract, query if is_url else f"scsearch1:{query}", False)
        except Exception as e:
            return f"не нашёл: {str(e)[:120]}"
        entries = info.get("entries") if info.get("entries") is not None else [info]
        added = []
        for en in entries:
            if en:
                url = en.get("webpage_url") or en.get("url")
                if url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"
                added.append(Track(en.get("title") or "Без названия", url, member, en.get("duration"),
                                   thumb=en.get("thumbnail")))
        if not added:
            return "ничего не нашлось"
        p.queue.extend(added)
        if p.current and p.current.radio and vc.is_playing():
            vc.stop()
        elif not vc.is_playing() and not vc.is_paused():
            await self.play_next(guild)
        return f"включаю **{added[0].title}**"

    @app_commands.command(name="радио", description="Включить радиостанцию")
    @app_commands.rename(station="станция", url="своя_ссылка")
    @app_commands.choices(station=[app_commands.Choice(name=n, value=n) for n in list(config.RADIO)[:25]])
    async def radio(self, inter: discord.Interaction, station: app_commands.Choice[str] = None, url: str = None):
        if not station and not url:
            return await inter.response.send_message("Выбери станцию или вставь ссылку на поток.", ephemeral=True)
        await inter.response.defer()
        vc = await self.join(inter)
        if not vc:
            return
        p = self.player(inter.guild)
        p.reset()  # и прерывает подбор трека, если он шёл — иначе тот включился бы вместо радио
        p.loop = "off"
        name = station.value if station else "Своё радио"
        p.queue.append(Track(name, url or config.RADIO[station.value], inter.user, radio=True))
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await self.play_next(inter.guild)
        await inter.followup.send(f"📻 Включаю **{name}**")
        await self.show_panel(inter)

    async def control(self, inter, action):
        vc = self.vc(inter.guild)
        p = self.player(inter.guild)
        if not vc:
            return await inter.response.send_message("Я сейчас не в голосовом канале.", ephemeral=True)
        if not self.same_voice(inter):
            return await inter.response.send_message("Зайди в мой голосовой канал, чтобы управлять музыкой.",
                                                     ephemeral=True)
        msg = None
        if action == "toggle":
            if vc.is_paused():
                vc.resume()
                msg = "▶️ Продолжаю"
            elif vc.is_playing():
                vc.pause()
                msg = "⏸ Пауза"
        elif action == "skip":
            p.loop = "off" if p.loop == "track" else p.loop
            vc.stop()
            msg = "⏭ Пропущено"
        elif action == "stop":
            p.reset()
            p.loop = "off"
            vc.stop()
            if inter.guild.id not in self.hold:  # «Джарвис» слушает — музыку выключаем, но из войса не выходим
                await vc.disconnect()
            msg = "⏹ Остановлено"
        elif action == "loop":
            p.loop = {"off": "track", "track": "queue", "queue": "off"}[p.loop]
            msg = f"🔁 Повтор: {LOOP_NAMES[p.loop]}"
        elif action == "shuffle":
            q = list(p.queue)
            random.shuffle(q)
            p.queue = deque(q)
            msg = "🔀 Очередь перемешана"
        elif action == "auto":
            on = not self.autoplay_on(inter.guild)
            self.bot.db.gset(inter.guild, "music:autoplay", "1" if on else "0")
            msg = "🎲 Автоплей включён — когда очередь кончится, поставлю похожее." if on else "🎲 Автоплей выключен."
        await inter.response.send_message(msg or "Ок", ephemeral=True)
        await self.update_panel(inter.guild)

    @mus.command(name="пауза", description="Пауза / продолжить")
    async def m_pause(self, inter: discord.Interaction):
        await self.control(inter, "toggle")

    @mus.command(name="скип", description="Следующий трек")
    async def m_skip(self, inter: discord.Interaction):
        await self.control(inter, "skip")

    @mus.command(name="стоп", description="Остановить и выйти из канала")
    async def m_stop(self, inter: discord.Interaction):
        await self.control(inter, "stop")

    @mus.command(name="перемешать", description="Перемешать очередь")
    async def m_shuffle(self, inter: discord.Interaction):
        await self.control(inter, "shuffle")

    @mus.command(name="повтор", description="Режим повтора")
    @app_commands.rename(mode="режим")
    @app_commands.choices(mode=[app_commands.Choice(name=v, value=k) for k, v in LOOP_NAMES.items()])
    async def m_loop(self, inter: discord.Interaction, mode: app_commands.Choice[str]):
        self.player(inter.guild).loop = mode.value
        await inter.response.send_message(f"🔁 Повтор: {mode.name}", ephemeral=True)
        await self.update_panel(inter.guild)

    @mus.command(name="громкость", description="Громкость 5–150%")
    @app_commands.rename(volume="процент")
    async def m_volume(self, inter: discord.Interaction, volume: app_commands.Range[int, 5, 150]):
        self.player(inter.guild).volume = volume / 100
        vc = self.vc(inter.guild)
        if vc and vc.source is not None and hasattr(vc.source, "volume"):
            vc.source.volume = volume / 100
        await inter.response.send_message(f"🔊 Громкость {volume}%", ephemeral=True)
        await self.update_panel(inter.guild)

    @mus.command(name="очередь", description="Показать очередь")
    async def m_queue(self, inter: discord.Interaction):
        await inter.response.send_message(embed=self.panel_embed(inter.guild), ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        vc = self.vc(member.guild)
        if not vc or not vc.channel:
            return
        if not self.humans(member.guild, vc.channel.id):
            p = self.player(member.guild)
            # таймер уже идёт — не сбрасываем его каждым событием в войсах, иначе бот не выйдет никогда
            if not (p.leave_task and not p.leave_task.done() and p.leave_alone):
                self.schedule_leave(member.guild, 60, alone=True)

    # ═════════════ ГОЛОСОВЫЕ ОБЪЯВЛЕНИЯ, ТРЕВОГА, СОБЕСЕДОВАНИЕ ═════════════
    def voice_chain(self):
        """Выбранный голос, за ним — запасные мужские (чтобы голос не «прыгал» на женский или на робота)."""
        first = self.bot.db.get("tts:voice") or DEFAULT_VOICE
        if first not in VOICES:
            first = DEFAULT_VOICE
        return [first] + [v for v in BACKUP_VOICES if v != first]

    async def tts(self, text):
        """Озвучка текста бесплатно: нейроголоса Microsoft Edge; Google — только если не ответил ни один."""
        tmp = Path(tempfile.gettempdir())
        chain = self.voice_chain()
        path = tmp / f"tts_{abs(hash((chain[0], text))) % 10 ** 10}.mp3"
        if path.exists() and path.stat().st_size > 0:
            return path
        self.tts_made = getattr(self, "tts_made", 0) + 1
        if self.tts_made % 20 == 1:  # чистим старую озвучку, чтобы не забить диск хостинга
            border = time.time() - 6 * 3600
            for old in tmp.glob("tts_*.mp3"):
                try:
                    if old.stat().st_mtime < border:
                        old.unlink()
                except OSError:
                    pass
        try:
            try:
                import edge_tts
            except ImportError:
                edge_tts = None
            # Один сбой не выключает хороший голос навсегда (раньше после первой ошибки бот до перезапуска
            # говорил «роботом» Google). Пробуем выбранный голос, потом запасные.
            # Таймаут = лежит весь сервис нейроголосов: запасные тоже не ответят, поэтому не ждём каждого по
            # очереди (раньше выходило до 21 с тишины), а сразу говорим через Google и 3 минуты туда не ходим.
            edge_ok = edge_tts and time.monotonic() >= self.voice_down.get("*", 0)
            for voice in (chain if edge_ok else []):
                if time.monotonic() < self.voice_down.get(voice, 0):
                    continue
                try:
                    await asyncio.wait_for(edge_tts.Communicate(text, voice, rate="+4%").save(str(path)), timeout=5)
                    if path.exists() and path.stat().st_size > 0:
                        self.voice_fails[voice] = 0
                        return path
                except asyncio.TimeoutError:
                    self.voice_down["*"] = time.monotonic() + 180
                    log.info("Нейроголоса не отвечают — 3 минуты говорю через Google")
                    break
                except Exception as e:
                    fails = self.voice_fails[voice] = self.voice_fails.get(voice, 0) + 1
                    if fails >= 3:  # голос лежит — минуту не тратим на него время
                        self.voice_down[voice], self.voice_fails[voice] = time.monotonic() + 60, 0
                    log.info("Голос %s не ответил (%s) — пробую следующий", voice, type(e).__name__)
            from gtts import gTTS
            # без таймаута зависший запрос к Google держал замок объявлений — замолкал весь голос бота
            await asyncio.wait_for(asyncio.to_thread(lambda: gTTS(text, lang="ru", timeout=10).save(str(path))),
                                   timeout=15)
            return path
        except Exception:
            path.unlink(missing_ok=True)  # недописанный файл не должен остаться в кэше
            raise

    @app_commands.default_permissions(manage_guild=True)  # служебное: видно только админам
    @app_commands.command(name="голос", description="Выбрать голос, которым говорит бот, и послушать его")
    @app_commands.rename(voice="голос")
    @app_commands.choices(voice=[app_commands.Choice(name=v, value=k) for k, v in VOICES.items()])
    async def voice_cmd(self, inter: discord.Interaction, voice: app_commands.Choice[str]):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Голос бота выбирает руководство.", ephemeral=True)
        self.bot.db.set("tts:voice", voice.value)
        self.voice_fails.clear()
        self.voice_down.clear()
        here = inter.user.voice.channel if inter.user.voice else None
        await inter.response.send_message(f"🗣 Голос бота: **{voice.name}**." + (" Сейчас скажу пару слов." if here else
                                          " Зайди в войс и повтори команду, чтобы послушать."), ephemeral=True)
        if here:
            await self.announce(inter.guild, [here.id], "Ну как вам мой голос? Швейцар на связи, не благодарите.")

    def voice_targets(self, guild, limit=3):
        """Голосовые каналы, где сейчас сидят люди (самые людные — первыми)."""
        afk = self.bot.ch(guild, "afk")
        chans = [c for c in guild.voice_channels if (not afk or c.id != afk.id) and
                 [m for m in c.members if not m.bot]]
        chans.sort(key=lambda c: len([m for m in c.members if not m.bot]), reverse=True)
        return chans[:limit]

    async def _play_file(self, vc, path):
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        src = disnake.PCMVolumeTransformer(disnake.FFmpegPCMAudio(str(path), executable=FFMPEG), volume=1.0)
        vc.play(src, after=lambda e: loop.call_soon_threadsafe(done.set))
        try:
            await asyncio.wait_for(done.wait(), timeout=90)
        except asyncio.TimeoutError:
            vc.stop()

    async def announce(self, guild, channel_ids, text=None, siren=False, priority=False):
        """Заходит в каналы по очереди и проигрывает сирену и/или озвученный текст. Музыку не ломает:
        если играет музыка — объявление пропускается (кроме тревоги, она важнее)."""
        if not self.dn or not self.dn.is_ready() or not channel_ids:
            return False
        lock = self.locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            p = self.player(guild)
            vc = self.vc(guild)
            if vc and vc.is_playing() and p.current:
                if not priority:
                    # в канале с музыкой говорим поверх: музыка на паузу → фраза → музыка дальше
                    if text and not siren and vc.channel.id in channel_ids and Overlay and isinstance(vc.source, Overlay):
                        try:
                            done = vc.source.say(await self.tts(text), asyncio.get_running_loop())
                            await asyncio.wait_for(done.wait(), timeout=60)
                        except Exception as e:
                            log.warning("Фраза поверх музыки: %s", e)
                            return False
                        return True
                    return False
                p.reset()
                vc.stop()
            elif p.starting and not priority and not (vc and vc.channel and vc.channel.id in channel_ids):
                return False  # идёт подбор трека (пауза между песнями) — в чужой войс не уводим
            music_ch = vc.channel.id if vc and vc.channel and (p.current or p.queue or p.starting) else None
            files = []
            if siren:
                files.append(config.ASSETS / "siren.wav")
            if text:
                try:
                    files.append(await self.tts(text))
                except Exception as e:
                    log.warning("Озвучка не удалась: %s", e)
            if not files:
                return False
            for cid in channel_ids:
                ch = self.dn.get_channel(cid)
                if not ch:
                    continue
                try:
                    vc = self.vc(guild)
                    if vc and vc.channel.id != cid:
                        await vc.move_to(ch)
                    elif not vc:
                        vc = await ch.connect()
                    await asyncio.sleep(0.8)
                    for f in files:
                        await self._play_file(self.vc(guild), f)
                except Exception as e:
                    log.warning("Объявление в %s: %s", cid, e)
            vc = self.vc(guild)
            hold = self.hold.get(guild.id)
            if hold and vc:
                if vc.channel.id != hold and self.dn.get_channel(hold):
                    await vc.move_to(self.dn.get_channel(hold))  # вернуться туда, где слушаем
            elif vc and music_ch and (p.current or p.queue or p.starting):
                if vc.channel.id != music_ch and self.dn.get_channel(music_ch):
                    await vc.move_to(self.dn.get_channel(music_ch))  # музыка ждёт — назад к слушателям
            elif vc and not p.current and not p.queue and not p.starting:
                await vc.disconnect()
            return True

    async def alarm(self, guild, text, everywhere=True):
        targets = [c.id for c in self.voice_targets(guild, limit=6)]
        main = self.bot.ch(guild, "main_voice")
        if not targets and main:
            targets = [main.id]
        return await self.announce(guild, targets, text=text, siren=True, priority=True)

    @app_commands.command(name="тревога", description="Сирена и голосовое сообщение во всех войсах, где есть люди")
    @app_commands.rename(text="сообщение")
    @app_commands.default_permissions(manage_messages=True)
    async def alarm_cmd(self, inter: discord.Interaction, text: str):
        if not self.bot.has(inter.user, config.LEAD):
            return await inter.response.send_message("⛔ Только руководство.", ephemeral=True)
        await inter.response.send_message("🚨 Поднимаю тревогу в голосовых каналах…", ephemeral=True)
        ok = await self.alarm(inter.guild, f"Внимание! Тревога! {text}")
        await self.bot.log(inter.guild, f"🚨 Тревога от {inter.user.mention}: {text}", color=0xE53935)
        if not ok:
            await inter.followup.send("Голосовой модуль не готов — попробуй через пару секунд.", ephemeral=True)


async def setup(bot):
    bot.add_view(MusicControls())
    await bot.add_cog(Music(bot))
