#include "protocol.hpp"

#include <iostream>

int main() {
    using namespace trading::protocol;
    const auto request = make_loopback_request(
        17, 0xD00D000000000011ULL, 0x1234, 0x0102030405060708ULL, 42, 0);
    const auto bytes = request.serialize();

    if (bytes[0] != 0xA1 || bytes[1] != 0x07 || bytes[2] != 0x12 || bytes[3] != 0x34 ||
        bytes[25] != 0x00 || bytes[28] != 0x11 || bytes[31] != 0xEA || bytes[31] != crc8(bytes)) {
        std::cerr << "protocol serialization failed\n";
        return 1;
    }
    const auto parsed = parse(bytes);
    if (!parsed.valid() || parsed.packet.sequence != 17 || parsed.packet.data != 0xD00D000000000011ULL) {
        std::cerr << "protocol parsing failed\n";
        return 1;
    }

    auto bad_crc = bytes;
    bad_crc[31] ^= 0x01;
    if (parse(bad_crc).error != ParseError::BadChecksum) {
        std::cerr << "checksum rejection failed\n";
        return 1;
    }
    auto bad_sync = bytes;
    bad_sync[0] = 0x00;
    if (parse(bad_sync).error != ParseError::BadSync) {
        std::cerr << "sync rejection failed\n";
        return 1;
    }

    const auto response = make_status_response(request, StatusCode::Ok).serialize();
    const auto response_parse = parse(response);
    if (!response_parse.valid() || response_parse.packet.message_type != MessageType::Status ||
        response_parse.packet.side_status != static_cast<std::uint8_t>(StatusCode::Ok) ||
        response_parse.packet.sequence != request.sequence) {
        std::cerr << "status response construction failed\n";
        return 1;
    }

    std::cout << "protocol_unit_test: PASS\n";
    return 0;
}
