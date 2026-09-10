#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace trading::host {

struct LatencySample {
    std::uint32_t sequence = 0;
    std::uint64_t send_ns = 0;
    std::uint64_t receive_ns = 0;
    bool success = false;
    std::string failure;
};

struct LatencySummary {
    std::uint64_t packets_sent = 0;
    std::uint64_t packets_returned = 0;
    std::uint64_t failures = 0;
    std::uint64_t missing_packets = 0;
    std::uint64_t corrupted_packets = 0;
    std::uint64_t other_failures = 0;
    double average_us = 0.0;
    std::uint64_t minimum_ns = 0;
    std::uint64_t maximum_ns = 0;
    std::uint64_t p50_ns = 0;
    std::uint64_t p95_ns = 0;
    std::uint64_t p99_ns = 0;
    std::uint64_t sequence_errors = 0;
};

class LatencyTracker {
public:
    void record(LatencySample sample);
    void record_sequence_error();
    LatencySummary summary() const;
    const std::vector<LatencySample>& samples() const { return samples_; }

private:
    std::vector<LatencySample> samples_;
    std::uint64_t sequence_errors_ = 0;
};

std::uint64_t steady_time_ns();

} // namespace trading::host
