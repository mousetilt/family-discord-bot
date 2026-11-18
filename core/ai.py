# -*- coding: utf-8 -*-
"""ИИ для бота. Поддерживает БЕСПЛАТНЫЕ варианты:
  • Groq   — ключ на console.groq.com (GROQ_API_KEY)
  • Gemini — ключ на aistudio.google.com (GEMINI_API_KEY)
  • Ollama — модель на своём ПК, без ключа (OLLAMA_MODEL)
  • Claude (Anthropic) — ПЛАТНО, по желанию (ANTHROPIC_API_KEY или /ии ключ); основным — только по выбору
Без ключа ИИ выключен, а всё остальное в боте работает.

Уровни запросов (флаги ask/ask_json) и какие модели Claude их обслуживают:
  light=True  → CLAUDE_MODEL_FAST (Haiku): модерация, классификация, проверка связи, уточняющие вопросы
  без флага   → CLAUDE_MODEL_FAST (Haiku): обычные ответы, черновики, разбор тем форума
  smart=True  → CLAUDE_MODEL_SMART (Sonnet), при лимите — Haiku: юрист, итоги дел, намерения Швейцара, сводки
  (smart главнее light). У бесплатных провайдеров smart ни на что не влияет, light — как раньше (Groq: младшая
  модель первой). Лимит/ошибка Claude → cool/dead модели и откат на бесплатные Gemini/Groq той же семьи."""
import asyncio
import base64
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
    "claude": [config.CLAUDE_MODEL_FAST, config.CLAUDE_MODEL_SMART],
}
NAMES = {"groq": "Groq (бесплатно)", "gemini": "Google Gemini (бесплатно)", "ollama": "Ollama (локально)",
         "claude": "Claude (Anthropic, платно)"}
FREE = ("gemini", "groq")             # бесплатные — запасные для всех
KEYED = ("groq", "gemini", "claude")  # провайдеры с ключом семьи (guild_settings ai:<провайдер>)
VISION = ("gemini", "claude")         # кто читает картинки
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2023-06-01"
# модели, где включаем серверный запасной вариант при отказе (refusal) — без него запрос просто останавливается
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLAUDE_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1",
                          "claude-fable-5-2")


class AIUnavailable(Exception):
    pass


class AI:
    def __init__(self, db, keys=None, scope=None, primary=None):
        """keys=None — ключи из окружения хоста (домашняя семья). keys={'groq': .., 'gemini': .., 'claude': ..} —
        ключи семьи (/ии ключ); scope — ID её сервера (свой дневной лимит). primary — основной провайдер, выбранный
        семьёй (/ии основной); не задан — AI_PROVIDER, иначе бесплатный."""
        self.db = db
        self.scope = scope
        self.primary = "claude" if primary == "anthropic" else primary
        if keys is None:
            keys = {"groq": config.GROQ_API_KEY, "gemini": config.GEMINI_API_KEY, "claude": config.ANTHROPIC_KEY}
            self.own_keys = False
        else:
            self.own_keys = True
        self.keys = {p: (k or "").strip() for p, k in keys.items() if (k or "").strip()}
        self.provider = self._detect()
        own = {"groq": config.GROQ_MODEL, "gemini": config.GEMINI_MODEL,
               "ollama": config.OLLAMA_MODEL}.get(self.provider)
        self.models = ([own] if own else []) + [m for m in DEFAULTS.get(self.provider, []) if m != own]
        self.cool = {}    # (провайдер, модель) -> до какого момента не трогаем: лимит исчерпан
        self.dead = set() # (провайдер, модель), которых у провайдера больше нет
        self._no_json = set()  # модели Groq без JSON-режима — им шлём обычный запрос
        self._no_search = set()  # модели Gemini без поиска Google (для grounded)
        # бесплатные ИИ как подстраховка: кончился лимит / упал основной — отвечает следующий. Claude запасным
        # не ставим (платный — только по явному выбору основным); у Claude запасные — Gemini, затем Groq.
        self.backups = [p for p in FREE if p != self.provider and self.keys.get(p)] if self.provider else []
        self.backup = self.backups[0] if self.backups else None
        if self.own_keys:
            return
        if self.provider:
            log.info("ИИ: %s, модель %s", NAMES[self.provider], self.models[0])
        else:
            log.info("ИИ выключен (нет ключа в .env) — бот работает без ИИ-функций")

    def _detect(self):
        """Основной провайдер: выбор семьи → AI_PROVIDER → бесплатный → Ollama → Claude (если других нет).
        Выбранный провайдер без ключа не берём — молча работаем на бесплатном."""
        for p in (self.primary, config.AI_PROVIDER):
            if p in KEYED and p in self.keys:
                return p
        if self.own_keys:
            return next((p for p in ("gemini", "groq", "claude") if p in self.keys), None)
        if config.AI_PROVIDER == "ollama":
            return "ollama"
        if "groq" in self.keys:
            return "groq"
        if "gemini" in self.keys:
            return "gemini"
        if config.OLLAMA_MODEL:
            return "ollama"
        if "claude" in self.keys:
            return "claude"
        return None

    @property
    def is_claude(self):
        """Основной ИИ семьи — Claude (под него помечены smart-вызовы, агент, разбор картинок)."""
        return self.provider == "claude"

    @property
    def vision(self):
        """Может ли ИИ семьи читать картинки (Gemini или Claude в цепочке)."""
        return self.on and any(p in VISION for p in [self.provider] + self.backups)

    def tier_models(self, prov, smart=False, light=False):
        """Модели провайдера для уровня запроса (см. шапку модуля)."""
        if prov == "claude":
            fast, strong = config.CLAUDE_MODEL_FAST, config.CLAUDE_MODEL_SMART
            return [strong, fast] if smart and strong != fast else [fast]
        return list(self.models) if prov == self.provider else list(DEFAULTS[prov])

    def active_model(self, smart=False):
        return self.tier_models(self.provider, smart=smart)[0] if self.on else None

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

    async def ask(self, system, prompt, max_tokens=800, smart=False, cache=False, light=False, json_mode=False,
                  images=None):
        """Запрос к ИИ, возвращает текст ответа. Заодно — здоровье для сторожа (ok_ts / fail_since / fail_reason).
        json_mode — просить у провайдера строгий JSON (Gemini responseMimeType, Groq response_format).
        images — [(mime, bytes)]: картинки для разбора (Gemini бесплатно или Claude); smart/light — уровень модели
        (см. шапку модуля); cache — кэш большого system у Claude (у бесплатных ни на что не влияет)."""
        try:
            text = await self._ask(system, prompt, max_tokens, smart, cache, light, json_mode, images)
        except AIUnavailable as e:
            if self.on:
                self.fail_since = getattr(self, "fail_since", None) or time.time()
                self.fail_reason = str(e)[:200]
            raise
        self.ok_ts, self.fail_since = time.time(), None
        return text

    async def _ask(self, system, prompt, max_tokens=800, smart=False, cache=False, light=False, json_mode=False,
                   images=None):
        if not self.on:
            raise AIUnavailable("ИИ не подключён. Руководство может вставить бесплатный ключ Groq или Gemini "
                                "командой /ии ключ" if self.own_keys else
                                "ИИ не подключён. Бесплатный ключ: console.groq.com или aistudio.google.com")
        self._count()
        chain = self.chain(smart, light)
        if images:  # картинки понимают Gemini (бесплатно) и Claude; без них — честный отказ
            chain = [pm for pm in chain if pm[0] in VISION]
            if not chain:
                raise AIUnavailable("Картинки разбирают Gemini (бесплатный ключ) или Claude — /ии ключ. "
                                    "Или пришли текстом.")
        last_err, limited, now = None, False, time.monotonic()
        for prov, model in chain:
            key = (prov, model)
            if key in self.dead:
                continue
            if self.cool.get(key, 0) > now:
                limited = True
                continue
            try:
                opts = {"json_mode": json_mode} if prov in ("groq", "gemini") else {}
                if prov == "claude":
                    opts = {"smart": smart and model == config.CLAUDE_MODEL_SMART, "cache": cache}
                if images:
                    opts["images"] = images
                text = await getattr(self, f"_{prov}")(model, system, prompt, max_tokens, **opts)
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

    def chain(self, smart=False, light=False):
        """Порядок (провайдер, модель) для запроса: основной по уровню → бесплатные запасные."""
        chain = [(self.provider, m) for m in self.tier_models(self.provider, smart, light)]
        if light:  # фоновые задачи (модерация) — сначала на младшие модели, чтобы не съедать лимит основной
            chain.sort(key=lambda pm: "120b" in pm[1])
        for b in self.backups:
            chain += [(b, m) for m in DEFAULTS[b]]
        return chain

    async def ask_json(self, system, prompt, max_tokens=800, smart=False, light=False, images=None, cache=False):
        """Ответ-JSON. Провайдера просим о строгом JSON (меньше «не понял» из-за битых ответов), а разбор всё равно
        терпимый (parse_json) — на случай модели без JSON-режима."""
        raw = await self.ask(system + "\nОтвечай ТОЛЬКО валидным JSON, без пояснений и без ```.", prompt,
                             max_tokens=max_tokens, smart=smart, light=light, json_mode=True, images=images,
                             cache=cache)
        return parse_json(raw)

    # ---------- провайдеры ----------
    async def _post(self, url, payload, headers, timeout=30):
        timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                text = await r.text()
                if r.status in (429, 529):  # 529 — Anthropic перегружен: та же пауза, что и при лимите
                    raise _RateLimited(text[:400])
                if r.status == 400 and "credit balance" in text.lower():  # у ключа Claude кончились деньги
                    raise _RateLimited("credit balance too low - try again in 1h")
                if r.status in (401, 403) or (r.status == 400 and "api key" in text.lower()):
                    raise AIUnavailable("Ключ ИИ не подходит — проверь его (/ии ключ)")
                low = text.lower()
                # «модели нет» — только явно: 404 или 400 «не найдена / снята». Раньше любое 400 со словом «model»
                # (например, «This model's maximum context length…») выключало основную модель до перезапуска.
                if r.status == 404 or (r.status == 400 and any(w in low for w in (
                        "not found", "does not exist", "decommissioned", "not supported for generatecontent"))):
                    raise _ModelMissing(text[:200])
                if r.status >= 400:
                    raise AIUnavailable(f"ИИ ответил ошибкой {r.status}: {text[:200]}")
                return json.loads(text)

    async def _groq(self, model, system, prompt, max_tokens, json_mode=False):
        payload = {"model": model, "temperature": 0.3,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"
            payload["max_completion_tokens"] = max_tokens + 1500
        else:
            payload["max_tokens"] = max_tokens
        if json_mode and model not in self._no_json:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.keys.get('groq', '')}"}
        try:
            data = await self._post("https://api.groq.com/openai/v1/chat/completions", payload, headers)
        except AIUnavailable as e:
            # JSON-режим не поддержан моделью или ответ не прошёл его проверку — повторяем обычным запросом
            if "response_format" not in payload or not any(w in str(e).lower() for w in ("json", "response_format")):
                raise
            if "response_format" in str(e).lower():
                self._no_json.add(model)
            payload.pop("response_format")
            data = await self._post("https://api.groq.com/openai/v1/chat/completions", payload, headers)
        text = data["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # qwen думает вслух — мысли отрезаем

    async def _gemini(self, model, system, prompt, max_tokens, json_mode=False, images=None):
        parts = [{"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode("ascii")}}
                 for mime, raw in (images or [])] + [{"text": prompt}]
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"maxOutputTokens": max_tokens + 2048, "temperature": 0.3}}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        data = await self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                payload, {"x-goog-api-key": self.keys.get("gemini", "")})
        cands = data.get("candidates") or []
        parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    EMBED_MODEL = "gemini-embedding-001"
    EMBED_DIM = 256  # укороченные векторы: в 3 раза меньше памяти и счёта, точность для поиска по правилам та же

    async def embed(self, texts, query=False):
        """Смысловые векторы (бесплатно на ключе Gemini) → [[float]*EMBED_DIM] в том же порядке. Нет ключа — AIUnavailable.
        query=True — вектор вопроса (у Gemini разные режимы для вопроса и для документа)."""
        key = self.keys.get("gemini")
        if not key:
            raise AIUnavailable("Смысловой поиск работает на бесплатном ключе Gemini (/ии ключ)")
        out = []
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        for i in range(0, len(texts), 100):  # не больше 100 текстов за запрос
            reqs = [{"model": f"models/{self.EMBED_MODEL}", "content": {"parts": [{"text": t[:6000]}]},
                     "taskType": task, "outputDimensionality": self.EMBED_DIM} for t in texts[i:i + 100]]
            data = await self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.EMBED_MODEL}"
                                    ":batchEmbedContents", {"requests": reqs}, {"x-goog-api-key": key})
            out += [e.get("values") or [] for e in data.get("embeddings") or []]
        if len(out) != len(texts):
            raise AIUnavailable("Эмбеддинги вернулись не полностью")
        return out

    async def grounded(self, system, prompt, max_tokens=600):
        """Ответ Gemini с поиском Google (бесплатно на ключе Gemini) → (текст, [(название, ссылка)]).
        Нет ключа Gemini или поиск не ответил — AIUnavailable."""
        key = self.keys.get("gemini")
        if not key:
            raise AIUnavailable("Для поиска в интернете нужен бесплатный ключ Gemini (/ии ключ)")
        last, now = None, time.monotonic()
        for model in DEFAULTS["gemini"]:
            k = ("gemini", model)
            if k in self.dead or k in self._no_search or self.cool.get(k, 0) > now:
                continue
            payload = {"systemInstruction": {"parts": [{"text": system}]},
                       "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "tools": [{"google_search": {}}],
                       "generationConfig": {"maxOutputTokens": max_tokens + 2048, "temperature": 0.3}}
            try:
                data = await self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                        payload, {"x-goog-api-key": key})
            except _ModelMissing as e:  # модель есть, но без поиска — для обычных ответов её не трогаем
                self._no_search.add(k)
                last = e
                continue
            except _RateLimited as e:
                self.cool[k] = now + retry_after(str(e))
                last = e
                continue
            except (AIUnavailable, aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                last = e
                continue
            cand = (data.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
            chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
            src = [((c.get("web") or {}).get("title", ""), (c.get("web") or {}).get("uri", "")) for c in chunks
                   if c.get("web")]
            if text.strip():
                self._count()
                return text.strip(), src
        raise AIUnavailable(f"Поиск не ответил: {str(last)[:120]}")

    async def _ollama(self, model, system, prompt, max_tokens):
        payload = {"model": model, "stream": False, "options": {"num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        try:
            data = await self._post(config.OLLAMA_URL.rstrip("/") + "/api/chat", payload, {})
        except aiohttp.ClientConnectorError:
            raise AIUnavailable("Ollama не запущена на этом ПК")
        return (data.get("message") or {}).get("content", "")

    async def _claude(self, model, system, prompt, max_tokens, smart=False, cache=False, images=None):
        """Claude через Messages API (сырой HTTP, как остальные провайдеры — общий _post с его ошибками и лимитами).
        Haiku/Sonnet 5.5 думают сами (adaptive) — даём запас токенов на мысли, глубину держим effort: low для рутины,
        medium для smart. temperature не шлём (у этих моделей — 400). JSON — инструкцией (ask_json) + parse_json."""
        content = [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                "data": base64.b64encode(raw).decode("ascii")}}
                   for mime, raw in (images or [])] + [{"type": "text", "text": prompt}]
        sys_block = {"type": "text", "text": system}
        if cache:  # большой стабильный system (документы юриста, промпт Швейцара) — кэш, ~10% цены на повторах
            sys_block["cache_control"] = {"type": "ephemeral"}
        payload = {"model": model, "max_tokens": max_tokens + (4000 if smart else 1500),
                   "system": [sys_block], "messages": [{"role": "user", "content": content}],
                   "output_config": {"effort": "medium" if smart else "low"}}
        headers = {"x-api-key": self.keys.get("claude", ""), "anthropic-version": CLAUDE_VERSION}
        if model in CLAUDE_FALLBACK_MODELS:
            headers["anthropic-beta"] = CLAUDE_FALLBACK_BETA
            payload["fallbacks"] = "default"
        data = await self._post(CLAUDE_URL, payload, headers, timeout=90 if smart else 45)
        if data.get("stop_reason") == "refusal":
            raise AIUnavailable("Claude отказался отвечать на этот запрос")
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        if not text.strip():
            raise AIUnavailable(f"Claude не дал текста (stop_reason={data.get('stop_reason')})")
        return text


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
