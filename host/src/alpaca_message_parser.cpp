#include "alpaca_message_parser.hpp"

#include "latency_tracker.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>

namespace trading {
namespace {

using Json = nlohmann::json;

// nlohmann/json validates and interprets the object. This small lexeme
// recovery helper is only used for decimal price/quantity fields so a binary
// floating-point round trip cannot change an FPGA-facing micro-dollar value.
std::string raw_value_for_key(std::string_view object, std::string_view key) {
    const std::string needle = "\"" + std::string(key) + "\"";
    std::size_t position = object.find(needle);
    while (position != std::string_view::npos) {
        std::size_t colon = position + needle.size();
        while (colon < object.size() && std::isspace(static_cast<unsigned char>(object[colon]))) ++colon;
        if (colon < object.size() && object[colon] == ':') {
            ++colon;
            while (colon < object.size() && std::isspace(static_cast<unsigned char>(object[colon]))) ++colon;
            if (colon >= object.size()) return {};
            if (object[colon] == '"') {
                ++colon;
                std::string result;
                bool escaped = false;
                for (; colon < object.size(); ++colon) {
                    const char character = object[colon];
                    if (escaped) {
                        result.push_back(character);
                        escaped = false;
                    } else if (character == '\\') {
                        escaped = true;
                    } else if (character == '"') {
                        return result;
                    } else {
                        result.push_back(character);
                    }
                }
                return {};
            }
            const auto begin = colon;
            while (colon < object.size() && object[colon] != ',' && object[colon] != '}' &&
                   !std::isspace(static_cast<unsigned char>(object[colon]))) ++colon;
            return std::string(object.substr(begin, colon - begin));
        }
        position = object.find(needle, position + needle.size());
    }
    return {};
}

std::vector<std::string> record_objects(std::string_view frame) {
    std::vector<std::string> records;
    const auto first = frame.find_first_not_of(" \t\r\n");
    if (first == std::string_view::npos) return records;
    if (frame[first] == '{') {
        records.emplace_back(frame.substr(first));
        return records;
    }
    if (frame[first] != '[') return records;
    std::size_t cursor = first + 1;
    while (cursor < frame.size()) {
        while (cursor < frame.size() && (std::isspace(static_cast<unsigned char>(frame[cursor])) || frame[cursor] == ',')) ++cursor;
        if (cursor >= frame.size() || frame[cursor] == ']') break;
        if (frame[cursor] != '{') return {};
        const auto begin = cursor;
        int depth = 0;
        bool quoted = false;
        bool escaped = false;
        for (; cursor < frame.size(); ++cursor) {
            const char character = frame[cursor];
            if (quoted) {
                if (escaped) escaped = false;
                else if (character == '\\') escaped = true;
                else if (character == '"') quoted = false;
                continue;
            }
            if (character == '"') quoted = true;
            else if (character == '{') ++depth;
            else if (character == '}' && --depth == 0) {
                ++cursor;
                records.emplace_back(frame.substr(begin, cursor - begin));
                break;
            }
        }
        if (depth != 0) return {};
    }
    return records;
}

std::string required_string(const Json& object, const char* field) {
    if (!object.contains(field) || !object[field].is_string()) {
        throw std::invalid_argument(std::string("missing or invalid ") + field);
    }
    return object[field].get<std::string>();
}

void record_age(MarketDataTelemetry& telemetry,
                std::uint64_t provider_timestamp_ns,
                std::uint64_t receive_system_ns) {
    if (provider_timestamp_ns == 0 || receive_system_ns < provider_timestamp_ns) return;
    const auto age = receive_system_ns - provider_timestamp_ns;
    ++telemetry.provider_timestamp_age_samples;
    if (telemetry.provider_timestamp_age_samples == 1) {
        telemetry.provider_timestamp_age_min_ns = age;
        telemetry.provider_timestamp_age_max_ns = age;
    } else {
        telemetry.provider_timestamp_age_min_ns = std::min(telemetry.provider_timestamp_age_min_ns, age);
        telemetry.provider_timestamp_age_max_ns = std::max(telemetry.provider_timestamp_age_max_ns, age);
    }
    telemetry.provider_timestamp_age_sum_ns += static_cast<long double>(age);
}

} // namespace

const char* alpaca_message_kind_name(AlpacaMessageKind kind) {
    switch (kind) {
    case AlpacaMessageKind::Connected: return "connected";
    case AlpacaMessageKind::Authenticated: return "authenticated";
    case AlpacaMessageKind::Subscription: return "subscription";
    case AlpacaMessageKind::Quote: return "quote";
    case AlpacaMessageKind::Trade: return "trade";
    case AlpacaMessageKind::Error: return "error";
    case AlpacaMessageKind::Ignored: return "ignored";
    case AlpacaMessageKind::Malformed: return "malformed";
    }
    return "unknown";
}

std::uint64_t AlpacaMessageParser::parse_timestamp_ns(std::string_view timestamp) {
    // Gregorian days before the civil date, adapted for C++17 without a
    // locale or local-time conversion. The accepted form is UTC RFC3339.
    if (timestamp.size() < 20 || timestamp[4] != '-' || timestamp[7] != '-' ||
        timestamp[10] != 'T' || timestamp[13] != ':' || timestamp[16] != ':') {
        throw std::invalid_argument("timestamp is not RFC3339 UTC");
    }
    const auto digit = [&timestamp](std::size_t position) -> unsigned {
        if (position >= timestamp.size() || !std::isdigit(static_cast<unsigned char>(timestamp[position]))) {
            throw std::invalid_argument("timestamp contains a non-digit");
        }
        return static_cast<unsigned>(timestamp[position] - '0');
    };
    const unsigned year = digit(0) * 1000U + digit(1) * 100U + digit(2) * 10U + digit(3);
    const unsigned month = digit(5) * 10U + digit(6);
    const unsigned day = digit(8) * 10U + digit(9);
    const unsigned hour = digit(11) * 10U + digit(12);
    const unsigned minute = digit(14) * 10U + digit(15);
    const unsigned second = digit(17) * 10U + digit(18);
    if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 60) {
        throw std::invalid_argument("timestamp has an invalid component");
    }
    const bool leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
    const unsigned month_days[] = {31, leap ? 29U : 28U, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (day < 1 || day > month_days[month - 1]) throw std::invalid_argument("timestamp has an invalid day");

    std::size_t cursor = 19;
    std::uint32_t fraction = 0;
    unsigned fraction_digits = 0;
    if (cursor < timestamp.size() && timestamp[cursor] == '.') {
        ++cursor;
        while (cursor < timestamp.size() && std::isdigit(static_cast<unsigned char>(timestamp[cursor]))) {
            if (fraction_digits == 9) throw std::invalid_argument("timestamp has more than nine fractional digits");
            fraction = fraction * 10U + static_cast<std::uint32_t>(timestamp[cursor] - '0');
            ++fraction_digits;
            ++cursor;
        }
        if (fraction_digits == 0) throw std::invalid_argument("timestamp has an empty fraction");
    }
    if (cursor >= timestamp.size() || timestamp[cursor] != 'Z' || cursor + 1 != timestamp.size()) {
        throw std::invalid_argument("timestamp must use UTC Z suffix");
    }
    while (fraction_digits < 9) {
        fraction *= 10U;
        ++fraction_digits;
    }

    const auto days_from_civil = [](int y, unsigned m, unsigned d) -> std::int64_t {
        y -= m <= 2;
        const int era = (y >= 0 ? y : y - 399) / 400;
        const unsigned year_of_era = static_cast<unsigned>(y - era * 400);
        const unsigned day_of_year = (153 * (m + (m > 2 ? -3U : 9U)) + 2) / 5 + d - 1;
        const unsigned day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
        return static_cast<std::int64_t>(era) * 146097 + static_cast<std::int64_t>(day_of_era) - 719468;
    };
    const auto days = days_from_civil(static_cast<int>(year), month, day);
    if (days < 0) throw std::invalid_argument("timestamp predates Unix epoch");
    const auto seconds = static_cast<std::uint64_t>(days) * 86'400ULL + hour * 3'600ULL + minute * 60ULL + second;
    if (seconds > (std::numeric_limits<std::uint64_t>::max() - fraction) / 1'000'000'000ULL) {
        throw std::out_of_range("timestamp overflows nanoseconds");
    }
    return seconds * 1'000'000'000ULL + fraction;
}

std::uint32_t AlpacaMessageParser::parse_quantity(std::string_view integer_text, std::uint64_t multiplier) {
    if (integer_text.empty() || integer_text.front() == '-') throw std::invalid_argument("quantity must be non-negative");
    std::uint64_t value = 0;
    for (const char character : integer_text) {
        if (!std::isdigit(static_cast<unsigned char>(character))) throw std::invalid_argument("quantity must be an integer");
        const auto digit = static_cast<std::uint64_t>(character - '0');
        if (value > (std::numeric_limits<std::uint64_t>::max() - digit) / 10ULL) throw std::out_of_range("quantity overflow");
        value = value * 10ULL + digit;
    }
    if (multiplier != 0 && value > std::numeric_limits<std::uint64_t>::max() / multiplier) throw std::out_of_range("quantity overflow");
    value *= multiplier;
    if (value > std::numeric_limits<std::uint32_t>::max()) throw std::out_of_range("quantity exceeds FPGA width");
    return static_cast<std::uint32_t>(value);
}

AlpacaParseResult AlpacaMessageParser::parse_frame(std::string_view frame,
                                                   std::uint64_t receive_system_ns,
                                                   MarketDataTelemetry& telemetry) const {
    AlpacaParseResult result;
    std::vector<std::string> records;
    try {
        const auto root = Json::parse(frame.begin(), frame.end());
        if (!root.is_object() && !root.is_array()) throw std::invalid_argument("frame root must be object or array");
        records = record_objects(frame);
        if (records.empty()) throw std::invalid_argument("frame contains no message objects");
    } catch (const std::exception& error) {
        ++telemetry.malformed_messages;
        ++result.malformed_records;
        AlpacaMessage malformed;
        malformed.kind = AlpacaMessageKind::Malformed;
        malformed.receive_system_ns = receive_system_ns;
        malformed.error_text = error.what();
        result.messages.push_back(std::move(malformed));
        return result;
    }

    for (const auto& record : records) {
        AlpacaMessage message;
        message.receive_system_ns = receive_system_ns;
        try {
            const auto object = Json::parse(record);
            const auto type = required_string(object, "T");
            if (type == "success") {
                const auto text = required_string(object, "msg");
                if (text == "connected") message.kind = AlpacaMessageKind::Connected;
                else if (text == "authenticated") message.kind = AlpacaMessageKind::Authenticated;
                else message.kind = AlpacaMessageKind::Ignored;
                ++telemetry.status_messages;
            } else if (type == "subscription") {
                message.kind = AlpacaMessageKind::Subscription;
                ++telemetry.subscription_messages;
            } else if (type == "error") {
                message.kind = AlpacaMessageKind::Error;
                if (object.contains("code") && object["code"].is_number_integer()) message.error_code = object["code"].get<int>();
                message.error_text = object.value("msg", std::string("provider error"));
                ++telemetry.status_messages;
            } else if (type == "q") {
                message.kind = AlpacaMessageKind::Quote;
                message.symbol = required_string(object, "S");
                const auto timestamp_text = required_string(object, "t");
                message.provider_timestamp_ns = parse_timestamp_ns(timestamp_text);
                const auto symbol = symbols_.lookup_symbol(message.symbol);
                if (!symbol || !symbols_.is_enabled(*symbol)) {
                    ++telemetry.unknown_symbols;
                } else {
                    const auto bid_price = decimal_to_microdollars(raw_value_for_key(record, "bp"));
                    const auto ask_price = decimal_to_microdollars(raw_value_for_key(record, "ap"));
                    const auto bid_quantity = parse_quantity(raw_value_for_key(record, "bs"), 100);
                    const auto ask_quantity = parse_quantity(raw_value_for_key(record, "as"), 100);
                    const auto expanded = expand_complete_quote({*symbol, message.provider_timestamp_ns,
                                                                  bid_price, ask_price, bid_quantity, ask_quantity, 0}, 0, 0);
                    message.events.assign(expanded.begin(), expanded.end());
                    telemetry.events_normalized += message.events.size();
                }
                ++telemetry.quote_messages;
            } else if (type == "t") {
                message.kind = AlpacaMessageKind::Trade;
                message.symbol = required_string(object, "S");
                const auto timestamp_text = required_string(object, "t");
                message.provider_timestamp_ns = parse_timestamp_ns(timestamp_text);
                const auto symbol = symbols_.lookup_symbol(message.symbol);
                if (!symbol || !symbols_.is_enabled(*symbol)) {
                    ++telemetry.unknown_symbols;
                } else {
                    const auto price = decimal_to_microdollars(raw_value_for_key(record, "p"));
                    const auto quantity = parse_quantity(raw_value_for_key(record, "s"), 1);
                    // Alpaca stock trades do not supply a trustworthy aggressor
                    // side. kBidSide is the existing protocol unknown/default
                    // value; no direction is inferred from quotes.
                    message.events.push_back(MarketEvent::trade(*symbol, message.provider_timestamp_ns, price, quantity));
                    ++telemetry.events_normalized;
                }
                ++telemetry.trade_messages;
            } else {
                message.kind = AlpacaMessageKind::Ignored;
                ++telemetry.ignored_messages;
            }
        } catch (const std::exception& error) {
            message.kind = AlpacaMessageKind::Malformed;
            message.error_text = error.what();
            ++telemetry.malformed_messages;
            ++result.malformed_records;
        }
        ++telemetry.provider_messages_received;
        if (message.provider_timestamp_ns != 0) {
            telemetry.last_provider_timestamp_ns = message.provider_timestamp_ns;
            record_age(telemetry, message.provider_timestamp_ns, receive_system_ns);
        }
        result.messages.push_back(std::move(message));
    }
    return result;
}

} // namespace trading
