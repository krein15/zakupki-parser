"""Record the README/release GIF: an export of open notices by budget, from the click to the cards.

Usage: python tools/record_demo.py OUTPUT.gif

The window works on a temporary profiles folder (as tools/screenshot.py does) and asks the live site for the open
notices of the Tyumen region at 1–5 mln ₽: three requests, a few seconds. Frames are taken every FRAME_MS while the
export runs, then the result cards are scrolled. Identical frames in a row are merged into one longer frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import window_capture  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402
from screenshot import demo_profiles  # noqa: E402

from zkparser.gui import app as window  # noqa: E402
from zkparser.gui.app import App, setup_logging  # noqa: E402
from zkparser.gui.widgets import set_entry  # noqa: E402

FRAME_MS = 400
WIDTH = 1100  # the GIF is scaled down to this width
SCROLL_STEPS = 12


def main() -> None:
    output = Path(sys.argv[1])
    setup_logging()
    folder = demo_profiles()
    window.profiles_dir = lambda: folder
    App._refresh_monitoring = lambda _app: None  # no real bot name on a public picture
    app = App()
    app.appearance.set("Светлая")
    app._set_appearance("Светлая")
    app.period.set("Неделя")
    app._refresh_period()
    app.open_report_switch.deselect()
    app.prefs.output_dir = str(Path(window.default_output_dir()) / "demo")
    app.export_regions = ["72000000000"]
    app._show_export_regions()
    set_entry(app.export_price_from, "1 000 000")
    set_entry(app.export_price_to, "5 000 000")
    set_entry(app.export_words, "")
    app.export_filter.set(window.ALL_NOTICES)
    app.export_details.deselect()
    app.tabs.set("По бюджету")
    frames: list[Image.Image] = []

    def grab() -> None:
        app.update()
        frames.append(window_capture.capture(app))

    def before() -> None:
        for _ in range(4):  # the form for a moment before the click
            grab()
        app._start_export()
        app.after(FRAME_MS, running)

    def running() -> None:
        grab()
        if app._busy() or len(frames) < 8:
            app.after(FRAME_MS, running)
        else:
            app.after(1500, results)

    def results(step: int = 0) -> None:
        grab()
        if step < 4:  # the first cards for a while
            app.after(FRAME_MS, lambda: results(step + 1))
        elif step < 4 + SCROLL_STEPS:
            app.results_list._parent_canvas.yview_moveto((step - 3) * 0.02)
            app.after(FRAME_MS, lambda: results(step + 1))
        else:
            for _ in range(4):
                grab()
            app.destroy()

    app.after(2500, before)
    app.mainloop()
    save_gif(frames, output)
    print(f"{output}: {len(frames)} кадров, {output.stat().st_size // 1024} КБ")


def save_gif(frames: list[Image.Image], output: Path) -> None:
    scaled = [frame.resize((WIDTH, round(frame.height * WIDTH / frame.width)), Image.LANCZOS) for frame in frames]
    merged: list[tuple[Image.Image, int]] = []
    for frame in scaled:
        if merged and ImageChops.difference(frame, merged[-1][0]).getbbox() is None:
            merged[-1] = (merged[-1][0], merged[-1][1] + FRAME_MS)
        else:
            merged.append((frame, FRAME_MS))
    images = [frame.convert("P", palette=Image.ADAPTIVE, colors=128) for frame, _ in merged]
    durations = [duration for _, duration in merged]
    durations[-1] = 2500  # rest on the last frame before the loop starts again
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(output, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)


if __name__ == "__main__":
    main()
