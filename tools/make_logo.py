"""生成插件列表用的 logo.png（256x256）：暗色卡面 + 红色剑气 + 铁甲战士的小血条。

用法：python tools/make_logo.py
产物：astrbot_plugin_sts2_player/logo.png
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
OUT = Path(__file__).resolve().parent.parent / "logo.png"

BG_TOP = (28, 22, 30)
BG_BOTTOM = (18, 14, 20)
CARD = (46, 36, 44)
CARD_EDGE = (120, 90, 70)
GOLD = (214, 168, 92)
RED = (208, 60, 52)
RED_DARK = (140, 32, 30)
HP = (196, 48, 44)
HP_BACK = (60, 26, 26)


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return img


def main() -> None:
    img = vertical_gradient(SIZE, BG_TOP, BG_BOTTOM).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # 卡片本体
    card = (52, 34, 20, 204)
    draw.rounded_rectangle([46, 26, 210, 214], radius=18, fill=card, outline=CARD_EDGE, width=4)
    draw.rounded_rectangle([54, 34, 202, 206], radius=14, fill=CARD, outline=(90, 68, 56), width=2)

    # 剑气：两条斜线 + 一道弧
    draw.polygon([(78, 168), (96, 176), (176, 78), (168, 62)], fill=RED)
    draw.polygon([(84, 176), (96, 186), (186, 96), (180, 84)], fill=RED_DARK)
    draw.arc([62, 54, 196, 188], start=214, end=326, fill=GOLD, width=6)

    # 顶部小血条（燃烧之血的味道）
    draw.rounded_rectangle([74, 58, 182, 78], radius=8, fill=HP_BACK)
    draw.rounded_rectangle([74, 58, 170, 78], radius=8, fill=HP)
    draw.rounded_rectangle([74, 58, 182, 78], radius=8, outline=(24, 16, 16), width=2)

    # 底部能力槽
    for i in range(3):
        x = 84 + i * 34
        draw.ellipse([x, 186, x + 20, 206], fill=(70, 56, 48), outline=GOLD, width=2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT, "PNG")
    print(f"logo -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
