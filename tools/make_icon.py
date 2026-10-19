"""Generate assets/icon.ico and icon.png from the author's brand icon (run once; the result is committed).

The icon is ">_" of the krein brand kit, green on a dark rounded square (assets/brand/icon-dark-512.png): the window,
the taskbar and the .exe show it, and so does the header of the window.

Usage: python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SOURCE = ASSETS / "brand" / "icon-dark-512.png"
OUTPUT = ASSETS / "icon.ico"
SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    image = Image.open(SOURCE).convert("RGBA")
    image.save(OUTPUT, sizes=SIZES)
    image.resize((256, 256), Image.LANCZOS).save(OUTPUT.with_suffix(".png"))
    print(f"saved {OUTPUT}")


if __name__ == "__main__":
    main()
