"""Text helpers of the window's forms. No Tk here, so they are tested without a screen."""

from __future__ import annotations

import re
from datetime import date
from pathlib import PurePath


def split_list(text: str) -> list[str]:
    """Codes or INNs typed with commas, semicolons or spaces between them."""
    return [item for item in re.split(r"[,;\s]+", text) if item]


def safe_file_name(name: str) -> str:
    return " ".join(re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).split())[:60] or "профиль"


def without_path(error: Exception) -> str:
    """A profile error starts with the file name; in the window the form is the profile, so it is dropped."""
    text = str(error)
    head, separator, rest = text.partition(": ")
    return rest if separator and head.endswith(".toml") else text


def period_text(start: date, end: date) -> str:
    return f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m}–{end:%d.%m.%Y}"


def short_path(path: PurePath, limit: int = 40) -> str:
    """A long folder path cut to fit one line: the drive and the last two folders."""
    text = str(path)
    if len(text) <= limit or len(path.parts) <= 3:
        return text
    return str(type(path)(path.parts[0], "…", *path.parts[-2:]))
