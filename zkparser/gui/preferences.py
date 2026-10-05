"""What the window remembers between launches: the open profile, the period, the folders, the schedule."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path

from ..settings import app_data_dir

log = logging.getLogger(__name__)

TODAY, THREE_DAYS, WEEK, CUSTOM = "Сегодня", "3 дня", "Неделя", "Свой период"
PERIODS = (TODAY, THREE_DAYS, WEEK, CUSTOM)
PERIOD_DAYS = {TODAY: 0, THREE_DAYS: 2, WEEK: 6}
DATE_FORMAT = "%d.%m.%Y"


@dataclass
class Preferences:
    profile: str = ""  # file name in the profiles folder
    period: str = TODAY
    date_from: str = ""
    date_to: str = ""
    output_dir: str = ""
    open_report: bool = False
    appearance: str = "Системная"
    monitored: list[str] = field(default_factory=list)  # profile file names checked by the schedule
    schedule_from: str = "08:00"
    schedule_to: str = "20:00"
    schedule_every: int = 60
    export_regions: list[str] = field(default_factory=list)  # the "По бюджету" tab
    export_price_from: str = ""
    export_price_to: str = ""
    export_words: str = ""
    export_details: bool = False

    @staticmethod
    def default_path() -> Path:
        return app_data_dir() / "window.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Preferences:
        path = path or cls.default_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            log.warning("Window settings in %s are unreadable, starting with defaults", path)
            return cls()
        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        try:
            return cls(**values)
        except TypeError:
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), DATE_FORMAT).date()
    except ValueError:
        return None


def period_dates(period: str, today: date, date_from: str = "", date_to: str = "") -> tuple[date, date]:
    """The period of a preset, or of the two dates typed in. Raises ValueError with a message for the user."""
    if period in PERIOD_DAYS:
        return today - timedelta(days=PERIOD_DAYS[period]), today
    start, end = parse_date(date_from), parse_date(date_to)
    if start is None or end is None:
        raise ValueError("Укажите даты периода в виде ДД.ММ.ГГГГ")
    if start > end:
        raise ValueError("Начало периода позже конца")
    if end > today:
        raise ValueError("Конец периода — в будущем")
    return start, end
