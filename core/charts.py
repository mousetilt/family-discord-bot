# -*- coding: utf-8 -*-
"""Простые графики на Pillow (без matplotlib — тот съедает 50–80 МБ памяти на хосте с 1 ГБ).
• line_png(точки, заголовок) — линия (баланс казны по дням).
• bars_png(подписи, значения, заголовок) — горизонтальные столбики (топ активности).
Рисовать в потоке: await asyncio.to_thread(line_png, ...). Шрифты — из assets/fonts (кириллица)."""
import io

from PIL import Image, ImageDraw

from .images import font

W, H = 900, 420
BG, GRID, INK, MUTED = (24, 26, 33), (52, 56, 68), (232, 226, 214), (150, 150, 160)
GOLD, RED = (212, 175, 55), (229, 57, 53)


def _short(n):
    a = abs(n)
    if a >= 1_000_000:
        return f"{n / 1_000_000:.1f}кк".replace(".0кк", "кк")
    if a >= 1000:
        return f"{n / 1000:.0f}к"
    return str(int(n))


def line_png(points, title):
    """points — [(подпись по X, значение)], 2+ точки. → PNG (bytes)."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((24, 16), title, font=font("Montserrat-Bold.ttf", 24), fill=INK)
    if len(points) < 2:
        d.text((W // 2, H // 2), "Мало данных", font=font("Montserrat-Medium.ttf", 22), fill=MUTED, anchor="mm")
        return _png(img)
    left, top, right, bottom = 90, 64, W - 24, H - 48
    vals = [v for _, v in points]
    lo, hi = min(vals + [0]), max(vals + [0])
    if hi == lo:
        hi = lo + 1
    small = font("Montserrat-Medium.ttf", 16)
    for i in range(5):
        y = bottom - (bottom - top) * i / 4
        d.line([(left, y), (right, y)], fill=GRID)
        d.text((left - 10, y), _short(lo + (hi - lo) * i / 4), font=small, fill=MUTED, anchor="rm")
    if lo < 0 < hi:
        y0 = bottom - (bottom - top) * (0 - lo) / (hi - lo)
        d.line([(left, y0), (right, y0)], fill=MUTED)
    step = (right - left) / (len(points) - 1)
    xy = [(left + i * step, bottom - (bottom - top) * (v - lo) / (hi - lo)) for i, (_, v) in enumerate(points)]
    d.line(xy, fill=GOLD, width=4, joint="curve")
    for i, (label, _) in enumerate(points):
        if i % max(1, len(points) // 6) == 0 or i == len(points) - 1:
            d.text((xy[i][0], bottom + 8), str(label), font=small, fill=MUTED, anchor="ma")
    last = vals[-1]
    d.text((right, top - 30), _short(last), font=font("Montserrat-Bold.ttf", 22), fill=RED if last < 0 else GOLD,
           anchor="ra")
    return _png(img)


def bars_png(labels, values, title, unit=""):
    """Горизонтальные столбики, до 10 строк. → PNG (bytes)."""
    rows = list(zip(labels, values))[:10]
    h = max(160, 70 + 38 * len(rows))
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    d.text((24, 16), title, font=font("Montserrat-Bold.ttf", 24), fill=INK)
    top = max([v for _, v in rows] + [1])
    name_f, val_f = font("Montserrat-Medium.ttf", 18), font("Montserrat-Bold.ttf", 18)
    for i, (label, v) in enumerate(rows):
        y = 64 + i * 38
        d.text((24, y + 14), str(label)[:22], font=name_f, fill=INK, anchor="lm")
        x0, x1 = 280, 280 + int((W - 400) * max(v, 0) / top)
        d.rounded_rectangle([x0, y + 2, max(x1, x0 + 4), y + 26], radius=6, fill=GOLD)
        d.text((max(x1, x0 + 4) + 10, y + 14), f"{v:g}{unit}", font=val_f, fill=INK, anchor="lm")
    return _png(img)


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
