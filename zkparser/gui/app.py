"""Main window: what to do on the left, how the work goes — progress, log, report — on the right.

The first tab is all a newcomer needs: "Поиск" asks for a region, a niche and a price and finds the open notices; a
niche is a ready-made template, so the first search needs no words at all. "Результаты" shows what was found and why.
"Профиль" holds the fine tuning — words, strong and minus words, codes, rarely needed filters folded away — and runs
a profile over a period; "Мониторинг" sends new notices of the profiles to Telegram on a schedule.

Network work runs in a background thread; the window learns about its progress through a queue it polls every
100 ms and never touches widgets from that thread.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import webbrowser
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk

from .. import __version__, scheduler
from ..cache import NoticeCache
from ..config import CHAT_KEY, TOKEN_KEY, load_telegram_settings, primary_env_file, save_env_value
from ..display import days_left, moment, money, no_deadline, price_range, regions_count, with_law
from ..examples import Suggestion, load_examples, parse_numbers, suggest, to_profile
from ..excel import build_file_name, export_file_name, export_open_notices, export_profile, moscow_offset
from ..export import LOOKBACK_DAYS, ExportQuery, ExportResult, OpenNotice, export_open
from ..fetch import STOPPED, FetchReport, chosen, problems
from ..filters import FILTERS
from ..monitor import MonitorResult, MonitorState, monitor
from ..monitor_cli import TEST_MESSAGE, bot_line, parse_time, state_path
from ..pipeline import Found, RunResult, run_profiles
from ..profiles import Profile, ProfileError, from_template, load_profile, load_templates, parse_profile, save_profile
from ..regions import REGIONS
from ..scheduler import MOSCOW, SchedulerError
from ..settings import app_data_dir, default_cache_dir, default_output_dir, okpd2_ids_path, profiles_dir
from ..telegram import SecretFilter, TelegramBot, TelegramError
from ..website.client import SiteClient
from ..website.okpd2 import Okpd2Ids
from ..website.search import ALL_LAWS
from . import theme
from .forms import period_text, rubles, safe_file_name, short_path, split_list, without_path
from .preferences import CUSTOM, PERIODS, Preferences, period_dates
from .widgets import (
    ExamplesDialog,
    RegionPicker,
    SectionCard,
    accent_button,
    entry,
    label,
    neutral_button,
    segmented,
    set_entry,
    set_text,
    text_lines,
    textbox,
    wrap_to_width,
)

log = logging.getLogger("zkparser.gui")

LOG_COLORS = {"warning": theme.WARNING, "error": theme.DANGER, "success": theme.SUCCESS}
NEW_PROFILE = "— новый профиль —"
NEW_MENU = "Новый профиль…"
EMPTY_TEMPLATE = "Пустой"
BY_EXAMPLES = "По примерам закупок…"
MAX_RESULT_CARDS = 60
TITLE = "Закупки Парсер"  # the name the window shows; folders and files keep APP_NAME, "Zakupki Parser"
BRAND_URL = "https://kreinstudio.ru/"
ICON_SIZE = (44, 44)
LOGO_SIZE = (233, 46)  # the full logo, 1651×326, with its clear space: the drawing is 213 px wide (the minimum 200)
SEARCH_TAB, RESULTS_TAB, PROFILE_TAB, MONITORING_TAB = "Поиск", "Результаты", "Профиль", "Мониторинг"
ALL_NOTICES = "Все закупки"
MY_PROFILE = "Мой профиль: "  # a saved profile in the niche menu; templates are listed by their names
PROFILE_FILTER, TEMPLATE_FILTER = "Профиль: ", "Шаблон: "  # how the niche menu was remembered before 0.9
MORE = "Дополнительно: типы торгов, только или кроме заказчиков, чат Telegram, этап"
LAW_TITLES = {"44": "44-ФЗ", "223": "223-ФЗ", "615": "ПП РФ 615 (капремонт)"}
FIRST_STEPS = (
    "Как начать:\n"
    "1. Выберите регион.\n"
    "2. Выберите нишу — или оставьте «Все закупки».\n"
    "3. Нажмите «Найти закупки»: отчёт Excel откроется сам.\n\n"
    "Здесь будет виден ход поиска."
)
INTERVALS = ["30", "60", "120"]
ENV_TEMPLATE = (
    "# Настройки Zakupki Parser. Файл не попадает в git: держите в нём токен бота.\n"
    "# Токен выдаёт @BotFather в Telegram, номер чата подскажет кнопка «Найти мой чат».\n"
    f"{TOKEN_KEY}=\n{CHAT_KEY}=\n"
)


class WindowLogHandler(logging.Handler):
    """Shows messages of the core (the site asking to wait, Telegram trouble) in the window's log."""

    def __init__(self, events: queue.Queue) -> None:
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            level = "error"
        else:
            level = "warning" if record.levelno >= logging.WARNING else "info"
        self.events.put(("log", (level, record.getMessage())))


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=theme.APP_BG)
        self.prefs = Preferences.load()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.profile_path: Path | None = None
        self.profile_files: dict[str, Path] = {}
        self.regions: list[str] = []
        self.export_regions: list[str] = [code for code in self.prefs.export_regions if code in REGIONS]
        self.last_report: Path | None = None
        self._filters_loaded = False
        self._closing = False

        self.title(f"{TITLE} {__version__}")
        self._size_window(1240, 820)
        icon = theme.resource_path("assets/icon.ico")
        if icon.exists():
            self.after(250, lambda: self.iconbitmap(str(icon)))  # CTk resets the icon right after start

        self._build_header()
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        content.grid_columnconfigure(0, weight=3, uniform="columns")
        content.grid_columnconfigure(1, weight=2, uniform="columns")
        content.grid_rowconfigure(0, weight=1)
        self.tabs = ctk.CTkTabview(
            content, fg_color="transparent", corner_radius=12, anchor="nw",
            segmented_button_selected_color=theme.ACCENT, segmented_button_selected_hover_color=theme.ACCENT_HOVER,
            segmented_button_unselected_color=theme.NEUTRAL_BUTTON,
            segmented_button_unselected_hover_color=theme.NEUTRAL_BUTTON_HOVER,
            segmented_button_fg_color=theme.NEUTRAL_BUTTON, text_color=theme.TEXT)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_search_tab(self.tabs.add(SEARCH_TAB))
        self._build_results_tab(self.tabs.add(RESULTS_TAB))
        self._build_profile_tab(self.tabs.add(PROFILE_TAB))
        self._build_monitoring_tab(self.tabs.add(MONITORING_TAB))
        self.tabs.set(SEARCH_TAB)
        self._build_run_panel(content)

        self.log_handler = handler = WindowLogHandler(self.events)
        handler.addFilter(SecretFilter(load_telegram_settings().token))
        for name in ("zkparser.website", "zkparser.monitor"):
            logging.getLogger(name).addHandler(handler)

        self._load_profile_list(self.prefs.profile)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(400, self._refresh_monitoring)

    # ------------------------------------------------------------------ layout

    def _size_window(self, width: int, height: int) -> None:
        """Fit the window into the screen: with Windows display scaling the requested size grows."""
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            scaling = 1.0
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(width, int(screen_width / scaling) - 40)
        height = min(height, int(screen_height / scaling) - 90)
        x = max((screen_width - int(width * scaling)) // 2, 0)
        y = max((screen_height - int(height * scaling)) // 3, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(1040, width), min(640, height))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(18, 14))
        icon = _brand_image("icon-dark-512.png", "icon-dark-512.png", ICON_SIZE)
        if icon:
            ctk.CTkLabel(header, image=icon, text="").pack(side="left")
        else:  # sources run without Pillow
            ctk.CTkLabel(header, text=">_", width=44, height=44, corner_radius=12, fg_color="#0A0C0F",
                         text_color="#5CF28E", font=theme.font(17, "bold")).pack(side="left")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", padx=12)
        name = ctk.CTkFrame(titles, fg_color="transparent")
        name.pack(anchor="w")
        ctk.CTkLabel(name, text=TITLE, font=theme.font(21, "bold"), text_color=theme.TEXT).pack(side="left")
        author = ctk.CTkLabel(name, text="by kreinstudio", font=theme.font(13), text_color=theme.TEXT_MUTED,
                              cursor="hand2")
        author.pack(side="left", padx=(8, 0), pady=(6, 0))
        author.bind("<Button-1>", lambda _: webbrowser.open(BRAND_URL))
        ctk.CTkLabel(titles, text="Госзакупки под ваш ассортимент: отчёт в Excel и новые закупки в Telegram",
                     font=theme.font(13), text_color=theme.TEXT_MUTED).pack(anchor="w")
        image = _brand_image("logo-light@2x.png", "logo-dark@2x.png", LOGO_SIZE)
        logo = ctk.CTkLabel(header, image=image, text="", cursor="hand2") if image else None
        if logo:
            logo.pack(side="right")
            logo.bind("<Button-1>", lambda _: webbrowser.open(BRAND_URL))
        self.appearance = segmented(header, ["Светлая", "Тёмная", "Системная"], self._set_appearance)
        self.appearance.set(self.prefs.appearance)
        self.appearance.pack(side="right", padx=(0, 12 if logo else 0))
        self._set_appearance(self.prefs.appearance)

    def _build_search_tab(self, tab: ctk.CTkBaseClass) -> None:
        card = SectionCard(tab, "Найти открытые закупки",
                           f"Те, по которым ещё принимаются заявки, — размещённые за {LOOKBACK_DAYS} дней.")
        card.pack(fill="x")
        grid = ctk.CTkFrame(card.body, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        rows = iter(range(10))

        def field(text: str, widget: ctk.CTkBaseClass, strong: bool = True) -> None:
            row_number = next(rows)
            caption = ctk.CTkLabel(grid, text=text, font=theme.font(13, "bold") if strong else theme.font(12),
                                   text_color=theme.TEXT if strong else theme.TEXT_MUTED, anchor="w")
            caption.grid(row=row_number, column=0, sticky="w", padx=(0, 14), pady=5)
            widget.grid(row=row_number, column=1, sticky="ew", pady=5)

        regions_row = ctk.CTkFrame(grid, fg_color="transparent")
        neutral_button(regions_row, "Выбрать…", self._pick_export_regions, width=100).pack(side="left")
        self.export_regions_label = ctk.CTkLabel(regions_row, text="", font=theme.font(13), text_color=theme.TEXT,
                                                 anchor="w", justify="left", wraplength=440)
        self.export_regions_label.pack(side="left", padx=10, fill="x", expand=True)
        field("1. Регион", regions_row)

        self.export_filter = ctk.CTkOptionMenu(
            grid, values=[ALL_NOTICES], height=34, font=theme.font(13), dropdown_font=theme.font(13),
            fg_color=theme.INPUT_BG, button_color=theme.NEUTRAL_BUTTON, button_hover_color=theme.NEUTRAL_BUTTON_HOVER,
            text_color=theme.TEXT, dynamic_resizing=False)
        self.export_filter.set(ALL_NOTICES)
        field("2. Ниша", self.export_filter)
        self.export_words = entry(grid, "необязательно, например: бумага")
        field("    или слово", self.export_words, strong=False)

        prices = ctk.CTkFrame(grid, fg_color="transparent")
        self.export_price_from = entry(prices, "от, ₽", width=150)
        self.export_price_from.pack(side="left")
        label(prices, "—").pack(side="left", padx=8)
        self.export_price_to = entry(prices, "до, ₽", width=150)
        self.export_price_to.pack(side="left")
        label(prices, "необязательно").pack(side="left", padx=10)
        field("3. Цена", prices)
        laws_row = ctk.CTkFrame(grid, fg_color="transparent")
        self.export_law_vars = self._law_boxes(laws_row, self.prefs.export_laws)
        field("Тип торгов", laws_row, strong=False)

        self.export_button = ctk.CTkButton(card.body, text="Найти закупки", width=260, height=46, corner_radius=12,
                                           font=theme.font(17, "bold"), fg_color=theme.ACCENT,
                                           hover_color=theme.ACCENT_HOVER, command=self._start_export)
        self.export_button.pack(anchor="w", pady=(14, 0))
        self.export_details = ctk.CTkSwitch(card.body, text="с позициями и ИНН заказчиков — дольше",
                                            font=theme.font(12), text_color=theme.TEXT, progress_color=theme.ACCENT)
        self.export_details.pack(anchor="w", pady=(10, 0))

        set_entry(self.export_price_from, self.prefs.export_price_from)
        set_entry(self.export_price_to, self.prefs.export_price_to)
        set_entry(self.export_words, self.prefs.export_words)
        if self.prefs.export_details:
            self.export_details.select()
        self._show_export_regions()

    def _build_profile_tab(self, tab: ctk.CTkBaseClass) -> None:
        self.profile_page = page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        card = SectionCard(page, "Профиль — регионы, слова и коды одной ниши",
                           "По профилю приходят новые закупки в Telegram (вкладка «Мониторинг»). Удобнее всего "
                           "начать с шаблона ниши: меню «Новый профиль…».")
        card.pack(fill="x", padx=(0, 6))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 12))
        # Buttons are packed from the right first; the list of profiles takes whatever width is left.
        neutral_button(row, "Папка", lambda: self._open_path(profiles_dir()), width=72).pack(side="right", padx=(8, 0))
        accent_button(row, "Сохранить", self._save_profile, width=106).pack(side="right", padx=(8, 0))
        self.templates = {template.name: template for template in load_templates()}
        self.template_menu = ctk.CTkOptionMenu(
            row, values=[BY_EXAMPLES, EMPTY_TEMPLATE, *self.templates], command=self._new_from_template, width=150,
            height=34,
            font=theme.font(13), dropdown_font=theme.font(13), fg_color=theme.NEUTRAL_BUTTON,
            button_color=theme.NEUTRAL_BUTTON, button_hover_color=theme.NEUTRAL_BUTTON_HOVER, text_color=theme.TEXT,
            dynamic_resizing=False)
        self.template_menu.set(NEW_MENU)
        self.template_menu.pack(side="right", padx=(8, 0))
        self.profile_menu = ctk.CTkOptionMenu(
            row, values=[NEW_PROFILE], command=self._choose_profile, width=160, height=34, font=theme.font(13),
            dropdown_font=theme.font(13), fg_color=theme.INPUT_BG, button_color=theme.NEUTRAL_BUTTON,
            button_hover_color=theme.NEUTRAL_BUTTON_HOVER, text_color=theme.TEXT, dynamic_resizing=False)
        self.profile_menu.pack(side="left", fill="x", expand=True)

        # Labels on the left of the fields, hints under them.
        hints: list[ctk.CTkLabel] = []

        def form(master: ctk.CTkBaseClass) -> Any:
            grid = ctk.CTkFrame(master, fg_color="transparent")
            # One width for the labels of every block: the folded fields line up with the others.
            grid.grid_columnconfigure(0, minsize=round(170 * ctk.ScalingTracker.get_widget_scaling(grid)))
            grid.grid_columnconfigure(1, weight=1)
            rows = iter(range(20))

            def field(text: str, widget: ctk.CTkBaseClass, hint: str = "") -> None:
                row_number = next(rows)
                label(grid, text).grid(row=row_number, column=0, sticky="nw", padx=(0, 14), pady=(10, 5))
                widget.grid(row=row_number, column=1, sticky="ew", pady=5)
                if hint:
                    note = ctk.CTkLabel(grid, text=hint, font=theme.font(11), text_color=theme.TEXT_MUTED,
                                        anchor="w", justify="left", wraplength=420)
                    note.grid(row=next(rows), column=1, sticky="ew", pady=(0, 4))
                    hints.append(note)

            return grid, field

        grid, field = form(card.body)
        grid.pack(fill="x")
        self.name = entry(grid, "Например: Канцтовары — Тюмень")
        field("Название", self.name)
        regions_row = ctk.CTkFrame(grid, fg_color="transparent")
        neutral_button(regions_row, "Выбрать…", self._pick_regions, width=100).pack(side="left")
        self.regions_label = ctk.CTkLabel(regions_row, text="", font=theme.font(13), text_color=theme.TEXT,
                                          anchor="w", justify="left", wraplength=440)
        self.regions_label.pack(side="left", padx=10, fill="x", expand=True)
        field("Регионы заказчиков", regions_row)
        prices = ctk.CTkFrame(grid, fg_color="transparent")
        self.price_from = entry(prices, "от, ₽", width=150)
        self.price_from.pack(side="left")
        label(prices, "—").pack(side="left", padx=8)
        self.price_to = entry(prices, "до, ₽", width=150)
        self.price_to.pack(side="left")
        field("Начальная цена", prices)

        self.keywords = textbox(grid, 90)
        field("Ключевые слова", self.keywords,
              "По одному на строку, ищутся словоформы: «картридж» найдёт «картриджей»; «канцеляр*» — начало слова; "
              "несколько слов — все в названии или в одной позиции; \"фраза в кавычках\" — точный порядок.")
        self.strong = textbox(grid, 72)
        field("Сильные слова", self.strong,
              "Ключевые слова, которые перевешивают минус-слово рядом с собой: \"доработк* программн*\" оставит "
              "«Доработку программного обеспечения и продление лицензии», хотя «лицензи*» — минус-слово.")
        junk = ctk.CTkFrame(grid, fg_color="transparent")
        junk.grid_columnconfigure((0, 1), weight=1, uniform="junk")
        self.filter_vars: dict[str, ctk.BooleanVar] = {}
        for index, (key, item) in enumerate(FILTERS.items()):
            var = self.filter_vars[key] = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(junk, text=item.title, variable=var, font=theme.font(13), text_color=theme.TEXT,
                            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, checkbox_width=18,
                            checkbox_height=18, corner_radius=5).grid(row=index // 2, column=index % 2, sticky="w",
                                                                      padx=(0, 10), pady=3)
        field("Фильтры мусора", junk,
              "Отметьте, что вам точно не нужно: программа сама отсеет такие закупки — минус-слова для этого "
              "писать не нужно.")
        self.minus = textbox(grid, 72)
        field("Минус-слова", self.minus, "Свои, сверх фильтров. В названии отсеивают закупку, в позиции — только "
                                         "позицию.")
        self.okpd2 = entry(grid, "17.23, 32.99.12")
        field("ОКПД2", self.okpd2, "Начала кодов через запятую: «17.23» — вся группа.")
        self.ktru = entry(grid, "нет")
        field("КТРУ", self.ktru)

        # Rarely needed: folded until opened or until the profile uses one of them.
        self.more_button = ctk.CTkButton(card.body, text="▸ " + MORE, height=28, anchor="w", font=theme.font(13),
                                         fg_color="transparent", hover_color=theme.NEUTRAL_BUTTON,
                                         text_color=theme.ACCENT, command=self._toggle_more)
        self.more_button.pack(fill="x", pady=(8, 0))
        self.more, field = form(card.body)
        self.more_shown = False
        self.customer_inn = entry(self.more, "ИНН через запятую; пусто — любые")
        field("Только заказчики", self.customer_inn)
        self.exclude_inn = entry(self.more, "ИНН через запятую")
        field("Кроме заказчиков", self.exclude_inn)
        self.telegram_chat = entry(self.more, "пусто — чат по умолчанию")
        field("Telegram-чат", self.telegram_chat)
        self.all_stages = ctk.CTkSwitch(self.more, text="любой, а не только подача заявок", font=theme.font(13),
                                        text_color=theme.TEXT, progress_color=theme.ACCENT)
        field("Этап закупки", self.all_stages)
        laws_row = ctk.CTkFrame(self.more, fg_color="transparent")
        self.law_vars = self._law_boxes(laws_row, ALL_LAWS)
        field("Типы торгов", laws_row)
        wrap_to_width(card, hints, margin=220)

        run = ctk.CTkFrame(card.body, fg_color="transparent")
        run.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(run, text="Проверить профиль: закупки, размещённые за", font=theme.font(13, "bold"),
                     text_color=theme.TEXT, anchor="w").pack(fill="x")
        self.period = segmented(run, list(PERIODS), lambda _: self._refresh_period())
        self.period.set(self.prefs.period if self.prefs.period in PERIODS else PERIODS[0])
        self.period.pack(fill="x", pady=(4, 0))
        self.dates = dates = ctk.CTkFrame(run, fg_color="transparent")
        self.date_from = entry(dates, "ДД.ММ.ГГГГ", width=120)
        self.date_from.pack(side="left")
        label(dates, "—").pack(side="left", padx=8)
        self.date_to = entry(dates, "ДД.ММ.ГГГГ", width=120)
        self.date_to.pack(side="left")
        set_entry(self.date_from, self.prefs.date_from)
        set_entry(self.date_to, self.prefs.date_to)
        self.start_button = ctk.CTkButton(run, text="Найти по профилю", width=220, height=40, corner_radius=10,
                                          font=theme.font(15, "bold"), fg_color=theme.ACCENT,
                                          hover_color=theme.ACCENT_HOVER, command=self._start)
        self.start_button.pack(anchor="w", pady=(10, 0))
        self._refresh_period()  # the custom dates are shown only for "Свой период"
        note = "Файлы профилей — в папке profiles. Окно сохраняет файл целиком, без комментариев."
        ctk.CTkLabel(card.body, text=note, font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w").pack(
            fill="x", pady=(10, 0))

    def _law_boxes(self, master: ctk.CTkFrame, chosen: Any) -> dict[str, ctk.BooleanVar]:
        """A checkbox per kind of procurement, in a row."""
        variables = {}
        for law, title in LAW_TITLES.items():
            var = variables[law] = ctk.BooleanVar(value=law in chosen)
            ctk.CTkCheckBox(master, text=title, variable=var, font=theme.font(13), text_color=theme.TEXT,
                            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, checkbox_width=18,
                            checkbox_height=18, corner_radius=5).pack(side="left", padx=(0, 16))
        return variables

    def _toggle_more(self, show: bool | None = None) -> None:
        show = not self.more_shown if show is None else show
        self.more_shown = show
        if show:
            self.more.pack(fill="x", after=self.more_button)
        else:
            self.more.pack_forget()
        self.more_button.configure(text=("▾ " if show else "▸ ") + MORE)

    def _build_results_tab(self, tab: ctk.CTkBaseClass) -> None:
        self.results_header = ctk.CTkLabel(tab, text="Здесь появятся подошедшие закупки: нажмите «Найти закупки».",
                                           font=theme.font(14, "bold"), text_color=theme.TEXT, anchor="w",
                                           justify="left", wraplength=600)
        self.results_header.pack(fill="x", pady=(0, 8))
        wrap_to_width(tab, [self.results_header], margin=10)
        self.results_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.results_list.pack(fill="both", expand=True)

    def _build_monitoring_tab(self, tab: ctk.CTkBaseClass) -> None:
        # Scrolls like the profile: with the token field the tab is taller than a laptop screen fits, and the buttons
        # of the schedule at its bottom must not be cut off.
        self.monitoring_page = page = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        page.pack(fill="both", expand=True)
        bot = SectionCard(page, "Telegram",
                          "1. В Telegram откройте @BotFather, отправьте /newbot и придумайте боту имя — он пришлёт "
                          "токен.\n2. Вставьте токен сюда и нажмите «Подключить».\n3. Напишите своему боту любое "
                          "сообщение и нажмите «Найти мой чат».")
        bot.pack(fill="x", padx=(0, 6), pady=(0, 12))
        connect = ctk.CTkFrame(bot.body, fg_color="transparent")
        connect.pack(fill="x", pady=(0, 8))
        accent_button(connect, "Подключить", self._connect_bot, width=120).pack(side="right", padx=(8, 0))
        self.token_entry = entry(connect, "токен от @BotFather: 123456789:AAE…")
        self.token_entry.configure(show="•")
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.bot_status = ctk.CTkLabel(bot.body, text="Проверяю бота…", font=theme.font(13), text_color=theme.TEXT,
                                       anchor="w", justify="left")
        self.bot_status.pack(fill="x")
        buttons = ctk.CTkFrame(bot.body, fg_color="transparent")
        buttons.pack(fill="x", pady=(8, 0))
        neutral_button(buttons, "Найти мой чат", self._find_chat, width=140).pack(side="left")
        neutral_button(buttons, "Тестовое сообщение", self._test_message, width=170).pack(side="left", padx=8)
        neutral_button(buttons, "Открыть .env", self._open_env, width=120).pack(side="left")

        schedule = SectionCard(page, "Проверки по расписанию",
                               "Планировщик Windows проверяет отмеченные профили и шлёт новые закупки в Telegram.")
        schedule.pack(fill="x", padx=(0, 6))
        self.monitored_frame = ctk.CTkFrame(schedule.body, fg_color="transparent")
        self.monitored_frame.pack(fill="x", pady=(0, 8))
        self.monitored_vars: dict[str, ctk.BooleanVar] = {}
        times = ctk.CTkFrame(schedule.body, fg_color="transparent")
        times.pack(fill="x")
        label(times, "с").pack(side="left")
        self.schedule_from = entry(times, "08:00", width=70)
        self.schedule_from.pack(side="left", padx=(6, 10))
        label(times, "до").pack(side="left")
        self.schedule_to = entry(times, "20:00", width=70)
        self.schedule_to.pack(side="left", padx=(6, 10))
        label(times, "каждые").pack(side="left")
        self.schedule_every = ctk.CTkOptionMenu(times, values=INTERVALS, width=80, height=34, font=theme.font(13),
                                                fg_color=theme.INPUT_BG, button_color=theme.NEUTRAL_BUTTON,
                                                button_hover_color=theme.NEUTRAL_BUTTON_HOVER, text_color=theme.TEXT)
        self.schedule_every.pack(side="left", padx=6)
        label(times, "мин, по Москве").pack(side="left")
        set_entry(self.schedule_from, self.prefs.schedule_from)
        set_entry(self.schedule_to, self.prefs.schedule_to)
        every = str(self.prefs.schedule_every)
        self.schedule_every.set(every if every in INTERVALS else "60")
        self.schedule_status = ctk.CTkLabel(schedule.body, text="", font=theme.font(13), text_color=theme.TEXT,
                                            anchor="w", justify="left")
        self.schedule_status.pack(fill="x", pady=(10, 0))
        actions = ctk.CTkFrame(schedule.body, fg_color="transparent")
        actions.pack(fill="x", pady=(8, 0))
        accent_button(actions, "Включить", self._schedule_on, width=110).pack(side="left")
        neutral_button(actions, "Выключить", self._schedule_off, width=110).pack(side="left", padx=8)
        neutral_button(actions, "Проверить сейчас", self._check_now, width=150).pack(side="left")
        neutral_button(actions, "Журнал", self._open_monitor_log, width=90).pack(side="left", padx=8)

    def _build_run_panel(self, master: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(master, fg_color=theme.CARD_BG, corner_radius=14, border_width=1,
                             border_color=theme.CARD_BORDER)
        panel.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(panel, text="Ход поиска", font=theme.font(14, "bold"), text_color=theme.TEXT,
                     anchor="w").pack(fill="x", padx=20, pady=(14, 6))
        self.stop_button = ctk.CTkButton(panel, text="Остановить", height=38, corner_radius=10, font=theme.font(14),
                                         fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER, command=self._stop)
        self.progress = ctk.CTkProgressBar(panel, height=10, corner_radius=5, progress_color=theme.ACCENT,
                                           fg_color=theme.NEUTRAL_BUTTON)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(4, 4))
        self.status = ctk.CTkLabel(panel, text="Готов к работе", font=theme.font(13), text_color=theme.TEXT_MUTED,
                                   anchor="w")
        self.status.pack(fill="x", padx=20)

        ctk.CTkLabel(panel, text="Журнал", font=theme.font(14, "bold"), text_color=theme.TEXT, anchor="w").pack(
            fill="x", padx=20, pady=(10, 4))

        # The bottom of the panel is packed first and the log last: on a small screen the log gives way, not the
        # report buttons or the folder.
        output = ctk.CTkFrame(panel, fg_color="transparent")
        output.pack(side="bottom", fill="x", padx=20, pady=(4, 12))
        self.output_label = ctk.CTkLabel(output, text="", font=theme.font(12), text_color=theme.TEXT_MUTED,
                                         anchor="w", justify="left", wraplength=300)
        self.output_label.pack(side="left", fill="x", expand=True)
        neutral_button(output, "Изменить…", self._choose_output, width=100).pack(side="right")
        self.open_report_switch = ctk.CTkSwitch(panel, text="Открывать отчёт после поиска", font=theme.font(12),
                                                text_color=theme.TEXT, progress_color=theme.ACCENT)
        if self.prefs.open_report:
            self.open_report_switch.select()
        self.open_report_switch.pack(side="bottom", anchor="w", padx=20, pady=(8, 0))

        self.result_card = ctk.CTkFrame(panel, fg_color=theme.INPUT_BG, corner_radius=12)  # packed after a search
        self.result_title = ctk.CTkLabel(self.result_card, text="", font=theme.font(14, "bold"), text_color=theme.TEXT,
                                         anchor="w", justify="left")
        self.result_title.pack(fill="x", padx=14, pady=(10, 0))
        self.result_path = ctk.CTkLabel(self.result_card, text="", font=theme.font(12), text_color=theme.TEXT_MUTED,
                                        anchor="w", justify="left", wraplength=380)
        self.result_path.pack(fill="x", padx=14)
        buttons = ctk.CTkFrame(self.result_card, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=(6, 10))
        ctk.CTkButton(buttons, text="Открыть отчёт", height=34, font=theme.font(13), fg_color=theme.SUCCESS,
                      hover_color=theme.SUCCESS_HOVER, corner_radius=8, command=self._open_report).pack(side="left")
        neutral_button(buttons, "Показать в папке", self._show_report, width=140).pack(side="left", padx=8)

        self.log_box = ctk.CTkTextbox(panel, height=90, font=ctk.CTkFont(family="Consolas", size=12),
                                      fg_color=theme.INPUT_BG, text_color=theme.TEXT, border_width=0,
                                      corner_radius=10, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=20)
        self.log_box.insert("1.0", FIRST_STEPS)
        self.log_box.configure(state="disabled")
        self._apply_log_colors()
        self._show_output_dir()

    # ------------------------------------------------------------------ profile

    def _load_profile_list(self, select: str = "") -> None:
        folder = profiles_dir()
        # Shown without ".toml": the menu lists profiles, not files.
        self.profile_files = {path.stem: path for path in sorted(folder.glob("*.toml"))} if folder.exists() else {}
        self.profile_menu.configure(values=[*self.profile_files, NEW_PROFILE])
        self._build_monitored_list()
        self._refresh_export_filters()
        choice = select if select in self.profile_files else next(iter(self.profile_files), "")
        if choice:
            self.profile_menu.set(choice)
            self._choose_profile(choice)
        else:
            self._new_profile()

    def _choose_profile(self, name: str) -> None:
        if name == NEW_PROFILE:
            self._new_profile()
            return
        path = self.profile_files.get(name)
        if path is None:
            return
        try:
            profile = load_profile(path)
        except ProfileError as error:
            self._log("error", str(error))
            messagebox.showerror(TITLE, str(error), parent=self)
            return
        self.profile_path = path
        self.prefs.profile = name
        self._show_profile(profile)

    def _new_profile(self) -> None:
        self.profile_path = None
        self.profile_menu.set(NEW_PROFILE)
        self._show_profile(Profile(name="Новый профиль", regions=tuple(self.regions)))

    def _new_from_template(self, choice: str) -> None:
        """A new, not yet saved profile: words and codes from the template, regions kept from the form."""
        self.template_menu.set(NEW_MENU)
        if choice == BY_EXAMPLES:
            ExamplesDialog(self, self._collect_examples)
            return
        if choice not in self.templates:
            self._new_profile()
            return
        self.profile_path = None
        self.profile_menu.set(NEW_PROFILE)
        self._show_profile(from_template(self.templates[choice], tuple(self.regions)))
        self._log("info", f"Шаблон «{choice}»: проверьте регионы и слова, затем «Сохранить».")

    def _collect_examples(self, text: str) -> None:
        """Download the example notices and fill the form with what they share."""
        if self._busy():
            return
        numbers = parse_numbers(text)
        if not numbers:
            self._warn("Не нашёл номеров закупок: вставьте ссылки на закупки ЕИС или их 19-значные номера")
            return
        self._begin(f"Профиль по примерам: закупок {len(numbers)}")

        def work() -> None:
            with NoticeCache(default_cache_dir()) as cache:
                notices, problems = load_examples(SiteClient(), cache, numbers)
            self.events.put(("examples_done", (notices, problems, suggest(notices) if notices else None)))

        self._run_in_background(work, "examples")

    def _finish_examples(self, notices: list, problems: list[str], suggestion: Suggestion | None) -> None:
        self._set_running(False)
        self.status.configure(text="Готов к работе")
        self.progress.set(1)
        for problem in problems:
            self._log("warning", f"Не прочитана {problem}")
        if suggestion is None:
            return
        for notice in notices:
            self._log("info", f"Пример: {notice.title[:90]}")
        for note in suggestion.notes:
            self._log("info", note)
        self.profile_path = None
        self.profile_menu.set(NEW_PROFILE)
        self._show_profile(to_profile(suggestion, "Профиль по примерам", tuple(self.regions)))
        self.tabs.set(PROFILE_TAB)
        self._log("success", "Профиль собран: уберите лишние слова и коды (откуда что взялось — выше в журнале), "
                             "затем «Сохранить».")

    def _show_profile(self, profile: Profile) -> None:
        set_entry(self.name, profile.name)
        self.regions = list(profile.regions)
        self._show_regions()
        set_text(self.keywords, profile.keywords)
        set_text(self.strong, profile.strong)
        set_text(self.minus, profile.minus)
        for key, var in self.filter_vars.items():
            var.set(key in profile.filters)
        set_entry(self.okpd2, ", ".join(profile.okpd2))
        set_entry(self.ktru, ", ".join(profile.ktru))
        set_entry(self.customer_inn, ", ".join(profile.customer_inn))
        set_entry(self.exclude_inn, ", ".join(profile.exclude_customer_inn))
        set_entry(self.price_from, profile.price_from)
        set_entry(self.price_to, profile.price_to)
        set_entry(self.telegram_chat, profile.telegram_chat)
        if profile.all_stages:
            self.all_stages.select()
        else:
            self.all_stages.deselect()
        for law, var in self.law_vars.items():
            var.set(law in profile.laws)
        # A folded filter must not work unseen.
        used = (profile.customer_inn or profile.exclude_customer_inn or profile.telegram_chat or profile.all_stages
                or set(profile.laws) != set(ALL_LAWS))
        self._toggle_more(bool(used) or self.more_shown)

    def _form_profile(self) -> Profile:
        """The profile as typed in the window, checked the same way as a profile file."""
        data: dict[str, Any] = {
            "name": self.name.get().strip() or "Без названия",
            "regions": [code[:2] for code in self.regions],
            "keywords": text_lines(self.keywords),
            "strong": text_lines(self.strong),
            "minus": text_lines(self.minus),
            "filters": [key for key, var in self.filter_vars.items() if var.get()],
            "okpd2": split_list(self.okpd2.get()),
            "ktru": split_list(self.ktru.get()),
            "customer_inn": split_list(self.customer_inn.get()),
            "exclude_customer_inn": split_list(self.exclude_inn.get()),
            "all_stages": bool(self.all_stages.get()),
            "laws": [law for law, var in self.law_vars.items() if var.get()],
            "telegram_chat": self.telegram_chat.get().strip(),
        }
        for key, widget in (("price_from", self.price_from), ("price_to", self.price_to)):
            text = widget.get().replace(" ", "").strip()
            if text:
                if not text.isdigit():
                    raise ProfileError("Цена — целое число рублей")
                data[key] = int(text)
        return parse_profile(data, self.profile_path or Path("профиль.toml"))

    def _save_profile(self) -> None:
        try:
            profile = self._form_profile()
        except ProfileError as error:
            self._warn(without_path(error))
            return
        path = self.profile_path
        if path is None:
            profiles_dir().mkdir(parents=True, exist_ok=True)
            chosen = filedialog.asksaveasfilename(
                parent=self, initialdir=profiles_dir(), initialfile=f"{safe_file_name(profile.name)}.toml",
                defaultextension=".toml", filetypes=[("Профиль", "*.toml")], title="Сохранить профиль")
            if not chosen:
                return
            path = Path(chosen)
        save_profile(profile, path)
        self._log("success", f"Профиль сохранён: {path.name}")
        self._load_profile_list(path.stem)

    def _pick_regions(self) -> None:
        def done(selected: list[str]) -> None:
            self.regions = selected
            self._show_regions()

        RegionPicker(self, self.regions, done)

    def _show_regions(self) -> None:
        _show_region_names(self.regions_label, self.regions)

    # ------------------------------------------------------------------ search

    def _refresh_period(self) -> None:
        if self.period.get() == CUSTOM:
            self.dates.pack(fill="x", pady=(6, 0), before=self.start_button)
        else:
            self.dates.pack_forget()

    def _period(self) -> tuple[date, date]:
        return period_dates(self.period.get(), date.today(), self.date_from.get(), self.date_to.get())

    def _start(self) -> None:
        if self._busy():
            return
        try:
            profile = self._form_profile()
            start, end = self._period()
        except (ProfileError, ValueError) as error:
            self._warn(without_path(error))
            return
        out_dir = self._output_dir()
        self._begin(f"Поиск по профилю «{profile.name}» за {period_text(start, end)}")

        def work() -> None:
            client = SiteClient()
            regions_done = [0]

            def progress(number: int, total: int, _hit: object, _status: str) -> None:
                fraction = (regions_done[0] + number / max(total, 1)) / len(profile.regions)
                self.events.put(("progress", (fraction, f"Извещения: {number} из {total}")))

            def region_done(region: str, report: FetchReport) -> None:
                regions_done[0] += 1
                self.events.put(("log", ("info", _region_line(region, report))))

            with NoticeCache(default_cache_dir()) as cache:
                result = run_profiles(client, cache, [profile], start, end, progress=progress,
                                      region_done=region_done, cancel=self.cancel_event.is_set,
                                      ids=Okpd2Ids(client, okpd2_ids_path()))
            generated_at = datetime.now().replace(microsecond=0)
            path = None
            try:
                path = export_profile(out_dir / build_file_name(profile, generated_at), result.profiles[0], start,
                                      end, generated_at)
            except OSError as error:  # e.g. the folder is not writable: the results are still shown
                self.events.put(("log", ("error", f"Отчёт не сохранён: {error}")))
            self.events.put(("search_done", (result, path, start, end, client.stats.requests)))

        self._run_in_background(work, "search")

    def _show_results(self, result: RunResult, start: date, end: date) -> None:
        for child in self.results_list.winfo_children():
            child.destroy()
        profile_result = result.profiles[0]
        matches = profile_result.matches
        self.results_header.configure(
            text=f"«{profile_result.profile.name}» за {period_text(start, end)}: подошло {len(matches)} "
                 f"из {profile_result.checked}")
        for found in matches[:MAX_RESULT_CARDS]:
            self._result_card(found).pack(fill="x", pady=(0, 10), padx=(0, 6))
        if len(matches) > MAX_RESULT_CARDS:
            label(self.results_list, f"…и ещё {len(matches) - MAX_RESULT_CARDS} — в отчёте Excel").pack(anchor="w")

    def _result_card(self, found: Found) -> ctk.CTkFrame:
        notice = found.notice
        card = ctk.CTkFrame(self.results_list, fg_color=theme.CARD_BG, corner_radius=12, border_width=1,
                            border_color=theme.CARD_BORDER)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 0))
        deadline = (f"заявки до {moment(notice.applications_end)} ({moscow_offset(notice.applications_end)})"
                    if notice.applications_end else no_deadline(notice.placing_way or found.hit.placing_way))
        ctk.CTkLabel(top, text=f"{money(notice.max_price, notice.currency)} · {deadline}", font=theme.font(13, "bold"),
                     text_color=theme.TEXT, anchor="w").pack(side="left")
        stage = found.stage(datetime.now().astimezone())
        if stage:
            ctk.CTkLabel(top, text=stage, font=theme.font(12, "bold"), text_color=theme.stage_color(stage)).pack(
                side="right")
        title = ctk.CTkLabel(card, text=notice.title, font=theme.font(14, "bold"), text_color=theme.ACCENT,
                             anchor="w", justify="left", wraplength=500, cursor="hand2")
        title.pack(fill="x", padx=14, pady=(4, 0))
        title.bind("<Button-1>", lambda _: webbrowser.open(notice.url))
        customers = notice.customers
        customer = (f"{customers[0].customer.name}, ИНН {customers[0].customer.inn or 'не указан'}"
                    if len(customers) == 1
                    else f"Совместная закупка, заказчиков: {len(customers)}")
        way = with_law(notice.law, notice.placing_way)
        meta = ctk.CTkLabel(card, text=f"№ {notice.reg_number} · {way} · {customer}",
                            font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                            wraplength=500)
        meta.pack(fill="x", padx=14)
        why = ctk.CTkLabel(card, text="Почему: " + "; ".join(found.verdict.reasons), font=theme.font(12),
                           text_color=theme.TEXT, anchor="w", justify="left", wraplength=500)
        why.pack(fill="x", padx=14, pady=(4, 10))
        wrap_to_width(card, [title, meta, why])
        return card

    # ------------------------------------------------------------------ export by budget

    def _pick_export_regions(self, then: Any = None) -> None:
        def done(selected: list[str]) -> None:
            self.export_regions = selected
            self._show_export_regions()
            if selected and then:
                self.after(100, then)

        RegionPicker(self, self.export_regions, done)

    def _show_export_regions(self) -> None:
        _show_region_names(self.export_regions_label, self.export_regions)

    def _refresh_export_filters(self) -> None:
        """Every open notice, or the ones of a niche: a template, ready to use, or a saved profile."""
        values = [ALL_NOTICES, *self.templates, *(MY_PROFILE + name for name in self.profile_files)]
        self.export_filter.configure(values=values)
        wanted = self.export_filter.get() if self._filters_loaded else _menu_choice(self.prefs.export_filter)
        self._filters_loaded = True  # from now on the menu keeps what the user picked
        self.export_filter.set(wanted if wanted in values else ALL_NOTICES)

    def _export_profile(self) -> Profile | None:
        choice = self.export_filter.get()
        if choice in self.templates:
            return from_template(self.templates[choice])
        if choice.startswith(MY_PROFILE) and choice.removeprefix(MY_PROFILE) in self.profile_files:
            return load_profile(self.profile_files[choice.removeprefix(MY_PROFILE)])
        return None

    def _export_query(self) -> ExportQuery:
        if not self.export_regions:
            raise ValueError("Выберите хотя бы один регион")
        price_from, price_to = rubles(self.export_price_from.get()), rubles(self.export_price_to.get())
        if price_from is not None and price_to is not None and price_from > price_to:
            raise ValueError("Цена «от» больше цены «до»")
        laws = tuple(law for law, var in self.export_law_vars.items() if var.get())
        if not laws:
            raise ValueError("Отметьте хотя бы один тип торгов: 44-ФЗ, 223-ФЗ или ПП РФ 615")
        return ExportQuery(tuple(self.export_regions), price_from, price_to, self.export_words.get().strip(),
                           bool(self.export_details.get()), self._export_profile(), laws)

    def _start_export(self) -> None:
        if self._busy():
            return
        if not self.export_regions:  # the one thing a first search needs: ask for it and go on
            self._pick_export_regions(then=self._start_export)
            return
        try:
            query = self._export_query()
        except ValueError as error:  # ProfileError too
            self._warn(without_path(error))
            return
        out_dir = self._output_dir()
        self._begin(f"Открытые закупки: {_export_title(query)}")

        def work() -> None:
            client = SiteClient()
            # With details the downloads take the second half of the progress bar.
            share = 0.5 if query.detailed else 1.0

            def listing(region: str, collected: int, total: int) -> None:
                done = query.regions.index(region) + collected / max(total, 1)
                self.events.put(("progress", (done / len(query.regions) * share,
                                              f"{REGIONS[region]}: {collected} из {total}")))

            def progress(number: int, total: int, _hit: object, _status: str) -> None:
                self.events.put(("progress", (share + (1 - share) * number / max(total, 1),
                                              f"Извещения: {number} из {total}")))

            with NoticeCache(default_cache_dir()) as cache:
                result = export_open(client, cache, query, datetime.now().astimezone(), listing=listing,
                                     progress=progress, cancel=self.cancel_event.is_set,
                                     ids=Okpd2Ids(client, okpd2_ids_path()))
            generated_at = datetime.now().replace(microsecond=0)
            path = None
            try:
                path = export_open_notices(out_dir / export_file_name(result, generated_at), result, generated_at)
            except OSError as error:
                self.events.put(("log", ("error", f"Отчёт не сохранён: {error}")))
            self.events.put(("export_done", (result, path, client.stats.requests)))

        self._run_in_background(work, "export")

    def _finish_export(self, result: ExportResult, path: Path | None, requests: int) -> None:
        self._set_running(False)
        self._show_export(result)
        stopped = result.error == STOPPED
        prefix = "Остановлено" if stopped else "Готово"
        for code, count in result.regions.items():
            cut = " — сайт отдал не всё (больше 5 000), сузьте цену" if count.truncated else ""
            self._log("warning" if cut else "info",
                      f"{REGIONS.get(code, code)}: с этапом «Подача заявок» {count.listed}, открыто {count.open}{cut}")
        if result.error and not stopped:
            self._log("warning", f"Выгрузка остановлена: {result.error}. В отчёте — то, что успело собраться.")
        if result.skipped:
            opened = len(result.notices) + result.unmatched
            self._log("info", f"Скачивались только закупки с кодами ниши или её словами в названии: "
                              f"{opened - result.skipped} из {opened}")
        if result.downloads and result.downloads.failed:
            self._log("warning", f"Не скачалось извещений: {len(result.downloads.failed)} — в отчёте без подробностей")
        for number, reason in result.broken:
            self._log("warning", f"Не удалось разобрать {number}: {reason}")
        self._log("success", f"{prefix}: открыто {len(result.notices)}, срок подачи уже прошёл у {result.closed} "
                             f"из {result.listed}; запросов к сайту {requests}")
        self.result_title.configure(text=f"{prefix}: открытых закупок {len(result.notices)}")
        self._show_report_card(path, stopped)
        if result.notices:
            self.tabs.set(RESULTS_TAB)

    def _show_export(self, result: ExportResult) -> None:
        for child in self.results_list.winfo_children():
            child.destroy()
        niche = f", подходят профилю — {len(result.notices)} из {len(result.notices) + result.unmatched}" \
            if result.query.profile else ""
        self.results_header.configure(
            text=f"Открытые закупки, {_export_title(result.query)}: {len(result.notices)} "
                 f"(срок подачи прошёл у {result.closed} из {result.listed}{niche})")
        several = len(result.query.regions) > 1
        for item in result.notices[:MAX_RESULT_CARDS]:
            self._open_card(item, result.today, several).pack(fill="x", pady=(0, 10), padx=(0, 6))
        if len(result.notices) > MAX_RESULT_CARDS:
            label(self.results_list, f"…и ещё {len(result.notices) - MAX_RESULT_CARDS} — в отчёте Excel").pack(
                anchor="w")

    def _open_card(self, item: OpenNotice, today: date, show_regions: bool) -> ctk.CTkFrame:
        hit, notice = item.hit, item.notice
        card = ctk.CTkFrame(self.results_list, fg_color=theme.CARD_BG, corner_radius=12, border_width=1,
                            border_color=theme.CARD_BORDER)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 0))
        if notice and notice.applications_end:
            deadline = f"заявки до {moment(notice.applications_end)} ({moscow_offset(notice.applications_end)})"
        else:
            deadline = f"заявки до {hit.deadline:%d.%m.%Y}" if hit.deadline else no_deadline(hit.placing_way)
        price = money(notice.max_price, notice.currency) if notice else money(hit.price)
        ctk.CTkLabel(top, text=f"{price} · {deadline}", font=theme.font(13, "bold"), text_color=theme.TEXT,
                     anchor="w").pack(side="left")
        days = item.days_left(today)
        if days is not None:
            ctk.CTkLabel(top, text=days_left(days), font=theme.font(12, "bold"),
                         text_color=theme.WARNING if days <= 2 else theme.SUCCESS).pack(side="right")
        title = ctk.CTkLabel(card, text=notice.title if notice else hit.title, font=theme.font(14, "bold"),
                             text_color=theme.ACCENT, anchor="w", justify="left", wraplength=500, cursor="hand2")
        title.pack(fill="x", padx=14, pady=(4, 0))
        title.bind("<Button-1>", lambda _: webbrowser.open(hit.url))
        if notice and len(notice.customers) == 1:
            customer = f"{notice.customers[0].customer.name}, ИНН {notice.customers[0].customer.inn}"
        elif notice:
            customer = f"Совместная закупка, заказчиков: {len(notice.customers)}"
        else:
            customer = hit.organization
        parts = [f"№ {hit.reg_number}", with_law(hit.law, hit.placing_way), customer]
        if show_regions:
            parts.append(", ".join(REGIONS.get(code, code) for code in item.regions))
        meta = ctk.CTkLabel(card, text=" · ".join(part for part in parts if part), font=theme.font(12),
                            text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=500)
        meta.pack(fill="x", padx=14, pady=(0, 4 if item.verdict else 10))
        labels = [title, meta]
        if item.verdict:
            why = ctk.CTkLabel(card, text="Почему: " + ", ".join(item.verdict.matched_by), font=theme.font(12),
                               text_color=theme.TEXT, anchor="w", justify="left", wraplength=500)
            why.pack(fill="x", padx=14, pady=(0, 10))
            labels.append(why)
        wrap_to_width(card, labels)
        return card

    def _finish_search(self, result: RunResult, path: Path | None, start: date, end: date, requests: int) -> None:
        self._set_running(False)
        self._show_results(result, start, end)
        profile_result = result.profiles[0]
        stopped = result.error == STOPPED
        prefix = "Остановлено" if stopped else "Готово"
        self.result_title.configure(text=f"{prefix}: подошло {len(profile_result.matches)} из {profile_result.checked}")
        if result.error and not stopped:
            self._log("warning", f"Скачивание остановлено: {result.error}. Отобрано из того, что успело скачаться.")
        for number, reason in result.broken:
            self._log("warning", f"Не удалось разобрать {number}: {reason}")
        self._log("success", f"{prefix}: подошло {len(profile_result.matches)} из {profile_result.checked}, "
                             f"запросов к сайту {requests}")
        self._show_report_card(path, stopped)
        if profile_result.matches:
            self.tabs.set(RESULTS_TAB)

    def _show_report_card(self, path: Path | None, stopped: bool) -> None:
        self.status.configure(text="Готов к работе")
        self.progress.set(1)
        if path:
            self.last_report = path
            self.result_path.configure(text=path.name)
            self.result_card.pack(side="bottom", fill="x", padx=20, pady=(10, 0), before=self.log_box)
            if self.open_report_switch.get() and not stopped:
                self._open_report()

    # ------------------------------------------------------------------ monitoring

    def _build_monitored_list(self) -> None:
        for child in self.monitored_frame.winfo_children():
            child.destroy()
        self.monitored_vars = {}
        if not self.profile_files:
            label(self.monitored_frame, "Сохраните хотя бы один профиль").grid(row=0, column=0, sticky="w")
        label(self.monitored_frame, "Профили:").grid(row=0, column=0, sticky="w", padx=(0, 10))
        for index, name in enumerate(self.profile_files):
            var = ctk.BooleanVar(value=name in self.prefs.monitored)
            ctk.CTkCheckBox(self.monitored_frame, text=name, variable=var, font=theme.font(13),
                            text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                            checkbox_width=18, checkbox_height=18, corner_radius=5,
                            command=self._remember_monitored).grid(row=index // 2, column=1 + index % 2, sticky="w",
                                                                   padx=(0, 14), pady=2)
            self.monitored_vars[name] = var

    def _remember_monitored(self) -> None:
        self.prefs.monitored = [name for name, var in self.monitored_vars.items() if var.get()]

    def _monitored_paths(self) -> list[Path]:
        return [self.profile_files[name] for name in self.prefs.monitored if name in self.profile_files]

    def _refresh_monitoring(self) -> None:
        def work() -> None:
            settings = load_telegram_settings()
            if not settings.token:
                bot_text = "Бот не подключён: вставьте токен от @BotFather и нажмите «Подключить»."
            else:
                try:
                    bot_text = f"Бот @{TelegramBot(settings.token).username()}"
                except TelegramError as error:
                    bot_text = str(error)
                bot_text += f" · чат по умолчанию: {settings.chat or 'не задан'}"
            try:
                schedule_text = _schedule_text(scheduler.status())
                schedule_text += "\n" + bot_line(scheduler.bot_status()) + _feedback_line()
            except SchedulerError as error:
                schedule_text = str(error)
            self.events.put(("monitoring_status", (bot_text, schedule_text)))

        threading.Thread(target=work, name="monitoring-status", daemon=True).start()

    def _connect_bot(self) -> None:
        """Check the token with Telegram and keep it in .env; it never reaches a log."""
        token = self.token_entry.get().strip()
        if not token:
            self._warn("Вставьте токен, который прислал @BotFather: он выглядит как 123456789:AAE…")
            return
        self._protect(token)

        def work() -> None:
            try:
                name = TelegramBot(token).username()
            except TelegramError as error:
                self.events.put(("log", ("error", f"Бот не подключён: {_connect_hint(error)}")))
                return
            save_env_value(primary_env_file(), TOKEN_KEY, token)
            self.events.put(("bot_connected", name))

        threading.Thread(target=work, name="telegram-connect", daemon=True).start()

    def _bot_connected(self, name: str) -> None:
        set_entry(self.token_entry, "")
        chat = load_telegram_settings().chat
        next_step = "" if chat else " Теперь напишите ему любое сообщение и нажмите «Найти мой чат»."
        self._log("success", f"Бот @{name} подключён.{next_step}")
        self._refresh_monitoring()

    def _protect(self, token: str) -> None:
        """A token typed into the window is cut out of every log, as the one from .env is."""
        for handler in [self.log_handler, *logging.getLogger().handlers]:
            for item in handler.filters:
                if isinstance(item, SecretFilter) and token not in item.secrets:
                    item.secrets.append(token)

    def _find_chat(self) -> None:
        def work() -> None:
            settings = load_telegram_settings()
            bot = TelegramBot(settings.token)
            chats = bot.chats()
            if not chats:
                self.events.put(("log", ("warning", "Боту пока никто не писал: напишите ему сообщение и повторите.")))
            elif len(chats) == 1 and not settings.chat:
                save_env_value(primary_env_file(), CHAT_KEY, str(chats[0][0]))
                self.events.put(("log", ("success", f"Чат {chats[0][0]} ({chats[0][2]}) стал чатом по умолчанию.")))
            else:
                for chat_id, kind, name in chats:
                    self.events.put(("log", ("info", f"Чат {chat_id} · {kind} · {name}")))
            self.events.put(("refresh_monitoring", None))

        self._telegram_task(work)

    def _test_message(self) -> None:
        def work() -> None:
            settings = load_telegram_settings()
            if not settings.chat:
                self.events.put(("log", ("warning", "Не задан чат по умолчанию: нажмите «Найти мой чат».")))
                return
            TelegramBot(settings.token).send_message(settings.chat, TEST_MESSAGE)
            self.events.put(("log", ("success", f"Тестовое сообщение отправлено в чат {settings.chat}.")))

        self._telegram_task(work)

    def _telegram_task(self, work: Any) -> None:
        def guarded() -> None:
            try:
                work()
            except TelegramError as error:
                self.events.put(("log", ("error", str(error))))

        if not load_telegram_settings().token:
            self._warn("Сначала подключите бота: вставьте токен от @BotFather и нажмите «Подключить».")
            return
        threading.Thread(target=guarded, name="telegram", daemon=True).start()

    def _open_env(self) -> None:
        path = primary_env_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ENV_TEMPLATE, encoding="utf-8")
        self._open_path(path)

    def _schedule_on(self) -> None:
        paths = self._monitored_paths()
        if not paths:
            self._warn("Отметьте профили для проверок по расписанию.")
            return
        try:
            start, end = parse_time(self.schedule_from.get().strip()), parse_time(self.schedule_to.get().strip())
        except Exception:
            self._warn("Время — в виде ЧЧ:ММ, например 08:00.")
            return
        every = int(self.schedule_every.get())
        self.prefs.schedule_from, self.prefs.schedule_to = f"{start:%H:%M}", f"{end:%H:%M}"
        self.prefs.schedule_every = every

        def work() -> None:
            scheduler.create(paths, start, end, every, with_bot=bool(load_telegram_settings().token))
            self.events.put(("log", ("success", f"Проверки включены: с {start:%H:%M} до {end:%H:%M} по Москве, "
                                                f"каждые {every} мин.")))

        self._scheduler_task(work)

    def _schedule_off(self) -> None:
        def work() -> None:
            scheduler.set_enabled(False)
            self.events.put(("log", ("success", "Проверки по расписанию выключены.")))

        self._scheduler_task(work)

    def _scheduler_task(self, work: Any) -> None:
        def guarded() -> None:
            try:
                work()
            except SchedulerError as error:
                self.events.put(("log", ("error", str(error))))
            self.events.put(("refresh_monitoring", None))

        threading.Thread(target=guarded, name="scheduler", daemon=True).start()

    def _check_now(self) -> None:
        if self._busy():
            return
        paths = self._monitored_paths()
        if not paths:
            self._warn("Отметьте профили для проверки.")
            return
        try:
            profiles = [load_profile(path) for path in paths]
        except ProfileError as error:
            self._warn(str(error))
            return
        out_dir = self._output_dir()
        self._begin("Проверка новых закупок: " + ", ".join(profile.name for profile in profiles))

        def work() -> None:
            settings = load_telegram_settings()
            bot = None
            if settings.token:
                try:
                    bot = TelegramBot(settings.token)
                except TelegramError as error:
                    self.events.put(("log", ("error", str(error))))
            now = datetime.now().astimezone()
            with (
                NoticeCache(default_cache_dir()) as cache,
                MonitorState(app_data_dir() / "monitor.sqlite3") as state,
            ):
                client = SiteClient()
                result = monitor(client, cache, state, profiles, today=now.astimezone(MOSCOW).date(), now=now,
                                 bot=bot, default_chat=settings.chat, out_dir=out_dir,
                                 region_done=lambda region, report: self.events.put(
                                     ("log", ("info", _region_line(region, report)))),
                                 cancel=self.cancel_event.is_set, ids=Okpd2Ids(client, okpd2_ids_path()))
            self.events.put(("monitor_done", result))

        self._run_in_background(work, "monitor")

    def _finish_monitor(self, result: MonitorResult) -> None:
        self._set_running(False)
        self.status.configure(text="Готов к работе")
        self.progress.set(1)
        for outcome in result.outcomes:
            text = (f"«{outcome.profile.name}» за {period_text(result.start, result.end)}: подошло {outcome.matches}, "
                    f"новых {outcome.new}, отправлено в Telegram {outcome.sent}")
            self._log("warning" if outcome.error else "success", text)
            if outcome.error:
                self._log("error", f"Telegram: {outcome.error}")
            if outcome.report:
                self.last_report = outcome.report

    def _open_monitor_log(self) -> None:
        path = app_data_dir() / "logs" / "monitor.log"
        if path.exists():
            self._open_path(path)
        else:
            self._warn("Журнал появится после первой проверки.")

    # ------------------------------------------------------------------ running

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _begin(self, title: str) -> None:
        self.result_card.pack_forget()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress.set(0)
        self.cancel_event.clear()
        self._set_running(True)
        self.status.configure(text="Ищу закупки на сайте ЕИС…")
        self._log("info", title)

    def _run_in_background(self, work: Any, name: str) -> None:
        def guarded() -> None:
            try:
                work()
            except Exception as error:
                log.exception("%s failed", name)
                self.events.put(("log", ("error", f"Ошибка: {error}")))
                self.events.put(("failed", None))

        self.worker = threading.Thread(target=guarded, name=name, daemon=True)
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state, text="Идёт поиск…" if running else "Найти по профилю")
        self.export_button.configure(state=state, text="Идёт поиск…" if running else "Найти закупки")
        if running:
            self.stop_button.configure(state="normal", text="Остановить")
            self.stop_button.pack(fill="x", padx=20, before=self.progress)
        else:
            self.stop_button.pack_forget()

    def _stop(self) -> None:
        self.cancel_event.set()
        self.stop_button.configure(state="disabled", text="Останавливаю…")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(*payload)
                elif kind == "progress":
                    fraction, text = payload
                    self.progress.set(min(fraction, 1))
                    self.status.configure(text=text)
                elif kind == "search_done":
                    self._finish_search(*payload)
                elif kind == "export_done":
                    self._finish_export(*payload)
                elif kind == "examples_done":
                    self._finish_examples(*payload)
                elif kind == "bot_connected":
                    self._bot_connected(payload)
                elif kind == "monitor_done":
                    self._finish_monitor(payload)
                elif kind == "failed":
                    self._set_running(False)
                    self.status.configure(text="Ошибка — подробности в журнале")
                elif kind == "monitoring_status":
                    bot_text, schedule_text = payload
                    self.bot_status.configure(text=bot_text)
                    self.schedule_status.configure(text=schedule_text)
                elif kind == "refresh_monitoring":
                    self._refresh_monitoring()
                if self._closing and not self._busy():
                    self.destroy()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _log(self, level: str, message: str) -> None:
        self.log_box.configure(state="normal")
        if self.log_box.get("1.0", "end").strip() == FIRST_STEPS.strip():  # the first entry replaces the hint
            self.log_box.delete("1.0", "end")
        self.log_box.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.log_box.insert("end", message + "\n", level if level in LOG_COLORS else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, message)

    def _warn(self, message: str) -> None:
        self._log("warning", message)
        messagebox.showwarning(TITLE, message, parent=self)

    # ------------------------------------------------------------------ files and appearance

    def _output_dir(self) -> Path:
        return Path(self.prefs.output_dir) if self.prefs.output_dir else default_output_dir()

    def _show_output_dir(self) -> None:
        self.output_label.configure(text=f"Отчёты: {short_path(self._output_dir())}")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=str(self._output_dir()))
        if path:
            self.prefs.output_dir = str(Path(path))
            self._show_output_dir()

    def _open_report(self) -> None:
        if self.last_report and self.last_report.exists():
            os.startfile(self.last_report)

    def _show_report(self) -> None:
        if self.last_report:
            subprocess.Popen(["explorer", "/select,", str(self.last_report)])

    def _open_path(self, path: Path) -> None:
        if not path.suffix:  # a folder that may not exist yet
            path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _set_appearance(self, value: str) -> None:
        self.prefs.appearance = value
        ctk.set_appearance_mode({"Светлая": "light", "Тёмная": "dark"}.get(value, "system"))
        if hasattr(self, "log_box"):
            self._apply_log_colors()

    def _apply_log_colors(self) -> None:
        index = 1 if ctk.get_appearance_mode() == "Dark" else 0
        for level, colors in LOG_COLORS.items():
            self.log_box.tag_config(level, foreground=colors[index])
        self.log_box.tag_config("time", foreground=theme.TEXT_MUTED[index])

    def _on_close(self) -> None:
        self._save_preferences()
        if self._busy():
            if not messagebox.askyesno(TITLE, "Поиск ещё идёт. Остановить и выйти?", parent=self):
                return
            self._closing = True
            self.cancel_event.set()
            self.status.configure(text="Завершаю работу…")
            self.after(15_000, self.destroy)  # do not hang on a slow answer of the site
            return
        self.destroy()

    def _save_preferences(self) -> None:
        self.prefs.period = self.period.get()
        self.prefs.date_from = self.date_from.get().strip()
        self.prefs.date_to = self.date_to.get().strip()
        self.prefs.open_report = bool(self.open_report_switch.get())
        self.prefs.export_regions = list(self.export_regions)
        self.prefs.export_price_from = self.export_price_from.get().strip()
        self.prefs.export_price_to = self.export_price_to.get().strip()
        self.prefs.export_words = self.export_words.get().strip()
        self.prefs.export_details = bool(self.export_details.get())
        self.prefs.export_laws = [law for law, var in self.export_law_vars.items() if var.get()]
        self.prefs.export_filter = self.export_filter.get()
        try:
            self.prefs.save()
        except OSError:
            log.debug("Could not save window settings", exc_info=True)


def _show_region_names(widget: ctk.CTkLabel, codes: list[str]) -> None:
    names = [REGIONS[code] for code in codes if code in REGIONS]
    widget.configure(text=", ".join(names) or "не выбраны", text_color=theme.TEXT if names else theme.DANGER)


def _brand_image(light: str, dark: str, size: tuple[int, int]) -> ctk.CTkImage | None:
    """An image of the author's brand kit (assets/brand) for the light and the dark theme."""
    try:
        from PIL import Image
    except ImportError:  # sources run without Pillow: the window works, only without the pictures
        return None
    paths = [theme.resource_path(f"assets/brand/{name}") for name in (light, dark)]
    if not all(path.exists() for path in paths):
        return None
    return ctk.CTkImage(light_image=Image.open(paths[0]), dark_image=Image.open(paths[1]), size=size)


def _connect_hint(error: TelegramError) -> str:
    """What Telegram's refusal means for someone who pasted a token into the window, not into .env."""
    text = str(error)
    if "Unauthorized" in text:
        return "Telegram не принял токен. Скопируйте его из сообщения @BotFather целиком и вставьте ещё раз."
    if "не похож на токен" in text:
        return "это не похоже на токен бота: он выглядит как 123456789:AAE… — скопируйте его из @BotFather целиком."
    return text


def _menu_choice(remembered: str) -> str:
    """The niche menu item remembered by an older version: "Шаблон: Мебель" is "Мебель" now."""
    if remembered.startswith(TEMPLATE_FILTER):
        return remembered.removeprefix(TEMPLATE_FILTER)
    if remembered.startswith(PROFILE_FILTER):
        return MY_PROFILE + remembered.removeprefix(PROFILE_FILTER)
    return remembered


def _export_title(query: ExportQuery) -> str:
    regions = [REGIONS.get(code, code) for code in query.regions]
    parts = [", ".join(regions) if len(regions) <= 2 else regions_count(len(regions))]
    parts.append(price_range(query.price_from, query.price_to, short=True) or "любая цена")
    if query.profile is not None:
        parts.append(f"«{query.profile.name}»")
    if query.text:
        parts.append(f"«{query.text}»")
    return " · ".join(parts)


def _region_line(region: str, report: FetchReport) -> str:
    return (f"{REGIONS.get(region, region)}: найдено {report.found}{chosen(report)}, скачано {report.downloaded}, "
            f"из кэша {report.cached}{problems(report)}")


def _feedback_line() -> str:
    """The marks of the past week, the precision of the profiles in a line."""
    with MonitorState(state_path()) as state:
        rows = state.stats(datetime.now().astimezone() - timedelta(days=7))
    sent = sum(row.sent for row in rows)
    if not sent:
        return ""
    taken, skipped = sum(row.taken for row in rows), sum(row.skipped for row in rows)
    return f"\nЗа 7 дней прислано {sent}: ✅ беру {taken} · ❌ не моё {skipped} · без отметки {sent - taken - skipped}"


def _schedule_text(status: scheduler.TaskStatus) -> str:
    if not status.exists:
        return "Расписание не настроено: отметьте профили и нажмите «Включить»."
    text = "Проверки по расписанию включены." if status.enabled else "Проверки по расписанию выключены."
    if status.enabled and status.next_run:
        text += f" Следующая: {status.next_run} (время компьютера)."
    if status.last_run:
        text += f" Последняя: {status.last_run} — {status.last_result}."
    return text


def setup_logging() -> None:
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "window.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretFilter(load_telegram_settings().token))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for noisy in ("urllib3", "requests", "pymorphy3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    app = App()
    app.mainloop()
