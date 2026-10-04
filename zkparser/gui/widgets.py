"""Reusable widgets of the main window."""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from ..regions import REGIONS
from . import theme


def wrap_to_width(container: ctk.CTkBaseClass, labels: list[ctk.CTkLabel], margin: int = 40) -> None:
    """Re-wrap labels whenever the container changes width: Tk wraps to a fixed length, not to the space it has."""

    def resize(event: object) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(container)
        width = max(int(event.width / scaling) - margin, 200)
        for item in labels:
            item.configure(wraplength=width)

    container.bind("<Configure>", resize, add="+")


class SectionCard(ctk.CTkFrame):
    """A rounded card with a title; put content into ``self.body``."""

    def __init__(self, master: ctk.CTkBaseClass, title: str, hint: str = "") -> None:
        super().__init__(master, fg_color=theme.CARD_BG, corner_radius=14, border_width=1,
                         border_color=theme.CARD_BORDER)
        ctk.CTkLabel(self, text=title, font=theme.font(15, "bold"), text_color=theme.TEXT, anchor="w").pack(
            fill="x", padx=18, pady=(12, 0))
        if hint:
            note = ctk.CTkLabel(self, text=hint, font=theme.font(12), text_color=theme.TEXT_MUTED, justify="left",
                                anchor="w", wraplength=500)
            note.pack(fill="x", padx=18, pady=(2, 0))
            wrap_to_width(self, [note])
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=18, pady=(8, 14))


def label(master: ctk.CTkBaseClass, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w")


def entry(master: ctk.CTkBaseClass, placeholder: str = "", width: int = 140) -> ctk.CTkEntry:
    return ctk.CTkEntry(master, width=width, height=34, font=theme.font(13), border_width=1,
                        fg_color=theme.INPUT_BG, border_color=theme.CARD_BORDER, text_color=theme.TEXT,
                        placeholder_text=placeholder)


def textbox(master: ctk.CTkBaseClass, height: int = 120) -> ctk.CTkTextbox:
    return ctk.CTkTextbox(master, height=height, font=theme.font(13), border_width=1, fg_color=theme.INPUT_BG,
                          border_color=theme.CARD_BORDER, text_color=theme.TEXT, wrap="none")


def set_entry(widget: ctk.CTkEntry, value: object) -> None:
    text = "" if value in (None, "") else str(value)
    if widget.get() == text:  # nothing to change: an empty field keeps its placeholder
        return
    widget.delete(0, "end")
    if text:
        widget.insert(0, text)
    else:
        widget._activate_placeholder()  # CTk brings the placeholder back only when the field loses focus


def set_text(widget: ctk.CTkTextbox, lines: list[str] | tuple[str, ...]) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", "\n".join(lines))


def text_lines(widget: ctk.CTkTextbox) -> list[str]:
    return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]


def accent_button(master: ctk.CTkBaseClass, text: str, command: Callable[[], None], width: int = 140,
                  **kwargs: object) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, width=width, height=34, font=theme.font(13, "bold"),
                         fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, corner_radius=8, **kwargs)


def neutral_button(master: ctk.CTkBaseClass, text: str, command: Callable[[], None], width: int = 120,
                   **kwargs: object) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, width=width, height=34, font=theme.font(13),
                         fg_color=theme.NEUTRAL_BUTTON, hover_color=theme.NEUTRAL_BUTTON_HOVER,
                         text_color=theme.TEXT, corner_radius=8, **kwargs)


def segmented(master: ctk.CTkBaseClass, values: list[str], command: Callable[[str], None]) -> ctk.CTkSegmentedButton:
    return ctk.CTkSegmentedButton(
        master, values=values, command=command, font=theme.font(12), height=32,
        selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
        unselected_color=theme.NEUTRAL_BUTTON, unselected_hover_color=theme.NEUTRAL_BUTTON_HOVER,
        text_color=theme.TEXT, fg_color=theme.NEUTRAL_BUTTON)


class RegionPicker(ctk.CTkToplevel):
    """A dialog with a checkbox per region and a search box."""

    def __init__(self, master: ctk.CTk, selected: list[str], on_done: Callable[[list[str]], None]) -> None:
        super().__init__(master, fg_color=theme.APP_BG)
        self.title("Регионы заказчиков")
        self.geometry("440x600")
        self.transient(master)
        self.after(50, self.grab_set)  # grab only after the window is shown, otherwise Tk refuses it
        self._on_done = on_done
        self._items: dict[str, tuple[ctk.BooleanVar, ctk.CTkCheckBox]] = {}

        self.search = entry(self, "Найти регион…")
        self.search.pack(fill="x", padx=16, pady=(16, 8))
        self.search.bind("<KeyRelease>", lambda _: self._filter())
        self.list = ctk.CTkScrollableFrame(self, fg_color=theme.CARD_BG, corner_radius=10, border_width=1,
                                           border_color=theme.CARD_BORDER)
        self.list.pack(fill="both", expand=True, padx=16)
        for code, name in sorted(REGIONS.items(), key=lambda item: item[1]):
            var = ctk.BooleanVar(value=code in selected)
            box = ctk.CTkCheckBox(self.list, text=f"{name}  ·  {code[:2]}", variable=var, font=theme.font(13),
                                  text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                                  checkbox_width=18, checkbox_height=18, corner_radius=5)
            box.pack(anchor="w", padx=8, pady=3)
            self._items[code] = (var, box)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=16)
        accent_button(buttons, "Готово", self._done, width=110).pack(side="right")
        neutral_button(buttons, "Снять все", self._clear, width=110).pack(side="left")

    def _filter(self) -> None:
        text = self.search.get().strip().casefold()
        for _, box in self._items.values():
            box.pack_forget()
        for code, (var, box) in self._items.items():
            if not text or text in REGIONS[code].casefold() or text == code[:2] or var.get():
                box.pack(anchor="w", padx=8, pady=3)

    def _clear(self) -> None:
        for var, _ in self._items.values():
            var.set(False)

    def _done(self) -> None:
        self._on_done([code for code, (var, _) in self._items.items() if var.get()])
        self.grab_release()
        self.destroy()
