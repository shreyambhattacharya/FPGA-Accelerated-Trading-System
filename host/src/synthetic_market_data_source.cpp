#include "synthetic_market_data_source.hpp"

namespace trading {

SyntheticMarketDataSource::SyntheticMarketDataSource(std::size_t event_count)
    : event_count_(event_count) {}

bool SyntheticMarketDataSource::next(MarketEvent& event) {
    if (index_ >= event_count_) return false;
    const auto cycle = index_ / 5;
    const auto phase = index_ % 5;
    const auto symbol = static_cast<std::uint16_t>(cycle % 4);
    const auto timestamp = 1'700'000'000'000'000'000ULL + cycle * 1'000'000ULL + phase * 100'000ULL;
    const auto base = 100'000'000ULL + static_cast<std::uint64_t>(symbol) * 1'000'000ULL + cycle * 1'000ULL;
    switch (phase) {
    case 0:
        event = MarketEvent::quote(symbol, timestamp, base, 100 + static_cast<std::uint32_t>(cycle), kBidSide);
        break;
    case 1:
        event = MarketEvent::quote(symbol, timestamp, base + 10'000ULL, 110 + static_cast<std::uint32_t>(cycle), kAskSide);
        break;
    case 2:
        event = MarketEvent::trade(symbol, timestamp, base + 5'000ULL, 5 + static_cast<std::uint32_t>(cycle));
        break;
    case 3:
        event = MarketEvent::quote(symbol, timestamp, base + 1'000ULL, 120 + static_cast<std::uint32_t>(cycle), kBidSide);
        break;
    default:
        event = MarketEvent::quote(symbol, timestamp, base + 11'000ULL, 115 + static_cast<std::uint32_t>(cycle), kAskSide);
        break;
    }
    ++index_;
    return true;
}

} // namespace trading
