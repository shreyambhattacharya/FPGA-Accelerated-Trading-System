#include "fpga_client.hpp"
#include "symbol_registry.hpp"
#include "synthetic_market_data_source.hpp"
#include "trading_engine.hpp"

#if FPGA_TRADER_ENABLE_NETWORKING
#include "alpaca_market_data_source.hpp"
#endif

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace {

struct Options {
    std::string source = "synthetic";
    std::string feed = "iex";
    std::vector<std::string> symbols{"SPY", "QQQ", "NVDA", "AMD"};
    bool simulation = false;
    std::size_t max_events = 20;
    bool max_events_specified = false;
    std::uint64_t max_seconds = 0;
    std::string device = "/dev/spidev0.0";
    std::uint32_t speed_hz = 5'000'000;
};

bool parse_u64(const std::string& value, std::uint64_t& output) {
    if (value.empty()) return false;
    char* end = nullptr;
    const auto parsed = std::strtoull(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != '\0') return false;
    output = parsed;
    return true;
}

bool parse_symbols(const std::string& value, std::vector<std::string>& symbols) {
    symbols.clear();
    std::stringstream stream(value);
    std::string symbol;
    while (std::getline(stream, symbol, ',')) {
        if (symbol.empty()) return false;
        symbols.push_back(symbol);
    }
    return !symbols.empty();
}

bool parse_options(int argc, char** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--sim") {
            options.simulation = true;
        } else if (argument == "--source" && index + 1 < argc) {
            options.source = argv[++index];
            if (options.source != "synthetic" && options.source != "alpaca") return false;
        } else if (argument == "--feed" && index + 1 < argc) {
            options.feed = argv[++index];
        } else if (argument == "--symbols" && index + 1 < argc) {
            if (!parse_symbols(argv[++index], options.symbols)) return false;
        } else if ((argument == "--events" || argument == "--max-events") && index + 1 < argc) {
            std::uint64_t count = 0;
            if (!parse_u64(argv[++index], count) || count > std::numeric_limits<std::size_t>::max()) return false;
            options.max_events = static_cast<std::size_t>(count);
            options.max_events_specified = true;
        } else if (argument == "--max-seconds" && index + 1 < argc) {
            if (!parse_u64(argv[++index], options.max_seconds)) return false;
        } else if (argument == "--device" && index + 1 < argc) {
            options.device = argv[++index];
        } else if (argument == "--speed" && index + 1 < argc) {
            std::uint64_t speed = 0;
            if (!parse_u64(argv[++index], speed) || speed == 0 || speed > std::numeric_limits<std::uint32_t>::max()) return false;
            options.speed_hz = static_cast<std::uint32_t>(speed);
        } else if (argument == "--help") {
            return false;
        } else {
            return false;
        }
    }
    return true;
}

void usage() {
    std::cout << "Usage: trading_engine [--source synthetic|alpaca] [--sim] "
                 "[--feed iex|sip|delayed_sip|test] [--symbols SPY,QQQ,NVDA,AMD] "
                 "[--events N|--max-events N] [--max-seconds N] "
                 "[--device /dev/spidev0.0] [--speed 5000000]\n";
}

bool configure_symbols(trading::SymbolRegistry& registry, const std::vector<std::string>& names) {
    if (names.empty() || names.size() > registry.capacity()) {
        std::cerr << "symbol configuration failed: configured symbols exceed registry capacity\n";
        return false;
    }
    for (std::uint16_t slot = 0; slot < names.size(); ++slot) {
        std::string error;
        if (!registry.assign_symbol(slot, names[slot], error) || !registry.enable_slot(slot, error)) {
            std::cerr << "symbol configuration failed: " << error << "\n";
            return false;
        }
    }
    return true;
}

void print_latency(const trading::host::LatencySummary& latency) {
    std::cout << "fpga_latency_samples=" << latency.packets_sent << "\n"
              << "fpga_latency_average_us=" << latency.average_us << "\n"
              << "fpga_latency_min_ns=" << latency.minimum_ns << "\n"
              << "fpga_latency_max_ns=" << latency.maximum_ns << "\n";
}

} // namespace

int main(int argc, char** argv) {
    Options options;
    if (!parse_options(argc, argv, options)) {
        usage();
        return 2;
    }

    std::unique_ptr<trading::host::ByteTransport> transport;
    if (options.simulation) {
        transport = std::make_unique<trading::host::SoftwareLoopbackTransport>();
    } else {
        auto physical = std::make_unique<trading::host::SpiTransport>(trading::host::SpiConfig{
            options.device, options.speed_hz, 0, 8});
        std::string error;
        if (!physical->open_device(error)) {
            std::cerr << "Unable to open physical SPI: " << error << "\nUse --sim for a development-PC run.\n";
            return 3;
        }
        transport = std::move(physical);
    }

    trading::SymbolRegistry registry(32);
    if (!configure_symbols(registry, options.symbols)) return 4;
    std::unique_ptr<trading::MarketDataSource> source;
    if (options.source == "synthetic") {
        source = std::make_unique<trading::SyntheticMarketDataSource>(options.max_events);
    } else {
#if FPGA_TRADER_ENABLE_NETWORKING
        trading::AlpacaMarketDataConfig config;
        config.feed = options.feed;
        config.symbols = options.symbols;
        config.max_events = options.max_events_specified ? options.max_events : 0;
        config.max_seconds = std::chrono::seconds(options.max_seconds);
        source = std::make_unique<trading::AlpacaMarketDataSource>(
            std::move(config), registry, trading::make_beast_websocket_session());
#else
        std::cerr << "Alpaca source requested, but networking is disabled. Reconfigure with "
                     "-DFPGA_TRADER_ENABLE_NETWORKING=ON and install the documented dependencies.\n";
        return 5;
#endif
    }

    trading::SequenceManager sequences(32);
    trading::FpgaClient fpga(*transport);
    trading::TradingEngine engine(*source, registry, fpga, sequences,
                                  trading::TradingEngineConfig{
                                      registry.capacity(),
                                      options.simulation ? trading::TransportMode::Simulation : trading::TransportMode::RealHardware,
                                      options.device, options.speed_hz});
    const auto stats = engine.run();
    const auto latency = engine.latency_summary();
    const auto& source_stats = stats.market_data;

    std::cout << "mode=" << (options.simulation ? "SIMULATION" : "REAL_HARDWARE") << "\n"
              << "market_data_source=" << (source_stats.source.empty() ? options.source : source_stats.source) << "\n"
              << "market_feed=" << (source_stats.feed.empty() ? "NONE" : source_stats.feed) << "\n"
              << "fpga_transport=" << transport->description() << "\n"
              << "symbols=" << options.symbols.size() << "\n"
              << "source_frames=" << source_stats.websocket_frames_received << "\n"
              << "source_quotes=" << source_stats.quote_messages << "\n"
              << "source_trades=" << source_stats.trade_messages << "\n"
              << "events_received=" << stats.events_received << "\n"
              << "events_sent_to_fpga=" << stats.events_sent_to_fpga << "\n"
              << "events_rejected=" << stats.events_rejected << "\n"
              << "unknown_symbols=" << stats.unknown_symbols << "\n"
              << "malformed_messages=" << source_stats.malformed_messages << "\n"
              << "reconnects=" << source_stats.reconnect_count << "\n"
              << "transport_failures=" << stats.transport_failures << "\n"
              << "sequence_assignments=" << stats.sequence_assignments << "\n"
              << "queue_high_watermark=" << source_stats.queue_high_watermark << "\n"
              << "queue_overflow=" << source_stats.queue_overflow << "\n"
              << "candidate_result_egress=NOT_IMPLEMENTED\n";
    print_latency(latency);
    if (source_stats.provider_timestamp_age_samples != 0) {
        std::cout << "provider_timestamp_age_samples=" << source_stats.provider_timestamp_age_samples << "\n"
                  << "provider_timestamp_age_min_ns=" << source_stats.provider_timestamp_age_min_ns << "\n"
                  << "provider_timestamp_age_max_ns=" << source_stats.provider_timestamp_age_max_ns << "\n";
    }
    if (!stats.last_error.empty()) std::cout << "error=" << stats.last_error << "\n";
    return stats.last_error.empty() && stats.events_rejected == 0 ? 0 : 1;
}
