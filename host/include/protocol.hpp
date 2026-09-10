#pragma once

#include <array>
#include <cstdint>
#include <string>

namespace trading::protocol {

constexpr std::size_t kPacketBytes = 32;
constexpr std::uint8_t kSyncVersion = 0xA1;

enum class MessageType : std::uint8_t {
    MarketQuote = 0x01,
    MarketTrade = 0x02,
    Control = 0x03,
    Heartbeat = 0x04,
    Signal = 0x05,
    Status = 0x06,
    Loopback = 0x07,
};

enum class StatusCode : std::uint8_t {
    Ok = 0x01,
    BadSync = 0xE1,
    BadChecksum = 0xE2,
    BadType = 0xE3,
};

using PacketBytes = std::array<std::uint8_t, kPacketBytes>;

struct Packet {
    std::uint8_t sync_version = kSyncVersion;
    MessageType message_type = MessageType::Loopback;
    std::uint16_t symbol_id = 0;
    std::uint64_t timestamp_ns = 0;
    std::uint64_t data = 0;
    std::uint32_t quantity = 0;
    std::uint8_t side_status = 0;
    std::uint32_t sequence = 0;
    std::uint16_t flags = 0;
    std::uint8_t checksum = 0;

    PacketBytes serialize() const;
    static Packet decode_unchecked(const PacketBytes& bytes);
};

enum class ParseError {
    None,
    BadSync,
    BadChecksum,
};

struct ParseResult {
    Packet packet{};
    ParseError error = ParseError::None;
    bool valid() const { return error == ParseError::None; }
    std::string error_string() const;
};

std::uint8_t crc8(const PacketBytes& bytes);
ParseResult parse(const PacketBytes& bytes);

Packet make_loopback_request(std::uint32_t sequence,
                             std::uint64_t known_data,
                             std::uint16_t symbol_id = 0x1234,
                             std::uint64_t timestamp_ns = 0x0102030405060708ULL,
                             std::uint32_t quantity = 0x0000002A,
                             std::uint16_t flags = 0);

Packet make_status_response(const Packet& request, StatusCode status);

} // namespace trading::protocol
