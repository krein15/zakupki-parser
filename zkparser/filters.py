"""Junk filters: ready sets of minus words a profile switches on by name instead of writing them out.

Minus words are where a newcomer gets lost: which ones, how to write them, will they cut too much. But the junk of
most niches is the same few kinds — licences resold as "software", legal reference systems, repairs, construction,
road maintenance with thousands of positions. A filter is one such kind: its title is what the window shows, its
words work as the profile's own minus words (strong words outweigh them as well).

The sets come from the dictionaries checked on the Ural month (October 2026): the licence and reference system words
removed 52 of the 53 resold licences a developer's dictionary found and not one development.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Filter:
    title: str
    minus: tuple[str, ...]


FILTERS: dict[str, Filter] = {
    "licences": Filter("Перепродажа лицензий", (
        "лицензи*", "неисключительн*", '"прав* использовани*"', '"прав* на использовани*"', '"предоставлени* прав*"',
        "сертификат*", "активаци*", '"поддержк* производител*"', "антивирус*",
    )),
    "legal_systems": Filter("Справочные системы", (
        "консультант*", "гарант", '"справочно-правов*"', '"справочн* правов*"',
    )),
    "equipment": Filter("Оборудование и оргтехника", (
        "оборудовани*", "оргтехник*", "картридж*", '"контрольно-кассов*"', "комплектующ*",
    )),
    "repair": Filter("Ремонт и техобслуживание", (
        "ремонт*", '"техническ* обслуживани*"', "заправк*", "поверк*",
    )),
    "construction": Filter("Стройка и монтаж", (
        "строительств*", "монтаж*", "реконструкци*", '"проектно-сметн*"', "видеонаблюдени*", "скуд",
    )),
    "roads": Filter("Содержание дорог", (
        '"содержани* автомобильн* дорог*"', "улично-дорожн*", "благоустройств*", "озеленени*", "путепровод*",
    )),
    "medicine": Filter("Медицина и лаборатории", (
        "медицинск*", "лабораторн*", "реагент*", "клинико-диагностическ*",
    )),
    "training": Filter("Обучение и мероприятия", (
        "обучени*", '"повышени* квалификаци*"', '"образовательн* услуг*"', "семинар*", "конференци*",
    )),
    "print": Filter("Печать и подписка", (
        '"печатн* издани*"', "подписк*", "полиграф*",
    )),
}


def filter_minus(keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """(minus word, the title of its filter) for every word of the filters switched on."""
    return [(word, FILTERS[key].title) for key in keys if key in FILTERS for word in FILTERS[key].minus]
