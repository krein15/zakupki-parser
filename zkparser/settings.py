"""Where the program keeps its data."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Zakupki Parser"


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    path = Path(base) / APP_NAME.replace(" ", "")
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_cache_dir() -> Path:
    return app_data_dir() / "cache"


def default_output_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME
