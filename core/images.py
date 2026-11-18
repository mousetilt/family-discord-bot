# -*- coding: utf-8 -*-
"""Красивые карточки: приветствие, «новый член семьи»."""
import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import config

GOLD_STOPS = [(0.0, (255, 255, 255)), (0.45, (239, 233, 223)), (0.8, (201, 193, 180)), (1.0, (143, 135, 124))]
CREAM = (214, 207, 196)
WHITE = (255, 250, 240)


@lru_cache(maxsize=64)
def font(name, size):
    for path in (config.FONTS / name, "DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _gradient(w, h, stops=GOLD_STOPS):
    grad = Image.new("RGB", (1, max(h, 1)))
    for y in range(h):
        t = y / max(h - 1, 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                k = (t - p0) / (p1 - p0 or 1)
                grad.putpixel((0, y), tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)))
                break
    return grad.resize((max(w, 1), max(h, 1)))


def text_width(text, f, spacing=0):
    return sum(f.getlength(ch) for ch in text) + spacing * max(len(text) - 1, 0)


def fit_font(text, name, max_w, start, min_size=16, spacing=0):
    size = start
    while size > min_size and text_width(text, font(name, size), spacing) > max_w:
        size -= 2
    return font(name, size)


def draw_text(img, xy, text, f, fill=WHITE, spacing=0, gold=False, shadow=True, anchor_center=False):
    """Текст с тенью, межбуквенным интервалом и (опционально) золотым градиентом."""
    w = int(text_width(text, f, spacing)) + 4
    asc, desc = f.getmetrics()
    h = asc + desc + 4
    x, y = xy
    if anchor_center:
        x = int(x - w / 2)
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    cx = 0
    for ch in text:
        d.text((cx, 0), ch, font=f, fill=255)
        cx += f.getlength(ch) + spacing
    if shadow:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sh.putalpha(mask.filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.75)))
        img.alpha_composite(sh, (x + 2, y + 3))
    fill_img = _gradient(w, h).convert("RGBA") if gold else Image.new("RGBA", (w, h), fill + (255,))
    fill_img.putalpha(mask)
    img.alpha_composite(fill_img, (x, y))
    return w


def wrap(text, f, max_w, max_lines=2):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        if text_width(test, f) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while text_width(lines[-1] + "…", f) > max_w and " " in lines[-1]:
            lines[-1] = lines[-1].rsplit(" ", 1)[0]
        lines[-1] += "…"
    return lines


def load_avatar(data, size):
    try:
        im = Image.open(io.BytesIO(data)) if data else None
        im = im.convert("RGBA") if im else None
    except Exception:
        im = None
    if im is None:
        im = Image.new("RGBA", (size, size), (15, 15, 15, 255))
        d = ImageDraw.Draw(im)
        d.text((size // 2, size // 2), "?", font=font("Montserrat-Bold.ttf", size // 2), fill=(232, 226, 214), anchor="mm")
    return ImageOps.fit(im, (size, size), Image.LANCZOS)


def circle(im, size):
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    out = im.copy()
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def rounded(im, radius):
    w, h = im.size
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius=radius * 4, fill=255)
    out = im.copy()
    out.putalpha(mask.resize((w, h), Image.LANCZOS))
    return out


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def welcome_card(avatar_bytes, title, name, subtitle):
    bg = Image.open(config.ASSETS / "welcome_bg.png").convert("RGBA")
    size = 236
    cx, cy = 185, 200
    glow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - 135, cy - 135, cx + 135, cy + 135), fill=(232, 226, 214, 70))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(18)))
    ring = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((cx - 127, cy - 127, cx + 127, cy + 127), fill=(232, 226, 214, 255))
    bg.alpha_composite(ring)
    av = circle(load_avatar(avatar_bytes, size), size)
    bg.alpha_composite(av, (cx - size // 2, cy - size // 2))

    x, maxw = 392, 650
    draw_text(bg, (x, 84), title.upper(), font("Montserrat-ExtraBold.ttf", 30), spacing=6, gold=True)
    f_name = fit_font(name, "Montserrat-Bold.ttf", maxw, 64, 30)
    draw_text(bg, (x, 134), name, f_name)
    line = Image.new("RGBA", (maxw, 3), (0, 0, 0, 0))
    lg = _gradient(maxw, 3).convert("RGBA")
    fade = Image.linear_gradient("L").rotate(90).resize((maxw, 3))
    lg.putalpha(ImageOps.invert(fade))
    line.alpha_composite(lg)
    bg.alpha_composite(line, (x, 236))
    f_sub = font("Montserrat-Medium.ttf", 26)
    for i, ln in enumerate(wrap(subtitle, f_sub, maxw, 2)):
        draw_text(bg, (x, 256 + i * 36), ln, f_sub, fill=CREAM)
    return _png(bg)
