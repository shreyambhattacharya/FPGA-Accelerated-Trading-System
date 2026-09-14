#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>

namespace trading {

constexpr std::uint8_t kBidSide = 0;
constexpr std::uint8_t kAskSide = 1;

enum class MarketEventType : std::uint8_t {
    Quote = 0x01,
    Trade = 0x02,
};

struct MarketEvent {
    MarketEventType type = MarketEventType::Trade;
    std::uint16_t symbol_id = 0;
    std::uint64_t timestamp_ns = 0;
    std::uint64_t price_microdollars = 0;
    std::uint32_t quantity = 0;
    std::uint8_t side = kBidSide;
    std::uint32_t sequence = 0;
    std::uint16_t flags = 0;

    static MarketEvent quote(std::uint16_t symbol_id,
                             std::uint64_t timestamp_ns,
                             std::uint64_t price_microdollars,
                             std::uint32_t quantity,
                             std::uint8_t side,
                             std::uint16_t flags = 0);
    static MarketEvent trade(std::uint16_t symbol_id,
                             std::uint64_t timestamp_ns,
                             std::uint64_t price_microdollars,
                             std::uint32_t quantity,
                             std::uint8_t side = kBidSide,
                             std::uint16_t flags = 0);
};

struct CompleteQuote {
    std::uint16_t symbol_id = 0;
    std::uint64_t timestamp_ns = 0;
    std::uint64_t bid_price_microdollars = 0;
    std::uint64_t ask_price_microdollars = 0;
    std::uint32_t bid_quantity = 0;
    std::uint32_t ask_quantity = 0;
    std::uint16_t flags = 0;
};

std::array<MarketEvent, 2> expand_complete_quote(const CompleteQuote& quote,
                                                  std::uint32_t bid_sequence,
                                                  std::uint32_t ask_sequence);

// Decimal parsing is integer-only. It accepts at most six fractional digits
// because the FPGA-facing representation is USD * 1,000,000.
std::uint64_t decimal_to_microdollars(std::string_view decimal);

} // namespace trading
