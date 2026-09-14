#include "alpaca_market_data_source.hpp"
#include "alpaca_message_parser.hpp"
#include "fpga_client.hpp"
#include "sequence_manager.hpp"
#include "spi_transport.hpp"
#include "trading_engine.hpp"

#include <cassert>
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

class ScriptedSession final : public trading::WebSocketSession {
public:
    explicit ScriptedSession(std::vector<std::string> frames) : frames_(std::move(frames)) {}

    bool connect(const std::string& host, const std::string& target,
                 const trading::WebSocketTimeouts&, std::string&) override {
        connected_ = host == "stream.data.alpaca.markets" && target == "/v2/iex";
        return connected_;
    }

    bool send_text(const std::string& text, std::string&) override {
        sent_.push_back(text);
        return connected_;
    }

    bool receive_text(std::string& text, const trading::WebSocketTimeouts&,
                      bool& timed_out, std::string& error) override {
        timed_out = false;
        if (!connected_) {
            error = "not connected";
            return false;
        }
        if (next_frame_ >= frames_.size()) {
            error = "script exhausted";
            return false;
        }
        text = frames_[next_frame_++];
        return true;
    }

    bool requires_credentials() const override { return false; }

    void close() override { connected_ = false; }

    const std::vector<std::string>& sent() const { return sent_; }

private:
    std::vector<std::string> frames_;
    std::vector<std::string> sent_;
    std::size_t next_frame_ = 0;
    bool connected_ = false;
};

trading::SymbolRegistry one_symbol_registry() {
    trading::SymbolRegistry registry(32);
    std::string error;
    assert(registry.assign_symbol(0, "SPY", error));
    assert(registry.enable_slot(0, error));
    return registry;
}

void test_timestamp_and_parser() {
    assert(trading::AlpacaMessageParser::parse_timestamp_ns("2024-01-01T00:00:00Z") == 1'704'067'200'000'000'000ULL);
    assert(trading::AlpacaMessageParser::parse_timestamp_ns("2024-01-01T00:00:00.123456789Z") == 1'704'067'200'123'456'789ULL);

    auto registry = one_symbol_registry();
    trading::AlpacaMessageParser parser(registry);
    trading::MarketDataTelemetry telemetry;
    const auto result = parser.parse_frame(
        "[{\"T\":\"success\",\"msg\":\"connected\"},"
        "{\"T\":\"success\",\"msg\":\"authenticated\"},"
        "{\"T\":\"subscription\",\"quotes\":[\"SPY\"],\"trades\":[\"SPY\"]},"
        "{\"T\":\"q\",\"S\":\"SPY\",\"bp\":91.95,\"bs\":3,\"ap\":91.950001,\"as\":4,\"t\":\"2024-01-01T00:00:00.123456789Z\"},"
        "{\"T\":\"t\",\"S\":\"SPY\",\"p\":123.456789,\"s\":7,\"t\":\"2024-01-01T00:00:01Z\"}]",
        1'704'067'201'000'000'000ULL, telemetry);
    assert(result.messages.size() == 5);
    assert(result.messages[3].events.size() == 2);
    assert(result.messages[3].events[0].side == trading::kBidSide);
    assert(result.messages[3].events[1].side == trading::kAskSide);
    assert(result.messages[3].events[0].price_microdollars == 91'950'000);
    assert(result.messages[3].events[0].quantity == 300);
    assert(result.messages[3].events[1].quantity == 400);
    assert(result.messages[4].events.size() == 1);
    assert(result.messages[4].events[0].price_microdollars == 123'456'789);
    assert(result.messages[4].events[0].quantity == 7);
    assert(result.messages[4].events[0].side == trading::kBidSide);
    assert(telemetry.quote_messages == 1 && telemetry.trade_messages == 1);
    assert(telemetry.events_normalized == 3);

    const auto unknown = parser.parse_frame(
        "{\"T\":\"q\",\"S\":\"UNKNOWN\",\"bp\":1.00,\"bs\":1,\"ap\":1.01,\"as\":1,\"t\":\"2024-01-01T00:00:00Z\"}",
        1, telemetry);
    assert(unknown.messages.size() == 1 && unknown.messages[0].events.empty());
    assert(telemetry.unknown_symbols == 1);

    const auto malformed = parser.parse_frame("{\"T\":\"q\",\"S\":\"SPY\"}", 1, telemetry);
    assert(malformed.malformed_records == 1);
    assert(telemetry.malformed_messages >= 1);
}

void test_source_queue_and_lifecycle() {
    auto registry = one_symbol_registry();
    auto session = std::make_unique<ScriptedSession>(std::vector<std::string>{
        "[{\"T\":\"success\",\"msg\":\"connected\"},{\"T\":\"success\",\"msg\":\"authenticated\"}]",
        "[{\"T\":\"subscription\",\"quotes\":[\"SPY\"],\"trades\":[\"SPY\"]},{\"T\":\"q\",\"S\":\"SPY\",\"bp\":1.00,\"bs\":1,\"ap\":1.01,\"as\":2,\"t\":\"2024-01-01T00:00:00Z\"},{\"T\":\"t\",\"S\":\"SPY\",\"p\":1.005,\"s\":5,\"t\":\"2024-01-01T00:00:01Z\"}]"});
    auto* session_view = session.get();
    trading::AlpacaMarketDataConfig config;
    config.symbols = {"SPY"};
    config.max_events = 3;
    config.queue_capacity = 3;
    config.timeouts.authentication = std::chrono::milliseconds(100);
    config.timeouts.subscription = std::chrono::milliseconds(100);
    trading::AlpacaMarketDataSource source(std::move(config), registry, std::move(session));
    // The fixture source authenticates/subscribes without contacting the
    // Internet. Credentials are not needed by this injected session.
    trading::MarketEvent first, second, third;
    assert(source.next(first));
    assert(source.next(second));
    assert(source.next(third));
    assert(first.side == trading::kBidSide && second.side == trading::kAskSide);
    assert(first.timestamp_ns == second.timestamp_ns);
    assert(third.type == trading::MarketEventType::Trade);
    assert(first.sequence == 0 && second.sequence == 0 && third.sequence == 0);
    const auto stats = source.telemetry();
    assert(stats.websocket_frames_received == 2);
    assert(stats.queue_high_watermark == 3);
    assert(session_view->sent().size() == 2);
    assert(session_view->sent()[0].find("\"action\":\"auth\"") != std::string::npos);
    assert(session_view->sent()[1].find("\"action\":\"subscribe\"") != std::string::npos);
    assert(trading::AlpacaMarketDataSource::reconnect_delay(1) == std::chrono::seconds(1));
    assert(trading::AlpacaMarketDataSource::reconnect_delay(2) == std::chrono::seconds(2));
    assert(trading::AlpacaMarketDataSource::reconnect_delay(5) == std::chrono::seconds(15));
    source.request_stop();
    assert(source.state() == trading::AlpacaConnectionState::Stopped);
}

void test_packet_compatibility() {
    auto event = trading::MarketEvent::quote(0, 1'704'067'200'123'456'789ULL, 91'950'000, 300, trading::kBidSide);
    event.sequence = 0;
    const auto packet = trading::FpgaClient::packet_for(event).serialize();
    const trading::protocol::PacketBytes expected{
        0xA1, 0x01, 0x00, 0x00, 0x17, 0xA6, 0x10, 0x17,
        0x08, 0xC0, 0xCD, 0x15, 0x00, 0x00, 0x00, 0x00,
        0x05, 0x7B, 0x0B, 0xB0, 0x00, 0x00, 0x01, 0x2C,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x78};
    assert(packet == expected);
    assert(packet[31] == trading::protocol::crc8(packet));
}

} // namespace

int main() {
    test_timestamp_and_parser();
    test_source_queue_and_lifecycle();
    test_packet_compatibility();
    std::cout << "alpaca_networking_test: PASS\n";
    return 0;
}
