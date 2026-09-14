#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>

namespace trading {

struct WebSocketTimeouts {
    std::chrono::milliseconds dns_connect{10'000};
    std::chrono::milliseconds tls_handshake{10'000};
    std::chrono::milliseconds websocket_handshake{10'000};
    std::chrono::milliseconds authentication{10'000};
    std::chrono::milliseconds subscription{10'000};
    std::chrono::milliseconds read_poll{1'000};
};

class WebSocketSession {
public:
    virtual ~WebSocketSession() = default;
    virtual bool connect(const std::string& host,
                         const std::string& target,
                         const WebSocketTimeouts& timeouts,
                         std::string& error) = 0;
    virtual bool send_text(const std::string& text, std::string& error) = 0;
    // A read timeout is reported with timed_out=true and is not a connection
    // failure. It lets request_stop(), --max-seconds, and quiet markets remain
    // responsive without treating silence as an auth/subscription failure.
    virtual bool receive_text(std::string& text,
                              const WebSocketTimeouts& timeouts,
                              bool& timed_out,
                              std::string& error) = 0;
    virtual bool requires_credentials() const { return true; }
    virtual void close() = 0;
};

// Available only when FPGA_TRADER_ENABLE_NETWORKING is enabled. The optional
// target supplies Boost.Beast + OpenSSL TLS verification.
std::unique_ptr<WebSocketSession> make_beast_websocket_session();

} // namespace trading
