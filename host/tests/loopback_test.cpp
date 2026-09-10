#include "latency_tracker.hpp"
#include "spi_transport.hpp"

#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <string>

namespace {

struct Options {
    bool simulation = false;
    bool verbose = false;
    std::uint32_t count = 100;
    std::string device = "/dev/spidev0.0";
    std::uint32_t speed_hz = 5'000'000;
};

bool parse_u32(const std::string& value, std::uint32_t& output) {
    char* end = nullptr;
    const auto parsed = std::strtoul(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != '\0' || parsed > std::numeric_limits<std::uint32_t>::max()) {
        return false;
    }
    output = static_cast<std::uint32_t>(parsed);
    return true;
}

bool parse_options(int argc, char** argv, Options& options) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--sim") {
            options.simulation = true;
        } else if (arg == "--verbose") {
            options.verbose = true;
        } else if (arg == "--count" && i + 1 < argc) {
            if (!parse_u32(argv[++i], options.count) || options.count == 0) return false;
        } else if (arg == "--device" && i + 1 < argc) {
            options.device = argv[++i];
        } else if (arg == "--speed" && i + 1 < argc) {
            if (!parse_u32(argv[++i], options.speed_hz) || options.speed_hz == 0) return false;
        } else {
            return false;
        }
    }
    return true;
}

void print_usage() {
    std::cout << "Usage: loopback_test [--sim] [--count N] [--device /dev/spidev0.0] "
                 "[--speed HZ] [--verbose]\n";
}

} // namespace

int main(int argc, char** argv) {
    Options options;
    if (!parse_options(argc, argv, options)) {
        print_usage();
        return 2;
    }

    std::unique_ptr<trading::host::ByteTransport> transport;
    if (options.simulation) {
        transport = std::make_unique<trading::host::SoftwareLoopbackTransport>();
    } else {
        auto spi = std::make_unique<trading::host::SpiTransport>(trading::host::SpiConfig{
            options.device, options.speed_hz, 0, 8});
        std::string error;
        if (!spi->open_device(error)) {
            std::cerr << "Unable to open physical SPI: " << error << "\n"
                      << "Use --sim for a development-PC protocol test.\n";
            return 3;
        }
        transport = std::move(spi);
    }

    trading::host::LoopbackClient client(*transport);
    trading::host::LatencyTracker tracker;
    std::uint32_t expected_sequence = 0;
    for (std::uint32_t sequence = 0; sequence < options.count; ++sequence) {
        const auto request = trading::protocol::make_loopback_request(
            sequence, 0xD00D000000000000ULL | sequence);
        const auto send_ns = trading::host::steady_time_ns();
        const auto result = client.exchange(request);
        const auto receive_ns = trading::host::steady_time_ns();

        if (result.success && result.response.sequence != expected_sequence) {
            tracker.record_sequence_error();
        }
        tracker.record({sequence, send_ns, receive_ns, result.success, result.error});
        if (result.success) {
            expected_sequence = sequence + 1;
        } else if (options.verbose) {
            std::cerr << "sequence " << sequence << ": " << result.error << "\n";
        }
    }

    const auto summary = tracker.summary();
    std::cout << "transport=" << transport->description() << "\n"
              << "packets_sent=" << summary.packets_sent
              << " packets_returned=" << summary.packets_returned
              << " failures=" << summary.failures
              << " missing=" << summary.missing_packets
              << " corrupted=" << summary.corrupted_packets
              << " other_failures=" << summary.other_failures
              << " sequence_errors=" << summary.sequence_errors << "\n";
    if (summary.packets_returned != 0) {
        std::cout << "rtt_software_or_hardware_ns: min=" << summary.minimum_ns
                  << " p50=" << summary.p50_ns
                  << " p95=" << summary.p95_ns
                  << " p99=" << summary.p99_ns
                  << " max=" << summary.maximum_ns
                  << " average_us=" << summary.average_us << "\n";
    }

    return (summary.failures == 0 && summary.sequence_errors == 0) ? 0 : 1;
}
