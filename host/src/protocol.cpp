#include "protocol.hpp"

#include <algorithm>

namespace trading::protocol {
namespace {

void put_u16(PacketBytes& bytes, std::size_t offset, std::uint16_t value) {
    bytes[offset] = static_cast<std::uint8_t>(value >> 8);
    bytes[offset + 1] = static_cast<std::uint8_t>(value);
}

void put_u32(PacketBytes& bytes, std::size_t offset, std::uint32_t value) {
    for (int i = 0; i < 4; ++i) {
        bytes[offset + i] = static_cast<std::uint8_t>(value >> (8 * (3 - i)));
    }
}

void put_u64(PacketBytes& bytes, std::size_t offset, std::uint64_t value) {
    for (int i = 0; i < 8; ++i) {
        bytes[offset + i] = static_cast<std::uint8_t>(value >> (8 * (7 - i)));
    }
}

std::uint16_t get_u16(const PacketBytes& bytes, std::size_t offset) {
    return static_cast<std::uint16_t>((static_cast<std::uint16_t>(bytes[offset]) << 8) |
                                       bytes[offset + 1]);
}

std::uint32_t get_u32(const PacketBytes& bytes, std::size_t offset) {
    std::uint32_t value = 0;
    for (int i = 0; i < 4; ++i) value = (value << 8) | bytes[offset + i];
    return value;
}

std::uint64_t get_u64(const PacketBytes& bytes, std::size_t offset) {
    std::uint64_t value = 0;
    for (int i = 0; i < 8; ++i) value = (value << 8) | bytes[offset + i];
    return value;
}

} // namespace

std::uint8_t crc8(const PacketBytes& bytes) {
    std::uint8_t crc = 0;
    for (std::size_t i = 0; i < kPacketBytes - 1; ++i) {
        crc ^= bytes[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = static_cast<std::uint8_t>((crc & 0x80U) ? ((crc << 1) ^ 0x07U)
                                                                  : (crc << 1));
        }
    }
    return crc;
}

PacketBytes Packet::serialize() const {
    PacketBytes bytes{};
    bytes[0] = sync_version;
    bytes[1] = static_cast<std::uint8_t>(message_type);
    put_u16(bytes, 2, symbol_id);
    put_u64(bytes, 4, timestamp_ns);
    put_u64(bytes, 12, data);
    put_u32(bytes, 20, quantity);
    bytes[24] = side_status;
    put_u32(bytes, 25, sequence);
    put_u16(bytes, 29, flags);
    bytes[31] = crc8(bytes);
    return bytes;
}

Packet Packet::decode_unchecked(const PacketBytes& bytes) {
    Packet packet{};
    packet.sync_version = bytes[0];
    packet.message_type = static_cast<MessageType>(bytes[1]);
    packet.symbol_id = get_u16(bytes, 2);
    packet.timestamp_ns = get_u64(bytes, 4);
    packet.data = get_u64(bytes, 12);
    packet.quantity = get_u32(bytes, 20);
    packet.side_status = bytes[24];
    packet.sequence = get_u32(bytes, 25);
    packet.flags = get_u16(bytes, 29);
    packet.checksum = bytes[31];
    return packet;
}

ParseResult parse(const PacketBytes& bytes) {
    ParseResult result;
    result.packet = Packet::decode_unchecked(bytes);
    if (bytes[0] != kSyncVersion) {
        result.error = ParseError::BadSync;
    } else if (bytes[31] != crc8(bytes)) {
        result.error = ParseError::BadChecksum;
    }
    return result;
}

std::string ParseResult::error_string() const {
    switch (error) {
    case ParseError::None: return {};
    case ParseError::BadSync: return "invalid sync/version";
    case ParseError::BadChecksum: return "checksum mismatch";
    }
    return "unknown parse error";
}

Packet make_loopback_request(std::uint32_t sequence,
                             std::uint64_t known_data,
                             std::uint16_t symbol_id,
                             std::uint64_t timestamp_ns,
                             std::uint32_t quantity,
                             std::uint16_t flags) {
    Packet packet;
    packet.message_type = MessageType::Loopback;
    packet.symbol_id = symbol_id;
    packet.timestamp_ns = timestamp_ns;
    packet.data = known_data;
    packet.quantity = quantity;
    packet.sequence = sequence;
    packet.flags = flags;
    return packet;
}

Packet make_status_response(const Packet& request, StatusCode status) {
    Packet response = request;
    response.sync_version = kSyncVersion;
    response.message_type = MessageType::Status;
    response.side_status = static_cast<std::uint8_t>(status);
    response.flags = (status == StatusCode::Ok) ? 0x0001 : 0x0000;
    switch (status) {
    case StatusCode::Ok: response.flags = 0x0001; break;
    case StatusCode::BadSync: response.flags = 0x0002; break;
    case StatusCode::BadChecksum: response.flags = 0x0004; break;
    case StatusCode::BadType: response.flags = 0x0008; break;
    case StatusCode::BadSymbol: response.flags = 0x0010; break;
    case StatusCode::DuplicateSequence: response.flags = 0x0020; break;
    case StatusCode::StaleSequence: response.flags = 0x0040; break;
    case StatusCode::BadSide: response.flags = 0x0080; break;
    }
    response.checksum = 0;
    return response;
}

} // namespace trading::protocol
