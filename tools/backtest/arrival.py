"""Historical arrival-rate, inter-arrival, and serialized-service analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pickle
from statistics import mean, median
import time


def percentile(values, fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def distribution(values) -> dict:
    values = list(values)
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p1": percentile(values, .01),
        "p5": percentile(values, .05),
        "p10": percentile(values, .10),
        "p25": percentile(values, .25),
        "median": median(values) if values else None,
        "p75": percentile(values, .75),
        "p90": percentile(values, .90),
        "p95": percentile(values, .95),
        "p99": percentile(values, .99),
        "max": max(values) if values else None,
        "mean": mean(values) if values else None,
    }


class _OnlineStats:
    def __init__(self, *, sample_size=100_000, seed=7):
        self.count = 0
        self.total = 0.0
        self.minimum = None
        self.maximum = None
        self.sample_size = sample_size
        self.sample = []
        # A deterministic systematic sample avoids a Python RNG call on every
        # high-rate observation while retaining values across the stream.
        self.sample_stride = max(1, sample_size // 1000)

    def add(self, value):
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if len(self.sample) < self.sample_size:
            self.sample.append(value)
        elif self.count % self.sample_stride == 0:
            self.sample[(self.count // self.sample_stride) % self.sample_size] = value

    def to_dict(self):
        result = distribution(self.sample)
        result.update({
            "count": self.count,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.total / self.count if self.count else None,
            "quantile_method": "deterministic systematic sample",
            "quantile_sample_count": len(self.sample),
        })
        return result


def event_rate_analysis(events, *, windows_ns=(1_000_000_000, 100_000_000, 10_000_000, 1_000_000)) -> dict:
    events = sorted(events, key=lambda event: (event.timestamp_ns, event.source_index))
    groups = {"combined": events}
    for symbol_id in sorted({event.symbol_id for event in events}):
        groups[f"symbol_{symbol_id}"] = [event for event in events if event.symbol_id == symbol_id]
    result = {}
    for window_ns in windows_ns:
        label = _duration_label(window_ns)
        result[label] = {name: _window_rate(values, window_ns) for name, values in groups.items()}
    return result


def event_rate_analysis_stream(events, *, windows_ns=(1_000_000_000, 100_000_000, 10_000_000, 1_000_000)) -> dict:
    """Analyze a globally sorted event iterator without retaining timestamps."""

    states = {}
    labels = [_duration_label(window_ns) for window_ns in windows_ns]
    seeds = {(label, "combined"): sum((index + 1) * ord(character) for index, character in enumerate(f"{label}:combined")) for label in labels}

    def update(label, group, timestamp_ns, window_ns):
        key = (label, group)
        state = states.setdefault(key, {"start": timestamp_ns, "bucket": 0, "count": 0, "stats": _OnlineStats(seed=seeds.get(key, 7))})
        bucket = (timestamp_ns - state["start"]) // window_ns
        if state["count"] and bucket != state["bucket"]:
            state["stats"].add(state["count"] * 1_000_000_000 / window_ns)
            state["bucket"] = bucket
            state["count"] = 1
        else:
            state["count"] += 1

    for event in events:
        for label, window_ns in zip(labels, windows_ns):
            update(label, "combined", event.timestamp_ns, window_ns)
            update(label, f"symbol_{event.symbol_id}", event.timestamp_ns, window_ns)
    result = {}
    for label in labels:
        groups = {group: state["stats"].to_dict() | {"windows": state["stats"].count} for (state_label, group), state in states.items() if state_label == label}
        for (state_label, group), state in states.items():
            if state_label == label and state["count"]:
                state["stats"].add(state["count"] * 1_000_000_000 / windows_ns[labels.index(label)])
                groups[group] = state["stats"].to_dict() | {"windows": state["stats"].count}
        result[label] = groups
    return result


def interarrival_analysis(events, *, service_cycles: int = 536, clock_hz: int = 27_000_000) -> dict:
    events = sorted(events, key=lambda event: (event.timestamp_ns, event.source_index))
    groups = {"combined": events}
    for symbol_id in sorted({event.symbol_id for event in events}):
        groups[f"symbol_{symbol_id}"] = [event for event in events if event.symbol_id == symbol_id]
    result = {}
    for name, values in groups.items():
        deltas = [right.timestamp_ns - left.timestamp_ns for left, right in zip(values, values[1:])]
        stats = distribution(deltas)
        stats["units"] = "ns"
        stats["fraction_within_service_interval"] = sum(delta * clock_hz <= service_cycles * 1_000_000_000 for delta in deltas) / len(deltas) if deltas else None
        result[name] = stats
    return {"service_cycles": service_cycles, "clock_hz": clock_hz, "service_interval_ns": service_cycles * 1_000_000_000 / clock_hz, "groups": result}


def interarrival_analysis_stream(events, *, service_cycles: int = 536, clock_hz: int = 27_000_000) -> dict:
    states = {}
    for event in events:
        for group in ("combined", f"symbol_{event.symbol_id}"):
            state = states.setdefault(group, {"last": None, "stats": _OnlineStats(seed=17 + len(states)), "within": 0})
            if state["last"] is not None:
                delta = event.timestamp_ns - state["last"]
                state["stats"].add(delta)
                state["within"] += delta * clock_hz <= service_cycles * 1_000_000_000
            state["last"] = event.timestamp_ns
    groups = {}
    for group, state in states.items():
        result = state["stats"].to_dict()
        result["units"] = "ns"
        result["fraction_within_service_interval"] = state["within"] / state["stats"].count if state["stats"].count else None
        groups[group] = result
    return {"service_cycles": service_cycles, "clock_hz": clock_hz, "service_interval_ns": service_cycles * 1_000_000_000 / clock_hz, "groups": groups}


def simulate_serialized_capacity(events, *, service_cycles: int = 536, clock_hz: int = 27_000_000, depths=(16, 32, 64, 128, 256, 512, 1024)) -> dict:
    """Simulate a one-server FIFO using rational nanosecond service time."""

    ordered = sorted(events, key=lambda event: (event.timestamp_ns, event.source_index))
    service_num = service_cycles * 1_000_000_000
    result = {str(depth): _simulate_depth(ordered, depth, service_num, clock_hz) for depth in depths}
    infinite = _simulate_depth(ordered, None, service_num, clock_hz)
    return {
        "service_cycles": service_cycles,
        "clock_hz": clock_hz,
        "service_interval_ns": service_num / clock_hz,
        "finite_depths": result,
        "infinite_queue": infinite,
    }


def simulate_serialized_capacity_stream(events, *, service_cycles: int = 536, clock_hz: int = 27_000_000, depths=(16, 32, 64, 128, 256, 512, 1024)) -> dict:
    states = {depth: _CapacityState(depth, service_cycles * 1_000_000_000, clock_hz) for depth in depths}
    infinite = _CapacityState(None, service_cycles * 1_000_000_000, clock_hz)
    for event in events:
        for state in (*states.values(), infinite):
            state.observe(event.timestamp_ns)
    return {
        "service_cycles": service_cycles,
        "clock_hz": clock_hz,
        "service_interval_ns": service_cycles * 1_000_000_000 / clock_hz,
        "finite_depths": {str(depth): state.to_dict() for depth, state in states.items()},
        "infinite_queue": infinite.to_dict(),
    }


@dataclass
class _CombinedRateState:
    start: int | None
    bucket: int
    count: int
    stats: _OnlineStats

    @classmethod
    def create(cls, seed: int) -> "_CombinedRateState":
        return cls(None, 0, 0, _OnlineStats(sample_size=10_000, seed=seed))

    def observe(self, timestamp_ns: int, window_ns: int) -> None:
        if self.start is None:
            self.start = timestamp_ns
            self.bucket = 0
            self.count = 1
            return
        bucket = (timestamp_ns - self.start) // window_ns
        if bucket != self.bucket:
            if self.count:
                self.stats.add(self.count * 1_000_000_000 / window_ns)
            self.bucket = bucket
            self.count = 1
        else:
            self.count += 1

    def finish(self, window_ns: int) -> None:
        if self.count:
            self.stats.add(self.count * 1_000_000_000 / window_ns)
            self.count = 0


@dataclass
class _CombinedInterarrivalState:
    last: int | None
    stats: _OnlineStats
    within_service: int

    @classmethod
    def create(cls, seed: int) -> "_CombinedInterarrivalState":
        return cls(None, _OnlineStats(sample_size=10_000, seed=seed), 0)

    def observe(self, timestamp_ns: int, service_cycles: int, clock_hz: int) -> None:
        if self.last is not None:
            delta = timestamp_ns - self.last
            self.stats.add(delta)
            self.within_service += delta * clock_hz <= service_cycles * 1_000_000_000
        self.last = timestamp_ns


class _CombinedDiagnosticState:
    """Serializable bounded state for one combined timestamp-analysis pass."""

    def __init__(self, *, windows_ns, service_cycles, clock_hz, depths):
        self.windows_ns = tuple(windows_ns)
        self.service_cycles = service_cycles
        self.clock_hz = clock_hz
        self.depths = tuple(depths)
        self.processed_events = 0
        self.counts = {"total_events": 0, "quote_events": 0, "trade_events": 0, "bid_events": 0, "ask_events": 0}
        self.by_symbol = {}
        self.rate_states = {}
        self.interarrival_states = {"combined": _CombinedInterarrivalState.create(17)}
        self.capacity_states = {depth: _CapacityState(depth, service_cycles * 1_000_000_000, clock_hz) for depth in depths}
        self.capacity_states[None] = _CapacityState(None, service_cycles * 1_000_000_000, clock_hz)
        self.first_timestamp_ns = None
        self.last_timestamp_ns = None
        self.largest_gap_ns = 0

    def _rate(self, label: str, group: str) -> _CombinedRateState:
        key = (label, group)
        if key not in self.rate_states:
            seed = sum((index + 1) * ord(character) for index, character in enumerate(f"{label}:{group}"))
            self.rate_states[key] = _CombinedRateState.create(seed)
        return self.rate_states[key]

    def _interarrival(self, group: str) -> _CombinedInterarrivalState:
        if group not in self.interarrival_states:
            self.interarrival_states[group] = _CombinedInterarrivalState.create(17 + len(self.interarrival_states))
        return self.interarrival_states[group]

    def observe(self, event) -> None:
        timestamp_ns = event.timestamp_ns
        symbol = str(event.symbol_id)
        self.processed_events += 1
        self.counts["total_events"] += 1
        symbol_counts = self.by_symbol.setdefault(symbol, {"total_events": 0, "quote_events": 0, "trade_events": 0, "bid_events": 0, "ask_events": 0})
        symbol_counts["total_events"] += 1
        if event.event_type == 1:
            self.counts["quote_events"] += 1
            symbol_counts["quote_events"] += 1
            key = "bid_events" if event.side == 0 else "ask_events"
            self.counts[key] += 1
            symbol_counts[key] += 1
        elif event.event_type == 2:
            self.counts["trade_events"] += 1
            symbol_counts["trade_events"] += 1

        for window_ns in self.windows_ns:
            label = _duration_label(window_ns)
            self._rate(label, "combined").observe(timestamp_ns, window_ns)
            self._rate(label, f"symbol_{symbol}").observe(timestamp_ns, window_ns)
        self._interarrival("combined").observe(timestamp_ns, self.service_cycles, self.clock_hz)
        self._interarrival(f"symbol_{symbol}").observe(timestamp_ns, self.service_cycles, self.clock_hz)
        for capacity in self.capacity_states.values():
            capacity.observe(timestamp_ns)
        if self.first_timestamp_ns is None:
            self.first_timestamp_ns = timestamp_ns
        if self.last_timestamp_ns is not None:
            self.largest_gap_ns = max(self.largest_gap_ns, timestamp_ns - self.last_timestamp_ns)
        self.last_timestamp_ns = timestamp_ns

    def finalize(self) -> None:
        for (label, _group), state in self.rate_states.items():
            state.finish({"1s": 1_000_000_000, "100ms": 100_000_000, "10ms": 10_000_000, "1ms": 1_000_000}[label])

    def to_dict(self) -> dict:
        self.finalize()
        event_rates = {}
        for window_ns in self.windows_ns:
            label = _duration_label(window_ns)
            groups = {}
            for (state_label, group), state in self.rate_states.items():
                if state_label != label:
                    continue
                value = state.stats.to_dict()
                value.update({
                    "mean_events_per_second": value["mean"],
                    "median_events_per_second": value["median"],
                    "p95_events_per_second": value["p95"],
                    "p99_events_per_second": value["p99"],
                    "maximum_events_per_second": value["max"],
                })
                value["windows"] = state.stats.count
                groups[group] = value
            event_rates[label] = groups

        interarrival_groups = {}
        for group, state in self.interarrival_states.items():
            value = state.stats.to_dict()
            value["units"] = "ns"
            value["fraction_within_service_interval"] = state.within_service / state.stats.count if state.stats.count else None
            interarrival_groups[group] = value
        interarrival = {
            "service_cycles": self.service_cycles,
            "clock_hz": self.clock_hz,
            "service_interval_ns": self.service_cycles * 1_000_000_000 / self.clock_hz,
            "groups": interarrival_groups,
        }
        capacity = {
            "service_cycles": self.service_cycles,
            "clock_hz": self.clock_hz,
            "service_interval_ns": self.service_cycles * 1_000_000_000 / self.clock_hz,
            "finite_depths": {str(depth): state.to_dict() for depth, state in self.capacity_states.items() if depth is not None},
            "infinite_queue": self.capacity_states[None].to_dict(),
        }
        return {
            "aggregate": dict(self.counts),
            "by_symbol": {symbol: dict(values) for symbol, values in sorted(self.by_symbol.items(), key=lambda item: int(item[0]))},
            "event_rates": event_rates,
            "interarrival": interarrival,
            "pipeline_capacity": capacity,
            "first_event_timestamp_ns": self.first_timestamp_ns,
            "last_event_timestamp_ns": self.last_timestamp_ns,
            "largest_event_gap_ns": self.largest_gap_ns,
            "processed_events": self.processed_events,
            "quantile_method": "deterministic systematic sample; counts, extrema, means, queue occupancy, overflow, and maximum rates are exact",
        }


def combined_timestamp_analysis(
    event_factory,
    *,
    total_events: int | None = None,
    checkpoint_path: Path | None = None,
    state_manifest_path: Path | None = None,
    input_identity: dict | None = None,
    progress_callback=None,
    windows_ns=(1_000_000_000, 100_000_000, 10_000_000, 1_000_000),
    service_cycles: int = 536,
    clock_hz: int = 27_000_000,
    depths=(16, 32, 64, 128, 256, 512, 1024),
    checkpoint_event_interval: int = 5_000_000,
    progress_event_interval: int = 1_000_000,
    progress_time_seconds: float = 30.0,
) -> dict:
    """Calculate all timestamp-only diagnostics in one resumable pass."""

    state = None
    if checkpoint_path and state_manifest_path and checkpoint_path.exists() and state_manifest_path.exists():
        try:
            manifest = json.loads(state_manifest_path.read_text(encoding="utf-8"))
            if manifest.get("phase") == "combined_timestamp_analysis_in_progress" and manifest.get("input_identity") == (input_identity or {}):
                with checkpoint_path.open("rb") as source:
                    state = pickle.load(source)
        except (OSError, ValueError, EOFError, pickle.PickleError, AttributeError, KeyError):
            state = None
    if state is None:
        state = _CombinedDiagnosticState(windows_ns=windows_ns, service_cycles=service_cycles, clock_hz=clock_hz, depths=depths)
    start_index = state.processed_events
    started = time.perf_counter()
    last_progress_events = start_index
    last_progress_time = started
    last_checkpoint_events = start_index

    def report(force=False):
        nonlocal last_progress_events, last_progress_time
        now = time.perf_counter()
        if not force and state.processed_events - last_progress_events < progress_event_interval and now - last_progress_time < progress_time_seconds:
            return
        elapsed = now - started
        rate = (state.processed_events - start_index) / elapsed if elapsed else 0.0
        remaining = ((total_events - state.processed_events) / rate) if total_events is not None and rate > 0 else None
        payload = {
            "processed_events": state.processed_events,
            "total_events": total_events,
            "percent_complete": (state.processed_events / total_events * 100.0) if total_events else None,
            "elapsed_time": elapsed,
            "processing_rate_events_per_second": rate,
            "estimated_remaining_time": remaining,
        }
        if progress_callback:
            progress_callback(payload)
        last_progress_events = state.processed_events
        last_progress_time = now

    def checkpoint():
        if not checkpoint_path or not state_manifest_path:
            return
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        partial = checkpoint_path.with_name(checkpoint_path.name + ".part")
        with partial.open("wb") as destination:
            pickle.dump(state, destination, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(partial, checkpoint_path)
        _write_analysis_state(state_manifest_path, {
            "phase": "combined_timestamp_analysis_in_progress",
            "processed_events": state.processed_events,
            "total_events": total_events,
            "input_identity": input_identity or {},
            "checkpoint_path": str(checkpoint_path),
        })

    try:
        for event in event_factory(start_index):
            state.observe(event)
            report()
            if state.processed_events - last_checkpoint_events >= checkpoint_event_interval:
                checkpoint()
                last_checkpoint_events = state.processed_events
    except BaseException:
        # A user interrupt or worker failure must leave a resumable checkpoint
        # behind, while the exception still propagates to the caller.
        checkpoint()
        report(force=True)
        raise
    report(force=True)
    result = state.to_dict()
    if state_manifest_path:
        _write_analysis_state(state_manifest_path, {
            "phase": "combined_timestamp_analysis_complete",
            "processed_events": state.processed_events,
            "total_events": total_events,
            "input_identity": input_identity or {},
            "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        })
    return result


def _write_analysis_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    preserved_phases = existing.get("phases") if isinstance(existing, dict) else None
    if preserved_phases is not None and "phases" not in value:
        value = dict(value)
        value["phases"] = preserved_phases
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


class _CapacityState:
    def __init__(self, depth, service_num, clock_hz):
        self.depth = depth
        self.service_num = service_num
        self.clock_hz = clock_hz
        self.server_available = 0
        self.occupancies = _OnlineStats(seed=101 + (depth or 0))
        self.overflow = 0
        self.busy_start = None
        self.current_busy_end = None
        self.longest_busy = 0
        self.last_timestamp = None

    def observe(self, timestamp_ns):
        arrival = timestamp_ns * self.clock_hz
        if self.server_available <= arrival:
            if self.busy_start is not None and self.current_busy_end is not None:
                self.longest_busy = max(self.longest_busy, (self.current_busy_end // self.clock_hz) - (self.busy_start // self.clock_hz))
            self.busy_start = arrival
        outstanding = 0 if self.server_available <= arrival else (self.server_available - arrival + self.service_num - 1) // self.service_num
        if self.depth is not None and outstanding >= self.depth:
            self.overflow += 1
            self.occupancies.add(outstanding)
            self.last_timestamp = timestamp_ns
            return
        start = max(self.server_available, arrival)
        self.server_available = start + self.service_num
        self.current_busy_end = self.server_available
        self.occupancies.add((self.server_available - arrival + self.service_num - 1) // self.service_num)
        self.last_timestamp = timestamp_ns

    def to_dict(self):
        if self.busy_start is not None and self.current_busy_end is not None:
            self.longest_busy = max(self.longest_busy, (self.current_busy_end // self.clock_hz) - (self.busy_start // self.clock_hz))
        result = self.occupancies.to_dict()
        return {
            "max_occupancy": result["max" ] or 0,
            "mean_occupancy": result["mean"] or 0.0,
            "p95_occupancy": result["p95"] or 0.0,
            "p99_occupancy": result["p99"] or 0.0,
            "overflow_events": self.overflow,
            "longest_non_empty_interval_ns": self.longest_busy,
            "maximum_backlog_duration_ns": max(0, (self.server_available // self.clock_hz) - (self.last_timestamp or 0)),
        }


def _window_rate(events, window_ns):
    if not events:
        return {"windows": 0, "mean_events_per_second": 0.0, "median_events_per_second": 0.0, "p95_events_per_second": 0.0, "p99_events_per_second": 0.0, "maximum_events_per_second": 0.0}
    start = events[0].timestamp_ns
    buckets = defaultdict(int)
    for event in events:
        buckets[(event.timestamp_ns - start) // window_ns] += 1
    rates = [count * 1_000_000_000 / window_ns for count in buckets.values()]
    return {
        "windows": len(rates),
        "mean_events_per_second": mean(rates),
        "median_events_per_second": median(rates),
        "p95_events_per_second": percentile(rates, .95),
        "p99_events_per_second": percentile(rates, .99),
        "maximum_events_per_second": max(rates),
    }


def _simulate_depth(events, depth, service_num, clock_hz):
    if not events:
        return {"max_occupancy": 0, "mean_occupancy": 0.0, "p95_occupancy": 0.0, "p99_occupancy": 0.0, "overflow_events": 0, "longest_non_empty_interval_ns": 0.0, "maximum_backlog_duration_ns": 0.0}
    server_available = 0
    occupancies = []
    overflow = 0
    busy_start = None
    longest_busy = 0
    current_busy_end = None
    for event in events:
        arrival = event.timestamp_ns * clock_hz
        if server_available <= arrival:
            if busy_start is not None and current_busy_end is not None:
                longest_busy = max(longest_busy, (current_busy_end // clock_hz) - (busy_start // clock_hz))
            busy_start = arrival
        outstanding = 0 if server_available <= arrival else (server_available - arrival + service_num - 1) // service_num
        if depth is not None and outstanding >= depth:
            overflow += 1
            occupancies.append(outstanding)
            continue
        start = max(server_available, arrival)
        server_available = start + service_num
        current_busy_end = server_available
        occupancy_after = (server_available - arrival + service_num - 1) // service_num
        occupancies.append(occupancy_after)
    if busy_start is not None and current_busy_end is not None:
        longest_busy = max(longest_busy, (current_busy_end // clock_hz) - (busy_start // clock_hz))
    return {
        "max_occupancy": max(occupancies, default=0),
        "mean_occupancy": mean(occupancies) if occupancies else 0.0,
        "p95_occupancy": percentile(occupancies, .95) if occupancies else 0.0,
        "p99_occupancy": percentile(occupancies, .99) if occupancies else 0.0,
        "overflow_events": overflow,
        "longest_non_empty_interval_ns": longest_busy,
        "maximum_backlog_duration_ns": max(0, (server_available // clock_hz) - events[-1].timestamp_ns),
    }


def _duration_label(window_ns):
    return {1_000_000_000: "1s", 100_000_000: "100ms", 10_000_000: "10ms", 1_000_000: "1ms"}.get(window_ns, f"{window_ns}ns")
