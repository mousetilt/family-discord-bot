# -*- coding: utf-8 -*-
"""ИИ для бота. Поддерживает БЕСПЛАТНЫЕ варианты:
  • Groq   — ключ на console.groq.com (GROQ_API_KEY)
  • Gemini — ключ на aistudio.google.com (GEMINI_API_KEY)
  • Ollama — модель на своём ПК, без ключа (OLLAMA_MODEL)
  • Anthropic (Claude) — платно, по желанию (ANTHROPIC_API_KEY)
Без ключа ИИ выключен, а всё остальное в боте работает."""
import asyncio
import datetime as dt
import json
import logging
import re
import time

import aiohttp

from . import config

log = logging.getLogger("family-bot.ai")

DEFAULTS = {
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "gemini": ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite"],
    "ollama": ["qwen2.5:7b"],
    "anthropic": ["claude-haiku-5-5"],
}
NAMES = {"groq": "Groq (бесплатно)", "gemini": "Google Gemini (бесплатно)", "ollama": "Ollama (локально)",
         "anthropic": "Claude (платно)"}


class AIUnavailable(Exception):
    pass


class AI:
    def __init__(self, db, keys=None, scope=None):
        """keys=None — ключи из окружения хоста (домашняя семья). keys={'groq': .., 'gemini': ..} — ключи семьи,
        введённые через /ии ключ; scope — ID её сервера (свой дневной лимит)."""
        self.db = db
        self.scope = scope
        if keys is None:
            keys = {"groq": config.GROQ_API_KEY, "gemini": config.GEMINI_API_KEY}
            self.own_keys = False
        else:
            self.own_keys = True
        self.keys = {p: (k or "").strip() for p, k in keys.items() if (k or "").strip()}
        self.provider = self._detect()
        own = {"groq": config.GROQ_MODEL, "gemini": config.GEMINI_MODEL, "ollama": config.OLLAMA_MODEL,
               "anthropic": config.AI_MODEL}.get(self.provider)
        self.models = ([own] if own else []) + [m for m in DEFAULTS.get(self.provider, []) if m != own]
        self._anthropic = None
        self.cool = {}    # (провайдер, модель) -> до какого момента не трогаем: лимит исчерпан
        self.dead = set() # (провайдер, модель), которых у провайдера больше нет
        # второй бесплатный ИИ как подстраховка: кончился лимит у одного — отвечает другой
        self.backup = ("gemini" if self.provider == "groq" and self.keys.get("gemini") else
                       "groq" if self.provider == "gemini" and self.keys.get("groq") else None)
        if self.own_keys:
            return
        if self.provider:
            log.info("ИИ: %s, модель %s", NAMES[self.provider], self.models[0])
        else:
            log.info("ИИ выключен (нет ключа в .env) — бот работает без ИИ-функций")

    def _detect(self):
        p = config.AI_PROVIDER
        if self.own_keys:  # у семьи — только бесплатные Groq/Gemini по её ключам
            if p in self.keys:
                return p
            return "gemini" if "gemini" in self.keys else "groq" if "groq" in self.keys else None
        if p in ("groq", "gemini") and p in self.keys:
            return p
        if p in ("ollama", "anthropic"):
            return p
        if "groq" in self.keys:
            return "groq"
        if "gemini" in self.keys:
            return "gemini"
        if config.OLLAMA_MODEL:
            return "ollama"
        if config.ANTHROPIC_KEY:
            return "anthropic"
        return None

    @property
    def on(self):
        return self.provider is not None

    @property
    def name(self):
        return NAMES.get(self.provider, "выключен")

    def _day_key(self):
        day = dt.date.today().isoformat()
        return f"{day}:{self.scope}" if self.scope else day

    def used_today(self):
        row = self.db.one("SELECT calls FROM ai_usage WHERE day=?", self._day_key())
        return row["calls"] if row else 0

    def _count(self):
        key = self._day_key()
        if self.used_today() >= config.AI_DAILY_LIMIT:
            raise AIUnavailable("Дневной лимит запросов к ИИ исчерпан — завтра снова заработает")
        self.db.q("INSERT INTO ai_usage(day, calls) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", key)

    async def probe(self, provider):
        """Проверка ключа одним крошечным запросом → None (всё ок) или текст ошибки."""
        last = "нет ответа"
        for model in DEFAULTS[provider]:
            try:
                await getattr(self, f"_{provider}")(model, "Ответь одним словом.", "Скажи: ок", 16)
                return None
            except _RateLimited:
                return None  # ключ рабочий, просто лимит на эту минуту
            except _ModelMissing as e:
                last = str(e)[:150]
            except AIUnavailable as e:
                return str(e)[:200]
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, KeyError, IndexError) as e:
                last = type(e).__name__
        return f"ни одна модель не ответила ({last})"

    async def ask(self, system, prompt, max_tokens=800, smart=False, cache=False, light=False):
        """Запрос к ИИ, возвращает текст ответа."""
        if not self.on:
            raise AIUnavailable("ИИ не подключён. Руководство может вставить бесплатный ключ Groq или Gemini "
                                "командой /ии ключ" if self.own_keys else
                                "ИИ не подключён. Бесплатный ключ: console.groq.com или aistudio.google.com")
        self._count()
        chain = [(self.provider, m) for m in self.models]
        if light:  # фоновые задачи (модерация) — сначала на младшие модели, чтобы не съедать лимит основной
            chain.sort(key=lambda pm: "120b" in pm[1])
        if self.backup:
            chain += [(self.backup, m) for m in DEFAULTS[self.backup]]
        last_err, limited, now = None, False, time.monotonic()
        for prov, model in chain:
            key = (prov, model)
            if key in self.dead:
                continue
            if self.cool.get(key, 0) > now:
                limited = True
                continue
            try:
                text = await getattr(self, f"_{prov}")(model, system, prompt, max_tokens)
                return (text or "").strip()
            except _ModelMissing as e:
                last_err = e
                self.dead.add(key)
                log.warning("Модель %s недоступна (%s) — больше её не пробую", model, str(e)[:120])
            except _RateLimited as e:  # у бесплатных ИИ лимит свой на каждую модель — пробуем запасную
                last_err, limited = e, True
                wait = retry_after(str(e))
                self.cool[key] = now + wait
                log.warning("Модель %s: лимит исчерпан, не трогаю её %d мин, пробую следующую", model, wait // 60 or 1)
            except AIUnavailable as e:
                last_err = e
                if (prov, model) == chain[-1]:
                    raise
                log.warning("Модель %s: %s — пробую следующую", model, str(e)[:120])
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, KeyError, IndexError) as e:
                last_err = e
                log.warning("Модель %s не ответила (%s) — пробую следующую", model, type(e).__name__)
        if limited:
            raise AIUnavailable("Лимит бесплатного ИИ исчерпан — попробуй через несколько минут или завтра")
        raise AIUnavailable(f"Ни одна модель не ответила: {str(last_err)[:150]}")

    async def ask_json(self, system, prompt, max_tokens=800, smart=False, light=False):
        raw = await self.ask(system + "\nОтвечай ТОЛЬКО валидным JSON, без пояснений и без ```.", prompt,
                             max_tokens=max_tokens, smart=smart, light=light)
        return parse_json(raw)

    # ---------- провайдеры ----------
    async def _post(self, url, payload, headers):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                text = await r.text()
                if r.status == 429:
                    raise _RateLimited(text[:400])
                if r.status in (401, 403) or (r.status == 400 and "api key" in text.lower()):
                    raise AIUnavailable("Ключ ИИ не подходит — проверь его (/ии ключ)")
                if r.status == 404 or (r.status == 400 and "model" in text.lower()):
                    raise _ModelMissing(text[:200])
                if r.status >= 400:
                    raise AIUnavailable(f"ИИ ответил ошибкой {r.status}: {text[:200]}")
                return json.loads(text)

    async def _groq(self, model, system, prompt, max_tokens):
        payload = {"model": model, "temperature": 0.3,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"
            payload["max_completion_tokens"] = max_tokens + 1500
        else:
            payload["max_tokens"] = max_tokens
        data = await self._post("https://api.groq.com/openai/v1/chat/completions", payload,
                                {"Authorization": f"Bearer {self.keys.get('groq', '')}"})
        text = data["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # qwen думает вслух — мысли отрезаем

    async def _gemini(self, model, system, prompt, max_tokens):
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": max_tokens + 2048, "temperature": 0.3}}
        data = await self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                payload, {"x-goog-api-key": self.keys.get("gemini", "")})
        cands = data.get("candidates") or []
        parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    async def _ollama(self, model, system, prompt, max_tokens):
        payload = {"model": model, "stream": False, "options": {"num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        try:
            data = await self._post(config.OLLAMA_URL.rstrip("/") + "/api/chat", payload, {})
        except aiohttp.ClientConnectorError:
            raise AIUnavailable("Ollama не запущена на этом ПК")
        return (data.get("message") or {}).get("content", "")

    async def _anthropic(self, model, system, prompt, max_tokens):
        if self._anthropic is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError:
                raise AIUnavailable("Для Claude установи пакет: pip install anthropic")
            self._anthropic = AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
        r = await self._anthropic.messages.create(model=model, max_tokens=max_tokens, system=system,
                                                  messages=[{"role": "user", "content": prompt}])
        return "".join(getattr(b, "text", "") for b in r.content)


class _ModelMissing(Exception):
    pass


class _RateLimited(Exception):
    pass


def retry_after(text):
    """Сколько секунд провайдер просит подождать («try again in 7m12.5s»); не сказал — минута. Не дольше часа."""
    m = re.search(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", text or "")
    if m and any(m.groups()):
        sec = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
        return int(max(30, min(3600, sec)))
    return 60


def parse_json(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1:] if "\n" in raw else raw
    pairs = [("{", "}"), ("[", "]")]
    firsts = {o: raw.find(o) for o, _ in pairs}
    pairs.sort(key=lambda p: firsts[p[0]] if firsts[p[0]] != -1 else 10 ** 9)
    for opener, closer in pairs:
        s, e = raw.find(opener), raw.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(raw[s:e + 1])
            except json.JSONDecodeError:
                continue
    return None
