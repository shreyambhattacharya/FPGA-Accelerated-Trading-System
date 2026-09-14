#include "fpga_client.hpp"

#include <stdexcept>

namespace trading {

protocol::Packet FpgaClient::packet_for(const MarketEvent& event) {
    if (event.type != MarketEventType::Quote && event.type != MarketEventType::Trade) {
        throw std::invalid_argument("unsupported market event type");
    }
    if (event.side != kBidSide && event.side != kAskSide) {
        throw std::invalid_argument("market event side must be 0 or 1");
    }
    protocol::Packet packet;
    packet.message_type = event.type == MarketEventType::Quote
                              ? protocol::MessageType::MarketQuote
                              : protocol::MessageType::MarketTrade;
    packet.symbol_id = event.symbol_id;
    packet.timestamp_ns = event.timestamp_ns;
    packet.data = event.price_microdollars;
    packet.quantity = event.quantity;
    packet.side_status = event.side;
    packet.sequence = event.sequence;
    packet.flags = event.flags;
    return packet;
}

protocol::Packet FpgaClient::control_packet(std::uint16_t symbol_id,
                                            std::uint8_t subcommand,
                                            std::uint64_t data64,
                                            std::uint32_t data32,
                                            std::uint32_t sequence,
                                            std::uint16_t flags) {
    protocol::Packet packet;
    packet.message_type = protocol::MessageType::Control;
    packet.symbol_id = symbol_id;
    packet.data = data64;
    packet.quantity = data32;
    packet.side_status = subcommand;
    packet.sequence = sequence;
    packet.flags = flags;
    return packet;
}

FpgaSendResult FpgaClient::send_market_event(const MarketEvent& event) {
    FpgaSendResult result;
    result.sequence = event.sequence;
    const auto packet = packet_for(event);
    ++packets_submitted_;
    const auto transfer = transport_.transfer(packet.serialize());
    if (!transfer.ok) {
        ++transport_failures_;
        result.error = transfer.error;
        return result;
    }
    result.success = true;
    return result;
}

FpgaControlResult FpgaClient::send_control(const protocol::Packet& request,
                                           protocol::StatusCode expected_status) {
    FpgaControlResult result;
    ++packets_submitted_;
    host::LoopbackClient client(transport_);
    const auto exchange = client.exchange(request, expected_status);
    if (!exchange.success) {
        ++transport_failures_;
        result.error = exchange.error;
        return result;
    }
    result.success = true;
    result.status = exchange.response.side_status;
    return result;
}

} // namespace trading
