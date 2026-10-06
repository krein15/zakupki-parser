"""Local copy of downloaded notices: one XML file per registry number plus an index in SQLite.

A notice is downloaded again only when the search shows a later "Обновлено" date than the one seen at the previous
download. The search shows dates without the time, so a second edit made on the same day after the download is
picked up with the next update on a later day.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from .website.search import SearchHit

REPLACE_ATTEMPTS = 10
REPLACE_PAUSE = 0.2  # seconds between attempts to rename a fresh download into place


class NoticeCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.notices_dir = root / "notices"
        self.notices_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(root / "index.sqlite3")
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS notices "
                "(reg_number TEXT PRIMARY KEY, updated TEXT, fetched_at TEXT NOT NULL)"
            )

    def __enter__(self) -> NoticeCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def path(self, reg_number: str) -> Path:
        if not reg_number.isdigit():  # the number comes from the site and becomes a file name
            raise ValueError(f"Not a registry number: {reg_number!r}")
        return self.notices_dir / f"{reg_number}.xml"

    def is_fresh(self, hit: SearchHit) -> bool:
        """True if the cached copy is as recent as the search says the notice is."""
        if not self.path(hit.reg_number).exists():
            return False
        row = self._db.execute("SELECT updated FROM notices WHERE reg_number = ?", (hit.reg_number,)).fetchone()
        if row is None:
            return False
        if hit.updated is None:
            return True
        return row[0] is not None and date.fromisoformat(row[0]) >= hit.updated

    def store(self, hit: SearchHit, xml: bytes) -> Path:
        path = self.path(hit.reg_number)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(xml)
        _replace(temporary, path)
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO notices VALUES (?, ?, ?)",
                (
                    hit.reg_number,
                    hit.updated.isoformat() if hit.updated else None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        return path

    def load(self, reg_number: str) -> bytes | None:
        path = self.path(reg_number)
        return path.read_bytes() if path.exists() else None


def _replace(source: Path, target: Path) -> None:
    """Rename over the old copy. Windows refuses while another process holds either file — an antivirus checking
    the fresh download, a search indexer — so the rename is retried for a moment before giving up."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE)
