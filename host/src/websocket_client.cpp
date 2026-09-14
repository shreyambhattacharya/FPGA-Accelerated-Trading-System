#include "websocket_client.hpp"

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/version.hpp>
#include <boost/beast/websocket.hpp>
#include <openssl/ssl.h>

#include <sstream>

namespace trading {
namespace {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
using tcp = asio::ip::tcp;
using error_code = boost::system::error_code;

class BeastWebSocketSession final : public WebSocketSession {
public:
    BeastWebSocketSession()
        : ssl_context_(asio::ssl::context::tls_client), websocket_(io_context_, ssl_context_) {
        ssl_context_.set_verify_mode(asio::ssl::verify_peer);
        error_code error;
        ssl_context_.set_default_verify_paths(error);
        if (error) certificate_context_error_ = error.message();
    }

    bool connect(const std::string& host,
                 const std::string& target,
                 const WebSocketTimeouts& timeouts,
                 std::string& error) override {
        if (!certificate_context_error_.empty()) {
            error = "TLS trust-store initialization failed: " + certificate_context_error_;
            return false;
        }
        close();
        io_context_.restart();
        bool complete = false;
        bool succeeded = false;
        std::string failure;
        tcp::resolver resolver(io_context_);
        asio::steady_timer dns_timer(io_context_);

        auto finish = [&](const error_code& ec, const std::string& phase) {
            if (complete) return;
            complete = true;
            if (ec) failure = phase + ": " + ec.message();
            else succeeded = true;
            dns_timer.cancel();
        };

        resolver.async_resolve(host, "443", [&](const error_code& ec, tcp::resolver::results_type results) {
            if (ec) return finish(ec, "DNS resolution");
            beast::get_lowest_layer(websocket_).expires_after(timeouts.dns_connect);
            beast::get_lowest_layer(websocket_).async_connect(results,
                [&](const error_code& connect_error, const tcp::endpoint&) {
                    if (connect_error) return finish(connect_error, "TCP connect");
                    error_code sni_error;
                    if (!SSL_set_tlsext_host_name(websocket_.next_layer().native_handle(), host.c_str())) {
                        sni_error = asio::error::operation_aborted;
                    }
                    if (sni_error) return finish(sni_error, "TLS SNI");
                    websocket_.next_layer().set_verify_callback(asio::ssl::host_name_verification(host));
                    beast::get_lowest_layer(websocket_).expires_after(timeouts.tls_handshake);
                    websocket_.next_layer().async_handshake(asio::ssl::stream_base::client,
                        [&](const error_code& tls_error) {
                            if (tls_error) return finish(tls_error, "TLS handshake");
                            beast::get_lowest_layer(websocket_).expires_after(timeouts.websocket_handshake);
                            websocket_.set_option(websocket::stream_base::timeout::suggested(beast::role_type::client));
                            websocket_.async_handshake(host, target,
                                [&](const error_code& websocket_error) {
                                    if (websocket_error) return finish(websocket_error, "WebSocket handshake");
                                    beast::get_lowest_layer(websocket_).expires_never();
                                    finish({}, {});
                                });
                        });
                });
        });
        dns_timer.expires_after(timeouts.dns_connect);
        dns_timer.async_wait([&](const error_code& timer_error) {
            if (!timer_error && !complete) {
                resolver.cancel();
                beast::get_lowest_layer(websocket_).socket().cancel();
                finish(asio::error::timed_out, "DNS/connect timeout");
            }
        });
        io_context_.run();
        if (!succeeded) {
            close();
            error = failure.empty() ? "connection failed" : failure;
            return false;
        }
        connected_ = true;
        return true;
    }

    bool send_text(const std::string& text, std::string& error) override {
        if (!connected_) {
            error = "WebSocket is not connected";
            return false;
        }
        error_code ec;
        websocket_.text(true);
        websocket_.write(asio::buffer(text), ec);
        if (ec) {
            error = "WebSocket write: " + ec.message();
            return false;
        }
        return true;
    }

    bool receive_text(std::string& text,
                      const WebSocketTimeouts& timeouts,
                      bool& timed_out,
                      std::string& error) override {
        timed_out = false;
        if (!connected_) {
            error = "WebSocket is not connected";
            return false;
        }
        beast::flat_buffer buffer;
        beast::get_lowest_layer(websocket_).expires_after(timeouts.read_poll);
        error_code ec;
        websocket_.read(buffer, ec);
        beast::get_lowest_layer(websocket_).expires_never();
        if (ec == beast::error::timeout || ec == asio::error::timed_out || ec == asio::error::operation_aborted) {
            timed_out = true;
            return true;
        }
        if (ec) {
            error = "WebSocket read: " + ec.message();
            return false;
        }
        text = beast::buffers_to_string(buffer.data());
        return true;
    }

    void close() override {
        if (connected_) {
            error_code ignored;
            websocket_.close(websocket::close_code::normal, ignored);
        }
        error_code ignored;
        beast::get_lowest_layer(websocket_).socket().shutdown(tcp::socket::shutdown_both, ignored);
        beast::get_lowest_layer(websocket_).socket().close(ignored);
        connected_ = false;
    }

private:
    asio::io_context io_context_;
    asio::ssl::context ssl_context_;
    websocket::stream<beast::ssl_stream<beast::tcp_stream>> websocket_;
    std::string certificate_context_error_;
    bool connected_ = false;
};

} // namespace

std::unique_ptr<WebSocketSession> make_beast_websocket_session() {
    return std::make_unique<BeastWebSocketSession>();
}

} // namespace trading
