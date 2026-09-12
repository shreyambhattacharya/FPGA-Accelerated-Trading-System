"""Explicit intraday session filters and deterministic session boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
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
