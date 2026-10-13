"""Local copy of downloaded notices: one XML file per registry number plus an index in SQLite.

A notice is downloaded again only when the search shows a later "Обновлено" date than the one seen at the previous
download. The search shows dates without the time, so a second edit made on the same day after the download is
picked up with the next update on a later day.

A notice the site would not give is remembered too. Some never come through — road maintenance contracts with
thousands of positions break off on the way — and an hourly check should not spend its retries on them every hour:
after FAILURES_BEFORE_REST failed runs a notice rests for FAILURE_REST before the next attempt.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .website.search import SearchHit

REPLACE_ATTEMPTS = 10
REPLACE_PAUSE = 0.2  # seconds between attempts to rename a fresh download into place
FAILURES_BEFORE_REST = 2
FAILURE_REST = timedelta(hours=24)


@dataclass(frozen=True)
class Failure:
    attempts: int  # runs in a row that could not download the notice
    failed_at: datetime
    reason: str


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
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS failures (reg_number TEXT PRIMARY KEY, attempts INTEGER NOT NULL,"
                " failed_at TEXT NOT NULL, reason TEXT NOT NULL)"
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
            self._db.execute("DELETE FROM failures WHERE reg_number = ?", (hit.reg_number,))
            self._db.execute(
                "INSERT OR REPLACE INTO notices VALUES (?, ?, ?)",
                (
                    hit.reg_number,
                    hit.updated.isoformat() if hit.updated else None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        return path

    def record_failure(self, reg_number: str, reason: str, at: datetime | None = None) -> None:
        moment = (at or datetime.now()).isoformat(timespec="seconds")
        with self._db:
            self._db.execute(
                "INSERT INTO failures VALUES (?, 1, ?, ?) ON CONFLICT(reg_number) DO UPDATE SET "
                "attempts = attempts + 1, failed_at = excluded.failed_at, reason = excluded.reason",
                (reg_number, moment, reason),
            )

    def failure(self, reg_number: str) -> Failure | None:
        row = self._db.execute(
            "SELECT attempts, failed_at, reason FROM failures WHERE reg_number = ?", (reg_number,)
        ).fetchone()
        return Failure(row[0], datetime.fromisoformat(row[1]), row[2]) if row else None

    def resting(self, reg_number: str, now: datetime | None = None) -> Failure | None:
        """The failure that keeps a notice out of this run, if it has failed often and recently enough."""
        failure = self.failure(reg_number)
        if failure is None or failure.attempts < FAILURES_BEFORE_REST:
            return None
        return failure if (now or datetime.now()) - failure.failed_at < FAILURE_REST else None

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
