"""Chronological, session-date-based train/validation/test utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .canonical import NormalizedEvent, sort_events
from .session import SessionConfig


@dataclass(frozen=True)
class DatasetSplit:
    train: list[NormalizedEvent]
    validation: list[NormalizedEvent]
    test: list[NormalizedEvent]
    train_dates: list[date]
    validation_dates: list[date]
    test_dates: list[date]


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train: list[NormalizedEvent]
    validation: list[NormalizedEvent]
    test: list[NormalizedEvent]
    train_dates: list[date]
    validation_dates: list[date]
    test_dates: list[date]


def _date_map(events, session: SessionConfig):
    mapping: dict[date, list[NormalizedEvent]] = {}
    for event in sort_events(events):
        mapping.setdefault(session.session_date(event.timestamp_ns), []).append(event)
    return mapping


def chronological_split(events, *, session: SessionConfig | None = None, train_fraction=.6, validation_fraction=.2) -> DatasetSplit:
    if train_fraction <= 0 or validation_fraction < 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must leave a non-empty test split")
    session = session or SessionConfig()
    mapping = _date_map(events, session)
    dates = sorted(mapping)
    train_end = max(1, int(len(dates) * train_fraction))
    validation_end = min(len(dates) - 1, train_end + int(len(dates) * validation_fraction))
    train_dates, validation_dates, test_dates = dates[:train_end], dates[train_end:validation_end], dates[validation_end:]
    return DatasetSplit(
        train=_events_for(mapping, train_dates),
        validation=_events_for(mapping, validation_dates),
        test=_events_for(mapping, test_dates),
        train_dates=train_dates,
        validation_dates=validation_dates,
        test_dates=test_dates,
    )


def walk_forward_windows(events, *, session: SessionConfig | None = None, train_days=20, validation_days=0, test_days=5, step_days=None) -> list[WalkForwardWindow]:
    if min(train_days, test_days) < 1 or validation_days < 0:
        raise ValueError("train_days and test_days must be positive")
    session = session or SessionConfig()
    mapping = _date_map(events, session)
    dates = sorted(mapping)
    step = step_days or test_days
    result = []
    index = 0
    start = 0
    while start + train_days + validation_days + test_days <= len(dates):
        train_dates = dates[start : start + train_days]
        validation_start = start + train_days
        validation_dates = dates[validation_start : validation_start + validation_days]
        test_start = validation_start + validation_days
        test_dates = dates[test_start : test_start + test_days]
        result.append(WalkForwardWindow(
            index=index,
            train=_events_for(mapping, train_dates),
            validation=_events_for(mapping, validation_dates),
            test=_events_for(mapping, test_dates),
            train_dates=train_dates,
            validation_dates=validation_dates,
            test_dates=test_dates,
        ))
        start += step
        index += 1
    return result


def _events_for(mapping, dates):
    return [event for day in dates for event in mapping.get(day, [])]
