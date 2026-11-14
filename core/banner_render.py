# -*- coding: utf-8 -*-
"""Отрисовка баннера-табло. Запускается ОТДЕЛЬНЫМ процессом (python3 core/banner_render.py out.png total lead senior
[новость]) — чтобы память, занятая рисованием, возвращалась системе, а не оставалась в боте. Только Pillow."""
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 960, 540
FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")


def font(name, size):
    try:
        return ImageFont.truetype(os.path.join(FONTS, name), size)
    except OSError:
        return ImageFont.load_default()


def fit(d, text, name, size, width):
    """Шрифт, с которым строка влезает в ширину."""
    while size > 14 and d.textlength(text, font=font(name, size)) > width:
        size -= 2
    return font(name, size)


def render(path, total, lead, senior, news=""):
    glow = ImageChops.invert(Image.radial_gradient("L")).resize((W + 200, H + 240)).crop((100, 120, W + 100, H + 120))
    img = Image.composite(Image.new("RGB", (W, H), (58, 9, 15)), Image.new("RGB", (W, H), (6, 3, 5)), glow)
    d = ImageDraw.Draw(img)
    gold, cream, red, line = (200, 170, 120), (245, 240, 230), (225, 60, 70), (170, 135, 85)

    def c(text, fnt, cx, y, fill):
        d.text((cx - d.textlength(text, font=fnt) / 2, y), text, font=fnt, fill=fill)

    c("УЛЬЯНОВСКАЯ ДИНАСТИЯ", font("Montserrat-Bold.ttf", 34), 480, 22, gold)
    d.line((250, 76, 710, 76), fill=line, width=2)
    for cx, num, lab in ((185, total, "УЧАСТНИКИ"), (480, lead, "РУКОВОДСТВО"), (775, senior, "СТАРШИЕ")):
        c(str(num), fit(d, str(num), "RuslanDisplay.ttf", 250, 270), cx, 92, cream)
        c(lab, font("Montserrat-Bold.ttf", 30), cx, 352, gold)
    d.line((60, 400, 900, 400), fill=line, width=2)
    text = f"НОВОСТЬ: {news}" if news else "Честь · Верность · Семья"
    c(text, fit(d, text, "Montserrat-Bold.ttf", 40, 860), 480, 420, cream)
    if news:
        c("Честь · Верность · Семья", font("Montserrat-Bold.ttf", 28), 480, 482, red)
    img.save(path, "PNG", optimize=True)


if __name__ == "__main__":
    a = sys.argv
    render(a[1], a[2], a[3], a[4], a[5] if len(a) > 5 else "")
