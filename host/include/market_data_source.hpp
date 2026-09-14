#pragma once

#include "market_event.hpp"

#include <cstdint>
#include <string>

namespace trading {

struct MarketDataTelemetry {
    std::string source = "UNKNOWN";
    std::string feed;
    std::uint64_t websocket_frames_received = 0;
    std::uint64_t provider_messages_received = 0;
    std::uint64_t quote_messages = 0;
    std::uint64_t trade_messages = 0;
    std::uint64_t status_messages = 0;
    std::uint64_t subscription_messages = 0;
    std::uint64_t ignored_messages = 0;
    std::uint64_t malformed_messages = 0;
    std::uint64_t unknown_symbols = 0;
    std::uint64_t events_normalized = 0;
    std::uint64_t reconnect_count = 0;
    std::uint64_t auth_failures = 0;
    std::uint64_t subscription_failures = 0;
    std::uint64_t queue_high_watermark = 0;
    std::uint64_t queue_overflow = 0;
    std::uint64_t last_provider_timestamp_ns = 0;
    std::uint64_t last_receive_steady_ns = 0;
    std::uint64_t provider_timestamp_age_samples = 0;
    std::uint64_t provider_timestamp_age_min_ns = 0;
    std::uint64_t provider_timestamp_age_max_ns = 0;
    long double provider_timestamp_age_sum_ns = 0.0L;
    std::uint64_t normalization_latency_samples = 0;
    std::uint64_t normalization_latency_min_ns = 0;
    std::uint64_t normalization_latency_max_ns = 0;
    long double normalization_latency_sum_ns = 0.0L;
};

class MarketDataSource {
public:
    virtual ~MarketDataSource() = default;
    virtual bool next(MarketEvent& event) = 0;
    virtual void request_stop() {}
    virtual MarketDataTelemetry telemetry() const { return {}; }
    virtual std::string last_error() const { return {}; }
};

} // namespace trading
