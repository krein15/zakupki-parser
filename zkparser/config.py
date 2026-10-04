"""Secrets and personal settings from ``.env``: the Telegram bot token and the default chat.

Values come from environment variables first, then from ``.env`` in the current folder (the project folder when
Task Scheduler starts the program), then from ``.env`` in the program's data folder. ``.env`` never goes to git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .settings import app_data_dir

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
CHAT_KEY = "TELEGRAM_CHAT_ID"


@dataclass(frozen=True)
class TelegramSettings:
    token: str = ""
    chat: str = ""  # the default chat; a profile can name its own

    def __repr__(self) -> str:  # the token must not show up in logs or tracebacks
        return f"TelegramSettings(token={'***' if self.token else ''!r}, chat={self.chat!r})"


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env_files() -> list[Path]:
    return [Path.cwd() / ".env", app_data_dir() / ".env"]


def load_telegram_settings(files: list[Path] | None = None) -> TelegramSettings:
    found: dict[str, str] = {}
    for path in reversed(files if files is not None else env_files()):  # earlier files win
        found |= {key: value for key, value in read_env_file(path).items() if value}
    return TelegramSettings(
        token=os.environ.get(TOKEN_KEY) or found.get(TOKEN_KEY, ""),
        chat=os.environ.get(CHAT_KEY) or found.get(CHAT_KEY, ""),
    )


def save_env_value(path: Path, key: str, value: str) -> None:
    """Set ``key`` in an .env file, keeping every other line as it is."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
