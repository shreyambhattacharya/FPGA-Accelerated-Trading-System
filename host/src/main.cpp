#include "fpga_client.hpp"
#include "symbol_registry.hpp"
#include "synthetic_market_data_source.hpp"
#include "trading_engine.hpp"

#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <string>

namespace {

struct Options {
    bool simulation = false;
    std::size_t events = 20;
    std::string device = "/dev/spidev0.0";
    std::uint32_t speed_hz = 5'000'000;
};

bool parse_u64(const std::string& value, std::uint64_t& output) {
    char* end = nullptr;
    const auto parsed = std::strtoull(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != '\0' || parsed > std::numeric_limits<std::uint64_t>::max()) return false;
    output = parsed;
    return true;
}

bool parse_options(int argc, char** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--sim") {
            options.simulation = true;
        } else if (argument == "--events" && index + 1 < argc) {
            std::uint64_t count = 0;
            if (!parse_u64(argv[++index], count) || count > std::numeric_limits<std::size_t>::max()) return false;
            options.events = static_cast<std::size_t>(count);
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
    std::cout << "Usage: trading_engine --sim [--events N] [--device /dev/spidev0.0] [--speed 5000000]\n";
}

bool configure_default_symbols(trading::SymbolRegistry& registry) {
    const char* names[] = {"SPY", "QQQ", "NVDA", "AMD"};
    for (std::uint16_t slot = 0; slot < 4; ++slot) {
        std::string error;
        if (!registry.assign_symbol(slot, names[slot], error) || !registry.enable_slot(slot, error)) {
            std::cerr << "symbol configuration failed: " << error << "\n";
            return false;
        }
    }
    return true;
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
    if (!configure_default_symbols(registry)) return 4;
    trading::SyntheticMarketDataSource source(options.events);
    trading::SequenceManager sequences(32);
    trading::FpgaClient fpga(*transport);
    trading::TradingEngine engine(source, registry, fpga, sequences,
                                  trading::TradingEngineConfig{4,
                                      options.simulation ? trading::TransportMode::Simulation : trading::TransportMode::RealHardware,
                                      options.device, options.speed_hz});
    const auto stats = engine.run();
    const auto latency = engine.latency_summary();

    std::cout << "mode=" << (options.simulation ? "SIMULATION" : "REAL_HARDWARE") << "\n"
              << "symbols=4\n"
              << "events_received=" << stats.events_received << "\n"
              << "events_rejected=" << stats.events_rejected << "\n"
              << "events_sent_to_fpga=" << stats.events_sent_to_fpga << "\n"
              << "transport_failures=" << stats.transport_failures << "\n"
              << "unknown_symbols=" << stats.unknown_symbols << "\n"
              << "sequence_assignments=" << stats.sequence_assignments << "\n"
              << "latency_samples=" << latency.packets_sent << "\n";
    if (!stats.last_error.empty()) std::cout << "error=" << stats.last_error << "\n";
    return stats.last_error.empty() && stats.events_rejected == 0 ? 0 : 1;
}
