#pragma once

#include "market_event.hpp"
#include "spi_transport.hpp"

#include <cstdint>
#include <string>

namespace trading {

struct FpgaSendResult {
    bool success = false;
    std::uint32_t sequence = 0;
    std::string error;
};

struct FpgaControlResult {
    bool success = false;
    std::uint8_t status = 0;
    std::string error;
};

class FpgaClient {
public:
    explicit FpgaClient(host::ByteTransport& transport) : transport_(transport) {}

    static protocol::Packet packet_for(const MarketEvent& event);
    static protocol::Packet control_packet(std::uint16_t symbol_id,
                                            std::uint8_t subcommand,
                                            std::uint64_t data64,
                                            std::uint32_t data32,
                                            std::uint32_t sequence = 0,
                                            std::uint16_t flags = 0);

    FpgaSendResult send_market_event(const MarketEvent& event);
    FpgaControlResult send_control(const protocol::Packet& request,
                                   protocol::StatusCode expected_status = protocol::StatusCode::Ok);

    std::uint64_t packets_submitted() const { return packets_submitted_; }
    std::uint64_t transport_failures() const { return transport_failures_; }

private:
    host::ByteTransport& transport_;
    std::uint64_t packets_submitted_ = 0;
    std::uint64_t transport_failures_ = 0;
};

} // namespace trading
