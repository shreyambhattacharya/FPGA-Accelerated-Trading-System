"""Explicit intraday session filters and deterministic session boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

from .canonical import NormalizedEvent, sort_events


@dataclass(frozen=True)
class SessionConfig:
    timezone_name: str = "America/New_York"
    regular_start: str = "09:30"
    regular_end: str = "16:00"
    extended_hours: bool = False
    extended_start: str = "04:00"
    extended_end: str = "20:00"
    reset_each_session: bool = True
    reset_replay_sequences: bool = True
    exclude_weekends: bool = True
    exclude_us_holidays: bool = True

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def _bounds(self, extended: bool | None = None) -> tuple[time, time]:
        use_extended = self.extended_hours if extended is None else extended
        start_text, end_text = (
            (self.extended_start, self.extended_end)
            if use_extended
            else (self.regular_start, self.regular_end)
        )
        return _parse_time(start_text), _parse_time(end_text)

    def session_date(self, timestamp_ns: int) -> date:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(
            self.timezone
        ).date()

    def local_datetime(self, timestamp_ns: int) -> datetime:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(
            self.timezone
        )

    def in_session(self, timestamp_ns: int) -> bool:
        local = self.local_datetime(timestamp_ns)
        if self.exclude_weekends and local.weekday() >= 5:
            return False
        if self.exclude_us_holidays and is_us_market_holiday(local.date()):
            return False
        start, end = self._bounds()
        if start <= local.time() < end:
            return True
        if not self.extended_hours:
            return False
        extended_start, extended_end = self._bounds(extended=True)
        return extended_start <= local.time() < extended_end

    def filter_events(self, events: list[NormalizedEvent]) -> list[NormalizedEvent]:
        return [event for event in sort_events(events) if self.in_session(event.timestamp_ns)]


def _parse_time(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid session time {value!r}; expected HH:MM") from exc


def is_us_market_holiday(value: date) -> bool:
    """Return the standard full-day US equity holiday set for a date."""

    def observed(day: date) -> date:
        if day.weekday() == 5:
            return day - timedelta(days=1)
        if day.weekday() == 6:
            return day + timedelta(days=1)
        return day

    new_year = observed(date(value.year, 1, 1))
    juneteenth = observed(date(value.year, 6, 19))
    independence = observed(date(value.year, 7, 4))
    christmas = observed(date(value.year, 12, 25))
    mlk = _nth_weekday(value.year, 1, 0, 3)
    presidents = _nth_weekday(value.year, 2, 0, 3)
    memorial = _last_weekday(value.year, 5, 0)
    labor = _nth_weekday(value.year, 9, 0, 1)
    thanksgiving = _nth_weekday(value.year, 11, 3, 4)
    good_friday = _easter(value.year) - timedelta(days=2)
    return value in {new_year, juneteenth, independence, christmas, mlk, presidents, memorial, labor, thanksgiving, good_friday}


def _nth_weekday(year, month, weekday, ordinal):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (ordinal - 1))


def _last_weekday(year, month, weekday):
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    day = next_month - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter(year):
    # Anonymous Gregorian computus, sufficient for the exchange calendar.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)
