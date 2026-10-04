"""Running without a console: pythonw.exe from Task Scheduler keeps its output in a file."""

from __future__ import annotations

import sys

from zkparser.__main__ import capture_console


def test_output_goes_to_a_file_without_a_console(tmp_path, monkeypatch):
    monkeypatch.setattr("zkparser.settings.app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    capture_console()
    print("сообщение")
    sys.stderr.write("Traceback: что-то сломалось\n")
    stream = sys.stdout
    stream.close()
    text = (tmp_path / "logs" / "console.log").read_text(encoding="utf-8")
    assert "сообщение" in text
    assert "Traceback: что-то сломалось" in text


def test_a_real_console_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("zkparser.settings.app_data_dir", lambda: tmp_path)
    before = sys.stdout, sys.stderr
    capture_console()
    assert (sys.stdout, sys.stderr) == before
    assert not (tmp_path / "logs").exists()
