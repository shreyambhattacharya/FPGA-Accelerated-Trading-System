#include "fpga_client.hpp"
#include "market_event.hpp"
#include "market_data_source.hpp"
#include "sequence_manager.hpp"
#include "spi_transport.hpp"
#include "symbol_registry.hpp"
#include "synthetic_market_data_source.hpp"
#include "trading_engine.hpp"

#include <cassert>
#include <iostream>
#include <limits>
#include <string>
#include <stdexcept>

namespace {

class OneEventSource final : public trading::MarketDataSource {
public:
    explicit OneEventSource(trading::MarketEvent event) : event_(event) {}
    bool next(trading::MarketEvent& event) override {
        if (used_) return false;
        used_ = true;
        event = event_;
        return true;
    }
private:
    trading::MarketEvent event_;
    bool used_ = false;
};

class FailingTransport final : public trading::host::ByteTransport {
public:
    trading::host::TransferResult transfer(const trading::protocol::PacketBytes&) override {
        return {false, {}, "intentional transport failure"};
    }
    std::string description() const override { return "failing-test-transport"; }
};

trading::SymbolRegistry default_registry() {
    trading::SymbolRegistry registry(32);
    const char* symbols[] = {"SPY", "QQQ", "NVDA", "AMD"};
    for (std::uint16_t slot = 0; slot < 4; ++slot) {
        std::string error;
        assert(registry.assign_symbol(slot, symbols[slot], error));
        assert(registry.enable_slot(slot, error));
    }
    return registry;
}

void test_prices_and_quotes() {
    assert(trading::decimal_to_microdollars("0") == 0);
    assert(trading::decimal_to_microdollars("1") == 1'000'000);
    assert(trading::decimal_to_microdollars("1.000001") == 1'000'001);
    assert(trading::decimal_to_microdollars("123.456789") == 123'456'789);
    assert(trading::decimal_to_microdollars("18446744073709.551615") == std::numeric_limits<std::uint64_t>::max());
    bool failed = false;
    try { (void)trading::decimal_to_microdollars("1.0000001"); } catch (const std::invalid_argument&) { failed = true; }
    assert(failed);
    failed = false;
    try { (void)trading::decimal_to_microdollars("-1"); } catch (const std::invalid_argument&) { failed = true; }
    assert(failed);
    failed = false;
    try { (void)trading::decimal_to_microdollars("18446744073709551616"); } catch (const std::out_of_range&) { failed = true; }
    assert(failed);

    const auto expanded = trading::expand_complete_quote({0, 10, 100, 102, 7, 9, 0}, 4, 5);
    assert(expanded[0].side == trading::kBidSide && expanded[1].side == trading::kAskSide);
    assert(expanded[0].sequence == 4 && expanded[1].sequence == 5);
}

void test_registry_and_remap() {
    auto registry = default_registry();
    assert(registry.lookup_symbol("QQQ") == 1);
    assert(registry.lookup_slot(2).value() == "NVDA");
    std::string error;
    assert(!registry.assign_symbol(4, "SPY", error));
    assert(!registry.assign_symbol(4, "QQQ", error));
    assert(registry.begin_remap(1, error));
    assert(!registry.enable_slot(1, error));
    assert(registry.acknowledge_clear(1, error));
    assert(registry.complete_remap(1, "MSFT", error));
    assert(registry.lookup_symbol("MSFT").value() == 1);
    assert(registry.is_enabled(1));
    assert(!registry.assign_symbol(3, "MSFT", error));
    assert(registry.disable_slot(3, error));
    assert(registry.clear_slot(3, error));
}

void test_sequences() {
    trading::SequenceManager sequences(4);
    assert(sequences.next(0) == 0);
    assert(sequences.next(0) == 1);
    sequences.reset(0);
    assert(sequences.peek(0) == 0);
    sequences.set_next(0, std::numeric_limits<std::uint32_t>::max());
    bool failed = false;
    try { (void)sequences.next(0); } catch (const trading::SequenceOverflow&) { failed = true; }
    assert(failed);
}

void test_client_and_simulation() {
    trading::host::SoftwareLoopbackTransport transport;
    trading::FpgaClient client(transport);
    auto event = trading::MarketEvent::quote(0, 10, 100'000'000, 10, trading::kBidSide);
    event.sequence = 9;
    const auto packet = trading::FpgaClient::packet_for(event);
    assert(packet.message_type == trading::protocol::MessageType::MarketQuote);
    assert(packet.serialize()[31] == trading::protocol::crc8(packet.serialize()));
    assert(client.send_market_event(event).success);
    const auto control = trading::FpgaClient::control_packet(0, 0x1B, 0, 0, 10);
    assert(client.send_control(control).success);
}

void test_engine_counts_and_unknowns() {
    auto registry = default_registry();
    trading::host::SoftwareLoopbackTransport transport;
    trading::FpgaClient client(transport);
    trading::SequenceManager sequences(32);
    trading::SyntheticMarketDataSource source(20);
    trading::TradingEngine engine(source, registry, client, sequences);
    const auto stats = engine.run();
    assert(stats.events_received == 20);
    assert(stats.events_sent_to_fpga == 20);
    assert(stats.events_rejected == 0);
    assert(stats.unknown_symbols == 0);
    assert(stats.sequence_assignments == 20);

    OneEventSource unknown(trading::MarketEvent::trade(31, 20, 100, 1));
    trading::TradingEngine unknown_engine(unknown, registry, client, sequences);
    const auto unknown_stats = unknown_engine.run();
    assert(unknown_stats.events_received == 1);
    assert(unknown_stats.unknown_symbols == 1);
    assert(unknown_stats.events_sent_to_fpga == 0);
}

void test_transport_failure_propagation() {
    auto registry = default_registry();
    FailingTransport transport;
    trading::FpgaClient client(transport);
    trading::SequenceManager sequences(32);
    OneEventSource source(trading::MarketEvent::trade(0, 20, 100, 1));
    trading::TradingEngine engine(source, registry, client, sequences);
    const auto stats = engine.run();
    assert(stats.transport_failures == 1);
    assert(stats.events_sent_to_fpga == 0);
    assert(stats.last_error == "intentional transport failure");
}

} // namespace

int main() {
    test_prices_and_quotes();
    test_registry_and_remap();
    test_sequences();
    test_client_and_simulation();
    test_engine_counts_and_unknowns();
    test_transport_failure_propagation();
    std::cout << "host_runtime_test: PASS\n";
    return 0;
}
