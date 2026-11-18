# -*- coding: utf-8 -*-
"""Шифрование секретов в базе (пароль и куки общего аккаунта форума) — только стандартная библиотека.
Ключ НЕ в базе и НЕ в репозитории: переменная окружения хоста FORUM_SECRET, а если её нет — выводится из
DISCORD_TOKEN (он и так лежит только во вкладке «Окружение»). Утекла база без окружения — пароля в ней не прочесть.
Схема: HMAC-SHA256 в режиме счётчика (поток) + случайный nonce + HMAC-подпись (подмена/порча — обнаруживается).
Сменили токен/FORUM_SECRET — старые секреты не расшифруются: бот попросит ввести вход заново."""
import base64
import hashlib
import hmac
import os

PREFIX = "enc1:"


def _key():
    raw = os.getenv("FORUM_SECRET") or os.getenv("DISCORD_TOKEN") or ""
    if not raw.strip():
        raise RuntimeError("нет ключа шифрования (FORUM_SECRET / DISCORD_TOKEN в окружении)")
    return hashlib.sha256(b"family-bot/secret/v1/" + raw.strip().encode("utf-8")).digest()


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt(text):
    key = _key()
    data = str(text).encode("utf-8")
    nonce = os.urandom(16)
    body = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = hmac.new(key, b"tag" + nonce + body, hashlib.sha256).digest()[:16]
    return PREFIX + base64.urlsafe_b64encode(nonce + tag + body).decode("ascii")


def decrypt(value):
    """Расшифровать; старое значение без шифра (до этой версии) возвращается как есть. Порча/чужой ключ → None."""
    if value is None:
        return None
    if not str(value).startswith(PREFIX):
        return value
    try:
        raw = base64.urlsafe_b64decode(str(value)[len(PREFIX):].encode("ascii"))
        nonce, tag, body = raw[:16], raw[16:32], raw[32:]
        key = _key()
        if not hmac.compare_digest(tag, hmac.new(key, b"tag" + nonce + body, hashlib.sha256).digest()[:16]):
            return None
        return bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body)))).decode("utf-8")
    except Exception:  # noqa: BLE001 — битое значение
        return None
