"""Render the main window and save a screenshot (used for README images).

Usage: python tools/screenshot.py OUTPUT.png [light|dark] [tab name]

The window works on a temporary profiles folder with one profile made from the "Канцтовары и бумага" template, so the
screenshots do not depend on your own profiles. The "Результаты" tab is filled with the anonymized test notices, so no
network is needed. On the "Мониторинг" tab the bot's name and the chat number are replaced: the screenshots go into a
public repository.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import window_capture  # noqa: E402

from zkparser.gui import app as window  # noqa: E402
from zkparser.gui.app import App, setup_logging  # noqa: E402
from zkparser.gui.widgets import set_entry  # noqa: E402
from zkparser.matching import Matcher  # noqa: E402
from zkparser.notice_xml import parse_notice  # noqa: E402
from zkparser.pipeline import Found, ProfileResult, RunResult  # noqa: E402
from zkparser.profiles import Profile, from_template, load_templates, save_profile  # noqa: E402
from zkparser.website.search import SearchHit  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
DEMO_STAGES = ["Подача заявок", "Подача заявок", "Работа комиссии", "Подача заявок", "Подача заявок"]


def demo_results() -> RunResult:
    profile = Profile(name="Снабжение больниц — Тюмень", regions=("72000000000",),
                      keywords=("томатн* паст*", "лекарственн* препарат*", "топливо"), okpd2=("21.20", "10.39"))
    matcher = Matcher(profile)
    matches = []
    for path, stage in zip(sorted(FIXTURES.glob("notice_*.xml")), DEMO_STAGES, strict=False):
        notice = parse_notice(path.read_bytes())
        verdict = matcher.evaluate(notice)
        if verdict.matched:
            hit = SearchHit(notice.reg_number, notice.url, stage=stage, published=date(2026, 9, 30))
            matches.append(Found(notice, hit, verdict, ("72000000000",)))
    return RunResult([ProfileResult(profile, checked=112, matches=matches)])


def demo_profiles() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="zk-demo-"))
    template = next(t for t in load_templates() if t.name == "Канцтовары и бумага")
    profile = from_template(template, ("72000000000",))
    save_profile(Profile(**{**profile.__dict__, "name": "Канцтовары — Тюмень", "price_from": 10000}),
                 folder / "Канцтовары — Тюмень.toml")
    return folder


def main() -> None:
    output = Path(sys.argv[1])
    appearance = sys.argv[2] if len(sys.argv) > 2 else "light"
    tab = sys.argv[3] if len(sys.argv) > 3 else None
    setup_logging()
    folder = demo_profiles()
    window.profiles_dir = lambda: folder
    if tab == "Мониторинг":
        App._refresh_monitoring = lambda _app: None  # no real bot name or chat id on a public screenshot
    app = App()
    if tab == "Мониторинг":
        app.bot_status.configure(text="Бот @tenders_demo_bot · чат по умолчанию задан")
        app.schedule_status.configure(text="Проверки по расписанию включены. Следующая: 07.10.2026 10:00.\n"
                                           "Бот: слушает кнопки, последняя связь 06.10 19:31\n"
                                           "За 7 дней прислано 14: ✅ беру 3 · ❌ не моё 1 · без отметки 10")
        for var in app.monitored_vars.values():
            var.set(True)
    if tab == "По бюджету":  # the same example as in the README, whatever the window remembers
        app.export_regions = ["72000000000"]
        app._show_export_regions()
        set_entry(app.export_price_from, "1000000")
        set_entry(app.export_price_to, "5000000")
        set_entry(app.export_words, "")
        app.export_filter.set(window.ALL_NOTICES)
        app.export_details.deselect()
    app.period.set("Неделя")  # not the custom dates the window may remember
    app._refresh_period()
    choice = "Светлая" if appearance == "light" else "Тёмная"
    app.appearance.set(choice)
    app._set_appearance(choice)
    if tab:
        app.tabs.set(tab)
    if tab == "Результаты":
        app._show_results(demo_results(), date(2026, 9, 30), date(2026, 9, 30))

    def capture() -> None:
        app.update()
        window_capture.save(app, output)
        app.destroy()

    app.after(2500, capture)
    app.mainloop()


if __name__ == "__main__":
    main()
