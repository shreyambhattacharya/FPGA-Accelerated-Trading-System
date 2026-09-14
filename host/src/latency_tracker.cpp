#include "latency_tracker.hpp"

#include <algorithm>
#include <chrono>
#include <numeric>
#include <utility>

namespace trading::host {

std::uint64_t steady_time_ns() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
}

std::uint64_t system_time_ns() {
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
}

void LatencyTracker::record(LatencySample sample) {
    samples_.push_back(std::move(sample));
}

void LatencyTracker::record_sequence_error() {
    ++sequence_errors_;
}

LatencySummary LatencyTracker::summary() const {
    LatencySummary result;
    result.packets_sent = samples_.size();
    std::vector<std::uint64_t> durations;
    durations.reserve(samples_.size());
    for (const auto& sample : samples_) {
        if (sample.success && sample.receive_ns >= sample.send_ns) {
            durations.push_back(sample.receive_ns - sample.send_ns);
        }
    }
    result.packets_returned = durations.size();
    result.failures = result.packets_sent - result.packets_returned;
    result.sequence_errors = sequence_errors_;
    if (!samples_.empty() && samples_.back().receive_ns >= samples_.front().send_ns) {
        result.elapsed_ns = samples_.back().receive_ns - samples_.front().send_ns;
        if (result.elapsed_ns != 0) {
            result.effective_packets_per_second =
                static_cast<double>(result.packets_returned) * 1'000'000'000.0 /
                static_cast<double>(result.elapsed_ns);
        }
    }
    for (const auto& sample : samples_) {
        if (sample.success) continue;
        if (sample.failure.find("missing") != std::string::npos) {
            ++result.missing_packets;
        } else if (sample.failure.find("response") != std::string::npos ||
                   sample.failure.find("checksum") != std::string::npos) {
            ++result.corrupted_packets;
        } else {
            ++result.other_failures;
        }
    }
    if (durations.empty()) return result;

    std::sort(durations.begin(), durations.end());
    result.minimum_ns = durations.front();
    result.maximum_ns = durations.back();
    const auto sum = std::accumulate(durations.begin(), durations.end(), std::uint64_t{0});
    result.average_us = static_cast<double>(sum) / static_cast<double>(durations.size()) / 1000.0;

    const auto percentile = [&durations](double p) {
        const auto index = static_cast<std::size_t>(p * static_cast<double>(durations.size() - 1));
        return durations[index];
    };
    result.p50_ns = percentile(0.50);
    result.p95_ns = percentile(0.95);
    result.p99_ns = percentile(0.99);
    return result;
}

} // namespace trading::host
