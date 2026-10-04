"""Generate assets/icon.ico and icon.png (run once; the result is committed).

A notice sheet with lines of text and a green tick: a procurement that fits.

Usage: python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
ACCENT_TOP = (59, 130, 246)
ACCENT_BOTTOM = (29, 78, 216)
SHEET = (255, 255, 255)
LINES = (191, 207, 236)
TICK_BG = (22, 163, 74)
OUTPUT = Path(__file__).resolve().parents[1] / "assets" / "icon.ico"


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        ratio = y / SIZE
        pairs = zip(ACCENT_TOP, ACCENT_BOTTOM, strict=True)
        color = tuple(round(top + (bottom - top) * ratio) for top, bottom in pairs)
        draw.line([(0, y), (SIZE, y)], fill=(*color, 255))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=255)
    image.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(image)
    left, top, right, bottom = (int(SIZE * k) for k in (0.25, 0.17, 0.71, 0.83))
    draw.rounded_rectangle([left, top, right, bottom], radius=int(SIZE * 0.05), fill=SHEET)
    line_left, line_right = left + int(SIZE * 0.07), right - int(SIZE * 0.07)
    for index, width in enumerate((1.0, 0.8, 1.0, 0.55)):
        y = top + int(SIZE * (0.12 + index * 0.1))
        draw.rounded_rectangle([line_left, y, line_left + int((line_right - line_left) * width), y + int(SIZE * 0.035)],
                               radius=int(SIZE * 0.017), fill=LINES)

    center, radius = (int(SIZE * 0.69), int(SIZE * 0.7)), int(SIZE * 0.15)
    draw.ellipse([center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius], fill=TICK_BG,
                 outline=SHEET, width=int(SIZE * 0.025))
    tick = [(center[0] - radius * 0.45, center[1]), (center[0] - radius * 0.1, center[1] + radius * 0.38),
            (center[0] + radius * 0.5, center[1] - radius * 0.35)]
    draw.line(tick, fill=SHEET, width=int(SIZE * 0.045), joint="curve")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.resize((256, 256), Image.LANCZOS).save(OUTPUT.with_suffix(".png"))
    print(f"saved {OUTPUT}")


if __name__ == "__main__":
    main()
