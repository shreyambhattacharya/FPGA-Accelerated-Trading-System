#pragma once

#include "market_data_source.hpp"
#include "symbol_registry.hpp"

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace trading {

// Alpaca uses short message markers in its stock-data WebSocket protocol.
enum class AlpacaMessageKind {
    Connected,
    Authenticated,
    Subscription,
    Quote,
    Trade,
    Error,
    Ignored,
    Malformed,
};

struct AlpacaMessage {
    AlpacaMessageKind kind = AlpacaMessageKind::Ignored;
    std::string symbol;
    std::uint64_t provider_timestamp_ns = 0;
    std::uint64_t receive_system_ns = 0;
    std::vector<MarketEvent> events;
    int error_code = 0;
    std::string error_text;
};

struct AlpacaParseResult {
    std::vector<AlpacaMessage> messages;
    std::uint64_t malformed_records = 0;
};

// Parses and normalizes one complete WebSocket text frame. A frame can hold
// either one object or an array of objects; all objects are returned in input
// order. The implementation is compiled only with the optional networking
// target, which requires nlohmann/json.
class AlpacaMessageParser {
public:
    explicit AlpacaMessageParser(const SymbolRegistry& symbols) : symbols_(symbols) {}

    AlpacaParseResult parse_frame(std::string_view frame,
                                  std::uint64_t receive_system_ns,
                                  MarketDataTelemetry& telemetry) const;

    static std::uint64_t parse_timestamp_ns(std::string_view timestamp);
    static std::uint32_t parse_quantity(std::string_view integer_text,
                                        std::uint64_t multiplier = 1);

private:
    const SymbolRegistry& symbols_;
};

const char* alpaca_message_kind_name(AlpacaMessageKind kind);

} // namespace trading
