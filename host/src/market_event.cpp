#include "market_event.hpp"

#include <cctype>
#include <limits>
#include <stdexcept>

namespace trading {

MarketEvent MarketEvent::quote(std::uint16_t symbol_id_value,
                               std::uint64_t timestamp_ns_value,
                               std::uint64_t price_microdollars_value,
                               std::uint32_t quantity_value,
                               std::uint8_t side_value,
                               std::uint16_t flags_value) {
    if (side_value != kBidSide && side_value != kAskSide) {
        throw std::invalid_argument("quote side must be 0 (bid) or 1 (ask)");
    }
    return {MarketEventType::Quote, symbol_id_value, timestamp_ns_value,
            price_microdollars_value, quantity_value, side_value, 0, flags_value};
}

MarketEvent MarketEvent::trade(std::uint16_t symbol_id_value,
                               std::uint64_t timestamp_ns_value,
                               std::uint64_t price_microdollars_value,
                               std::uint32_t quantity_value,
                               std::uint8_t side_value,
                               std::uint16_t flags_value) {
    if (side_value != kBidSide && side_value != kAskSide) {
        throw std::invalid_argument("trade side must be 0 or 1");
    }
    return {MarketEventType::Trade, symbol_id_value, timestamp_ns_value,
            price_microdollars_value, quantity_value, side_value, 0, flags_value};
}

std::array<MarketEvent, 2> expand_complete_quote(const CompleteQuote& quote,
                                                  std::uint32_t bid_sequence,
                                                  std::uint32_t ask_sequence) {
    auto bid = MarketEvent::quote(quote.symbol_id, quote.timestamp_ns,
                                  quote.bid_price_microdollars, quote.bid_quantity,
                                  kBidSide, quote.flags);
    auto ask = MarketEvent::quote(quote.symbol_id, quote.timestamp_ns,
                                  quote.ask_price_microdollars, quote.ask_quantity,
                                  kAskSide, quote.flags);
    bid.sequence = bid_sequence;
    ask.sequence = ask_sequence;
    return {bid, ask};
}

std::uint64_t decimal_to_microdollars(std::string_view decimal) {
    if (decimal.empty() || decimal.front() == '-') {
        throw std::invalid_argument("price must be a non-negative decimal");
    }
    std::size_t index = 0;
    if (decimal.front() == '+') {
        index = 1;
    }
    if (index == decimal.size()) {
        throw std::invalid_argument("price has no digits");
    }

    const auto max_value = std::numeric_limits<std::uint64_t>::max();
    std::uint64_t whole = 0;
    std::size_t whole_digits = 0;
    while (index < decimal.size() && decimal[index] != '.') {
        const auto character = static_cast<unsigned char>(decimal[index]);
        if (!std::isdigit(character)) {
            throw std::invalid_argument("price contains a non-decimal character");
        }
        const auto digit = static_cast<std::uint64_t>(character - '0');
        if (whole > (max_value - digit) / 10) {
            throw std::out_of_range("price overflows micro-dollar representation");
        }
        whole = whole * 10 + digit;
        ++whole_digits;
        ++index;
    }
    if (whole_digits == 0) {
        throw std::invalid_argument("price requires digits before the decimal point");
    }

    std::uint64_t fraction = 0;
    std::size_t fraction_digits = 0;
    if (index < decimal.size()) {
        ++index;
        while (index < decimal.size()) {
            const auto character = static_cast<unsigned char>(decimal[index]);
            if (!std::isdigit(character) || fraction_digits == 6) {
                throw std::invalid_argument("price must contain at most six fractional digits");
            }
            fraction = fraction * 10 + static_cast<std::uint64_t>(character - '0');
            ++fraction_digits;
            ++index;
        }
    }
    while (fraction_digits < 6) {
        fraction *= 10;
        ++fraction_digits;
    }
    if (whole > (max_value - fraction) / 1'000'000ULL) {
        throw std::out_of_range("price overflows micro-dollar representation");
    }
    return whole * 1'000'000ULL + fraction;
}

} // namespace trading
