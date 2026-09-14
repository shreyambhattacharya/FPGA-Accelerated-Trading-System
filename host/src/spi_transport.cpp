#include "spi_transport.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <utility>

#ifdef __linux__
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <sys/ioctl.h>
#include <unistd.h>
#endif

namespace trading::host {

SpiTransport::SpiTransport(SpiConfig config) : config_(std::move(config)) {}

SpiTransport::~SpiTransport() {
#ifdef __linux__
    if (file_descriptor_ >= 0) ::close(file_descriptor_);
#endif
}

bool SpiTransport::open_device(std::string& error) {
#ifdef __linux__
    file_descriptor_ = ::open(config_.device.c_str(), O_RDWR | O_CLOEXEC);
    if (file_descriptor_ < 0) {
        error = "open(" + config_.device + "): " + std::strerror(errno);
        return false;
    }
    if (ioctl(file_descriptor_, SPI_IOC_WR_MODE, &config_.mode) < 0 ||
        ioctl(file_descriptor_, SPI_IOC_WR_BITS_PER_WORD, &config_.bits_per_word) < 0 ||
        ioctl(file_descriptor_, SPI_IOC_WR_MAX_SPEED_HZ, &config_.speed_hz) < 0) {
        error = "SPI configuration ioctl failed: " + std::string(std::strerror(errno));
        ::close(file_descriptor_);
        file_descriptor_ = -1;
        return false;
    }
    return true;
#else
    error = "Linux spidev is unavailable on this host; use --sim for a PC-side protocol test";
    return false;
#endif
}

TransferResult SpiTransport::transfer(const protocol::PacketBytes& transmit) {
    TransferResult result;
#ifdef __linux__
    if (file_descriptor_ < 0) {
        result.error = "SPI device is not open";
        return result;
    }
    protocol::PacketBytes receive{};
    spi_ioc_transfer transfer{};
    transfer.tx_buf = reinterpret_cast<__u64>(transmit.data());
    transfer.rx_buf = reinterpret_cast<__u64>(receive.data());
    transfer.len = static_cast<__u32>(protocol::kPacketBytes);
    transfer.speed_hz = config_.speed_hz;
    transfer.bits_per_word = config_.bits_per_word;
    const int rc = ioctl(file_descriptor_, SPI_IOC_MESSAGE(1), &transfer);
    if (rc < 0) {
        result.error = "SPI transfer failed: " + std::string(std::strerror(errno));
        return result;
    }
    result.ok = true;
    result.received = receive;
#else
    (void)transmit;
    result.error = "Linux spidev is unavailable on this host; use --sim for a PC-side protocol test";
#endif
    return result;
}

std::string SpiTransport::description() const {
    return config_.device + " @ " + std::to_string(config_.speed_hz) + " Hz, mode " +
           std::to_string(config_.mode);
}

TransferResult SoftwareLoopbackTransport::transfer(const protocol::PacketBytes& transmit) {
    TransferResult result;
    result.ok = true;
    result.received.fill(0);

    // Market events are one-way submissions in the current protocol. Keep
    // them out of the three-transfer diagnostic state machine so a synthetic
    // stream can exercise the real host event path deterministically.
    if (transmit[0] == protocol::kSyncVersion &&
        (transmit[1] == static_cast<std::uint8_t>(protocol::MessageType::MarketQuote) ||
         transmit[1] == static_cast<std::uint8_t>(protocol::MessageType::MarketTrade))) {
        return result;
    }

    switch (phase_) {
    case Phase::Request: {
        const auto request = protocol::Packet::decode_unchecked(transmit);
        protocol::StatusCode status = protocol::StatusCode::Ok;
        if (transmit[0] != protocol::kSyncVersion) {
            status = protocol::StatusCode::BadSync;
        } else if (transmit[31] != protocol::crc8(transmit)) {
            status = protocol::StatusCode::BadChecksum;
        } else if (request.message_type != protocol::MessageType::Loopback &&
                   request.message_type != protocol::MessageType::Control) {
            status = protocol::StatusCode::BadType;
        }
        pending_response_ = protocol::make_status_response(request, status);
        phase_ = Phase::Turnaround;
        break;
    }
    case Phase::Turnaround:
        phase_ = Phase::Response;
        break;
    case Phase::Response:
        result.received = pending_response_.serialize();
        phase_ = Phase::Request;
        break;
    }
    return result;
}

ExchangeResult LoopbackClient::exchange(const protocol::Packet& request,
                                        protocol::StatusCode expected_status) {
    ExchangeResult result;
    const protocol::PacketBytes zeros{};

    const auto request_transfer = transport_.transfer(request.serialize());
    if (!request_transfer.ok) {
        result.error = request_transfer.error;
        return result;
    }
    const auto turnaround_transfer = transport_.transfer(zeros);
    if (!turnaround_transfer.ok) {
        result.error = turnaround_transfer.error;
        return result;
    }
    const auto response_transfer = transport_.transfer(zeros);
    if (!response_transfer.ok) {
        result.error = response_transfer.error;
        return result;
    }

    if (std::all_of(response_transfer.received.begin(), response_transfer.received.end(),
                    [](std::uint8_t value) { return value == 0; })) {
        result.error = "response missing";
        return result;
    }

    const auto parsed = protocol::parse(response_transfer.received);
    if (!parsed.valid()) {
        result.error = "response " + parsed.error_string();
        return result;
    }
    result.response = parsed.packet;
    if (parsed.packet.message_type != protocol::MessageType::Status) {
        result.error = "response has unexpected message type";
        return result;
    }
    if (parsed.packet.sequence != request.sequence) {
        result.error = "response sequence mismatch";
        return result;
    }
    if (parsed.packet.side_status != static_cast<std::uint8_t>(expected_status)) {
        result.error = "response status mismatch";
        return result;
    }
    result.success = true;
    return result;
}

} // namespace trading::host
