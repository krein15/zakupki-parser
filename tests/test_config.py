""".env: reading, precedence, saving the default chat, and a token that never shows in repr."""

from __future__ import annotations

from zkparser.config import TelegramSettings, load_telegram_settings, read_env_file, save_env_value

TOKEN = "1234567890:AAEabcdefghijklmnopqrstuvwxyz012345"


def test_read_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        '﻿# comment\n\nTELEGRAM_BOT_TOKEN = "abc"\nTELEGRAM_CHAT_ID=\'42\'\nBROKEN LINE\nEMPTY=\n', encoding="utf-8"
    )
    assert read_env_file(path) == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42", "EMPTY": ""}


def test_missing_file_is_empty(tmp_path):
    assert read_env_file(tmp_path / ".env") == {}


def test_earlier_files_and_environment_win(tmp_path, monkeypatch):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    second.write_text("TELEGRAM_BOT_TOKEN=from-second\nTELEGRAM_CHAT_ID=2\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert load_telegram_settings([first, second]) == TelegramSettings("from-second", "1")  # empty values do not count
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "3")
    assert load_telegram_settings([first, second]).chat == "3"


def test_token_is_hidden_in_repr():
    settings = TelegramSettings(TOKEN, "42")
    assert TOKEN not in repr(settings)
    assert "***" in repr(settings)


def test_save_env_value_keeps_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"# bot\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    save_env_value(path, "TELEGRAM_CHAT_ID", "42")
    save_env_value(path, "OTHER", "x")
    assert path.read_text(encoding="utf-8") == f"# bot\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=42\nOTHER=x\n"


def test_save_env_value_creates_the_file(tmp_path):
    save_env_value(tmp_path / ".env", "TELEGRAM_CHAT_ID", "42")
    assert read_env_file(tmp_path / ".env") == {"TELEGRAM_CHAT_ID": "42"}
