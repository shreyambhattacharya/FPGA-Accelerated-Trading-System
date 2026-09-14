#pragma once

#include "alpaca_message_parser.hpp"
#include "market_data_source.hpp"
#include "websocket_client.hpp"

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace trading {

enum class AlpacaConnectionState {
    Disconnected,
    Resolving,
    TcpConnecting,
    TlsHandshake,
    WebSocketHandshake,
    Authenticating,
    Subscribing,
    Streaming,
    Backoff,
    Failed,
    Stopped,
};

const char* alpaca_connection_state_name(AlpacaConnectionState state);

struct AlpacaMarketDataConfig {
    std::string feed = "iex";
    std::vector<std::string> symbols{"SPY", "QQQ", "NVDA", "AMD"};
    std::size_t queue_capacity = 4096;
    std::size_t max_events = 0;
    std::chrono::seconds max_seconds{0};
    WebSocketTimeouts timeouts{};
    std::string api_key_environment = "ALPACA_API_KEY";
    std::string api_secret_environment = "ALPACA_API_SECRET";
};

class AlpacaMarketDataSource final : public MarketDataSource {
public:
    AlpacaMarketDataSource(AlpacaMarketDataConfig config,
                           const SymbolRegistry& symbols,
                           std::unique_ptr<WebSocketSession> session);
    ~AlpacaMarketDataSource() override;

    bool next(MarketEvent& event) override;
    void request_stop() override;
    MarketDataTelemetry telemetry() const override;

    AlpacaConnectionState state() const;
    std::string last_error() const override { return last_error_; }
    bool configuration_valid() const { return configuration_error_.empty(); }
    const std::string& configuration_error() const { return configuration_error_; }

    static std::chrono::milliseconds reconnect_delay(std::uint32_t attempt);

private:
    bool validate_configuration();
    bool connect_and_subscribe();
    bool receive_until_event();
    bool process_frame(const std::string& frame, std::uint64_t receive_system_ns);
    bool process_message(const AlpacaMessage& message);
    bool send_authentication();
    bool send_subscription();
    bool credentials_present(std::string& key, std::string& secret) const;
    bool deadline_reached() const;
    void set_state(AlpacaConnectionState state);
    void record_ingress(const AlpacaMessage& message, std::uint64_t normalization_start_ns);
    void record_failure_and_backoff(const std::string& error);

    AlpacaMarketDataConfig config_;
    const SymbolRegistry& symbols_;
    std::unique_ptr<WebSocketSession> session_;
    AlpacaMessageParser parser_;
    mutable std::mutex mutex_;
    std::deque<MarketEvent> queue_;
    MarketDataTelemetry telemetry_;
    AlpacaConnectionState state_ = AlpacaConnectionState::Disconnected;
    std::string configuration_error_;
    std::string last_error_;
    std::uint32_t reconnect_attempt_ = 0;
    std::uint64_t normalized_events_ = 0;
    std::uint64_t start_steady_ns_ = 0;
    bool auth_acknowledged_ = false;
    bool subscription_acknowledged_ = false;
    std::atomic<bool> stop_requested_{false};
};

} // namespace trading
