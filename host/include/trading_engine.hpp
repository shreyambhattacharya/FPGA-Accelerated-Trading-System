#pragma once

#include "fpga_client.hpp"
#include "latency_tracker.hpp"
#include "market_data_source.hpp"
#include "sequence_manager.hpp"
#include "spi_transport.hpp"
#include "symbol_registry.hpp"

#include <cstdint>
#include <string>

namespace trading {

enum class TransportMode {
    Simulation,
    RealHardware,
};

struct TradingEngineConfig {
    std::uint16_t num_symbols = 4;
    TransportMode transport_mode = TransportMode::Simulation;
    std::string spi_device = "/dev/spidev0.0";
    std::uint32_t spi_speed_hz = 5'000'000;
};

struct EngineStats {
    std::uint64_t events_received = 0;
    std::uint64_t events_rejected = 0;
    std::uint64_t events_sent_to_fpga = 0;
    std::uint64_t transport_failures = 0;
    std::uint64_t unknown_symbols = 0;
    std::uint64_t sequence_assignments = 0;
    std::uint64_t start_timestamp_ns = 0;
    std::uint64_t last_event_timestamp_ns = 0;
    std::string last_error;
};

class TradingEngine {
public:
    TradingEngine(MarketDataSource& source,
                  SymbolRegistry& symbols,
                  FpgaClient& fpga,
                  SequenceManager& sequences,
                  TradingEngineConfig config = {});

    EngineStats run();
    host::LatencySummary latency_summary() const { return latency_tracker_.summary(); }

private:
    MarketDataSource& source_;
    SymbolRegistry& symbols_;
    FpgaClient& fpga_;
    SequenceManager& sequences_;
    TradingEngineConfig config_;
    host::LatencyTracker latency_tracker_;
};

} // namespace trading
