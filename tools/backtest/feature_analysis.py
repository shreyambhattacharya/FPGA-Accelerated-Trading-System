"""Exact-integer feature distributions and candidate summaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from array import array

from .arrival import distribution, percentile


FEATURES = {
    "spread_bps_x100": ("spread_bps_x100", "spread_bps_x100_valid"),
    "momentum_bps_x100": ("momentum_bps_x100", "momentum_bps_x100_valid"),
    "imbalance_q15": ("imbalance_normalized", "imbalance_normalized_valid"),
    "midpoint_minus_vwap_bps_x100": ("midpoint_minus_vwap_bps_x100", "midpoint_minus_vwap_bps_x100_valid"),
    "rolling_volume": ("rolling_volume", None),
    "vwap": ("vwap", "vwap_quotient_valid"),
}


class _StreamingDistribution:
    """Bounded-memory exact counters with deterministic quantile sampling."""

    def __init__(self, *, sample_size=100_000, seed=7):
        self.count = 0
        self.invalid_count = 0
        self.minimum = None
        self.maximum = None
        self.total = 0
        self.sample = array("q")
        self.sample_size = sample_size
        self.sample_stride = max(1, sample_size // 1000)

    def observe(self, value, *, valid=True):
        if not valid:
            self.invalid_count += 1
            return
        value = int(value)
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
            "valid_count": self.count,
            "invalid_count": self.invalid_count,
            "quantile_method": "deterministic systematic sample",
            "quantile_sample_count": len(self.sample),
        })
        return result


class StreamingFeatureAccumulator:
    """Collect all feature counts while bounding quantile memory usage."""

    def __init__(self, *, sample_size=100_000):
        self.total_feature_records = 0
        self.groups = {"combined": self._new_group(sample_size, 7)}
        self.sample_size = sample_size

    @staticmethod
    def _new_group(sample_size, seed):
        return {name: _StreamingDistribution(sample_size=sample_size, seed=seed + index) for index, name in enumerate((*FEATURES, "signal_score"))}

    def _group(self, name):
        if name not in self.groups:
            self.groups[name] = self._new_group(self.sample_size, 1000 + len(self.groups) * 31)
        return self.groups[name]

    def observe(self, candidate):
        self.total_feature_records += 1
        groups = (self._group("combined"), self._group(f"symbol_{candidate.symbol_id}"))
        for group in groups:
            for label, (attribute, valid_attribute) in FEATURES.items():
                valid = valid_attribute is None or bool(getattr(candidate.feature, valid_attribute))
                group[label].observe(getattr(candidate.feature, attribute), valid=valid)
            group["signal_score"].observe(candidate.score)

    def to_dict(self):
        result = {name: {} for name in (*FEATURES, "signal_score")}
        for group_name, group in sorted(self.groups.items()):
            for name in result:
                result[name][group_name] = group[name].to_dict()
        return result


class StreamingCandidateAccumulator:
    """Candidate counters that do not retain SIGNAL_NONE rows."""

    def __init__(self, *, timezone_name="America/New_York"):
        from zoneinfo import ZoneInfo

        self.zone = ZoneInfo(timezone_name)
        self.total_feature_records = 0
        self.action_counts = Counter()
        self.by_symbol = Counter()
        self.by_score = Counter()
        self.by_reason = Counter()
        self.by_day = Counter()
        self.invalid = 0
        self.disabled = 0
        self.cooldown = 0
        self.first_timestamp = None
        self.last_timestamp = None

    def observe(self, candidate):
        action_names = {0: "SIGNAL_NONE", 1: "LONG_CANDIDATE", 2: "SHORT_CANDIDATE"}
        self.total_feature_records += 1
        self.action_counts[action_names.get(candidate.action, f"ACTION_{candidate.action}")] += 1
        self.invalid += bool(candidate.reason_bits & 0x0100)
        self.disabled += bool(candidate.reason_bits & (0x0200 | 0x0400 | 0x0800 | 0x1000))
        self.cooldown += bool(candidate.reason_bits & 0x0080)
        if self.first_timestamp is None:
            self.first_timestamp = candidate.timestamp_ns
        self.last_timestamp = candidate.timestamp_ns
        if candidate.action != 0:
            self.by_symbol[str(candidate.symbol_id)] += 1
            self.by_score[str(candidate.score)] += 1
            self.by_reason[str(candidate.reason_bits)] += 1
            day = datetime.fromtimestamp(candidate.timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(self.zone).date().isoformat()
            self.by_day[day] += 1

    def to_dict(self):
        span_ns = (self.last_timestamp - self.first_timestamp) if self.first_timestamp is not None and self.last_timestamp is not None else 0
        count = self.total_feature_records
        candidates = sum(value for name, value in self.action_counts.items() if name != "SIGNAL_NONE")
        return {
            "total_feature_records": count,
            "action_counts": dict(self.action_counts),
            "candidate_count": candidates,
            "candidate_rate_per_minute": candidates / (span_ns / 60_000_000_000) if span_ns else 0.0,
            "candidate_rate_per_1000_events": candidates / count * 1000 if count else 0.0,
            "candidate_count_by_symbol": dict(sorted(self.by_symbol.items())),
            "candidate_count_by_day": dict(sorted(self.by_day.items())),
            "candidate_count_by_score": dict(sorted(self.by_score.items(), key=lambda item: int(item[0]))),
            "candidate_count_by_reason_bits": dict(sorted(self.by_reason.items(), key=lambda item: int(item[0]))),
            "prevention_counts_from_reason_bits": {"invalid_features": self.invalid, "disabled_state": self.disabled, "cooldown": self.cooldown},
        }


def feature_distributions(candidates, *, per_symbol=True) -> dict:
    groups = {"combined": list(candidates)}
    if per_symbol:
        for symbol_id in sorted({candidate.symbol_id for candidate in candidates}):
            groups[f"symbol_{symbol_id}"] = [candidate for candidate in candidates if candidate.symbol_id == symbol_id]
    result = {name: {} for name in FEATURES}
    for group_name, rows in groups.items():
        for label, (attribute, valid_attribute) in FEATURES.items():
            valid_values = [getattr(row.feature, attribute) for row in rows if valid_attribute is None or getattr(row.feature, valid_attribute)]
            invalid_count = len(rows) - len(valid_values) if valid_attribute is not None else 0
            stats = distribution(valid_values)
            stats["valid_count"] = len(valid_values)
            stats["invalid_count"] = invalid_count
            result[label][group_name] = stats
    result["signal_score"] = {group: distribution([row.score for row in rows]) | {"valid_count": len(rows), "invalid_count": 0} for group, rows in groups.items()}
    return result


def candidate_summary(candidates, *, timezone_name="America/New_York") -> dict:
    from zoneinfo import ZoneInfo

    action_names = {0: "SIGNAL_NONE", 1: "LONG_CANDIDATE", 2: "SHORT_CANDIDATE"}
    action_counts = Counter(action_names.get(row.action, f"ACTION_{row.action}") for row in candidates)
    by_symbol = Counter(str(row.symbol_id) for row in candidates if row.action != 0)
    by_score = Counter(str(row.score) for row in candidates if row.action != 0)
    by_reason = Counter(str(row.reason_bits) for row in candidates if row.action != 0)
    by_day = Counter(datetime.fromtimestamp(row.timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(ZoneInfo(timezone_name)).date().isoformat() for row in candidates if row.action != 0)
    invalid = sum(bool(row.reason_bits & 0x0100) for row in candidates)
    disabled = sum(bool(row.reason_bits & (0x0200 | 0x0400 | 0x0800 | 0x1000)) for row in candidates)
    cooldown = sum(bool(row.reason_bits & 0x0080) for row in candidates)
    candidate_count = sum(row.action != 0 for row in candidates)
    span_ns = max((row.timestamp_ns for row in candidates), default=0) - min((row.timestamp_ns for row in candidates), default=0)
    return {
        "total_feature_records": len(candidates),
        "action_counts": dict(action_counts),
        "candidate_count": candidate_count,
        "candidate_rate_per_minute": candidate_count / (span_ns / 60_000_000_000) if span_ns else 0.0,
        "candidate_rate_per_1000_events": candidate_count / len(candidates) * 1000 if candidates else 0.0,
        "candidate_count_by_symbol": dict(sorted(by_symbol.items())),
        "candidate_count_by_day": dict(sorted(by_day.items())),
        "candidate_count_by_score": dict(sorted(by_score.items(), key=lambda item: int(item[0]))),
        "candidate_count_by_reason_bits": dict(sorted(by_reason.items(), key=lambda item: int(item[0]))),
        "prevention_counts_from_reason_bits": {"invalid_features": invalid, "disabled_state": disabled, "cooldown": cooldown},
    }


def threshold_realism(distributions, strategy_config, candidates=None) -> dict:
    thresholds = {
        "long_momentum": ("momentum_bps_x100", strategy_config.long_min_momentum_bps_x100, "lower"),
        "short_momentum": ("momentum_bps_x100", strategy_config.short_max_momentum_bps_x100, "upper"),
        "long_vwap_delta": ("midpoint_minus_vwap_bps_x100", strategy_config.long_min_vwap_delta_bps_x100, "lower"),
        "short_vwap_delta": ("midpoint_minus_vwap_bps_x100", strategy_config.short_max_vwap_delta_bps_x100, "upper"),
        "long_imbalance": ("imbalance_q15", strategy_config.long_min_imbalance_q15, "lower"),
        "short_imbalance": ("imbalance_q15", strategy_config.short_max_imbalance_q15, "upper"),
        "maximum_spread": ("spread_bps_x100", strategy_config.max_spread_bps_x100, "upper"),
        "minimum_volume": ("rolling_volume", strategy_config.min_rolling_volume, "lower"),
    }
    result = {}
    for name, (feature, threshold, direction) in thresholds.items():
        stats = distributions.get(feature, {}).get("combined", {})
        sample = stats.get("count", 0)
        values = []
        if candidates is not None:
            attribute, valid_attribute = FEATURES[feature]
            values = [getattr(row.feature, attribute) for row in candidates if valid_attribute is None or getattr(row.feature, valid_attribute)]
        percentile_rank = sum(value <= threshold for value in values) / len(values) * 100 if values else _estimated_percentile(stats, threshold)
        category = "unavailable"
        if percentile_rank is not None:
            restrictive = percentile_rank >= 90 if direction == "lower" else percentile_rank <= 10
            permissive = percentile_rank <= 10 if direction == "lower" else percentile_rank >= 90
            category = "very restrictive" if restrictive else "very permissive" if permissive else "moderate"
        result[name] = {"feature": feature, "threshold": threshold, "direction": direction, "sample_count": sample, "percentile_rank": percentile_rank, "category": category, "p1": stats.get("p1"), "median": stats.get("median"), "p99": stats.get("p99")}
    return result


def _estimated_percentile(stats, threshold):
    """Estimate a percentile from the retained deterministic quantiles."""

    points = [(1.0, stats.get("p1")), (5.0, stats.get("p5")), (10.0, stats.get("p10")),
              (25.0, stats.get("p25")), (50.0, stats.get("median")), (75.0, stats.get("p75")),
              (90.0, stats.get("p90")), (95.0, stats.get("p95")), (99.0, stats.get("p99"))]
    points = [(rank, value) for rank, value in points if value is not None]
    if not points:
        return None
    if threshold <= points[0][1]:
        return points[0][0]
    if threshold >= points[-1][1]:
        return points[-1][0]
    for (left_rank, left_value), (right_rank, right_value) in zip(points, points[1:]):
        if threshold <= right_value:
            if right_value == left_value:
                return right_rank
            fraction = (threshold - left_value) / (right_value - left_value)
            return left_rank + fraction * (right_rank - left_rank)
    return points[-1][0]


def suggest_search_ranges(distributions) -> dict:
    ranges = {}
    for feature in ("momentum_bps_x100", "midpoint_minus_vwap_bps_x100", "imbalance_q15", "spread_bps_x100", "rolling_volume"):
        stats = distributions.get(feature, {}).get("combined", {})
        ranges[feature] = {"p10": stats.get("p10"), "p25": stats.get("p25"), "median": stats.get("median"), "p75": stats.get("p75"), "p90": stats.get("p90")}
    ranges["signal_cooldown_events"] = [0, 1, 2, 4, 8, 16]
    return ranges
