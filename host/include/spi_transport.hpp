#pragma once

#include "protocol.hpp"

#include <cstdint>
#include <memory>
#include <string>

namespace trading::host {

struct SpiConfig {
    std::string device = "/dev/spidev0.0";
    std::uint32_t speed_hz = 5'000'000;
    std::uint8_t mode = 0; // SPI mode 0
    std::uint8_t bits_per_word = 8;
};

struct TransferResult {
    bool ok = false;
    protocol::PacketBytes received{};
    std::string error;
};

class ByteTransport {
public:
    virtual ~ByteTransport() = default;
    virtual TransferResult transfer(const protocol::PacketBytes& transmit) = 0;
    virtual std::string description() const = 0;
};

class SpiTransport final : public ByteTransport {
public:
    explicit SpiTransport(SpiConfig config);
    ~SpiTransport() override;

    bool open_device(std::string& error);
    TransferResult transfer(const protocol::PacketBytes& transmit) override;
    std::string description() const override;

private:
    SpiConfig config_;
    int file_descriptor_ = -1;
};

// Deterministic PC-side model of the three-transfer FPGA turnaround. This is
// a protocol test double, not a hardware benchmark.
class SoftwareLoopbackTransport final : public ByteTransport {
public:
    TransferResult transfer(const protocol::PacketBytes& transmit) override;
    std::string description() const override { return "software-loopback"; }

private:
    enum class Phase { Request, Turnaround, Response };
    Phase phase_ = Phase::Request;
    protocol::Packet pending_response_{};
};

struct ExchangeResult {
    bool success = false;
    protocol::Packet response{};
    std::string error;
};

class LoopbackClient {
public:
    explicit LoopbackClient(ByteTransport& transport) : transport_(transport) {}

    ExchangeResult exchange(const protocol::Packet& request,
                            protocol::StatusCode expected_status = protocol::StatusCode::Ok);

private:
    ByteTransport& transport_;
};

} // namespace trading::host
