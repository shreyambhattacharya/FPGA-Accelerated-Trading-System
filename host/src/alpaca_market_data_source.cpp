#include "alpaca_market_data_source.hpp"

#include "latency_tracker.hpp"

#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unordered_set>

namespace trading {
namespace {

constexpr const char* kAlpacaHost = "stream.data.alpaca.markets";

std::string json_escape(std::string_view value) {
    std::string escaped;
    escaped.reserve(value.size() + 2);
    for (const char character : value) {
        switch (character) {
        case '\\': escaped += "\\\\"; break;
        case '"': escaped += "\\\""; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default: escaped.push_back(character); break;
        }
    }
    return escaped;
}

std::string string_array_json(const std::vector<std::string>& values) {
    std::string result = "[";
    for (std::size_t index = 0; index < values.size(); ++index) {
        if (index != 0) result += ',';
        result += '"' + json_escape(values[index]) + '"';
    }
    result += ']';
    return result;
}

std::uint64_t now_steady() {
    return host::steady_time_ns();
}

} // namespace

const char* alpaca_connection_state_name(AlpacaConnectionState state) {
    switch (state) {
    case AlpacaConnectionState::Disconnected: return "DISCONNECTED";
    case AlpacaConnectionState::Resolving: return "RESOLVING";
    case AlpacaConnectionState::TcpConnecting: return "TCP_CONNECTING";
    case AlpacaConnectionState::TlsHandshake: return "TLS_HANDSHAKE";
    case AlpacaConnectionState::WebSocketHandshake: return "WEBSOCKET_HANDSHAKE";
    case AlpacaConnectionState::Authenticating: return "AUTHENTICATING";
    case AlpacaConnectionState::Subscribing: return "SUBSCRIBING";
    case AlpacaConnectionState::Streaming: return "STREAMING";
    case AlpacaConnectionState::Backoff: return "BACKOFF";
    case AlpacaConnectionState::Failed: return "FAILED";
    case AlpacaConnectionState::Stopped: return "STOPPED";
    }
    return "UNKNOWN";
}

AlpacaMarketDataSource::AlpacaMarketDataSource(AlpacaMarketDataConfig config,
                                               const SymbolRegistry& symbols,
                                               std::unique_ptr<WebSocketSession> session)
    : config_(std::move(config)), symbols_(symbols), session_(std::move(session)), parser_(symbols) {
    telemetry_.source = "ALPACA";
    telemetry_.feed = config_.feed;
    validate_configuration();
}

AlpacaMarketDataSource::~AlpacaMarketDataSource() {
    request_stop();
}

void AlpacaMarketDataSource::set_state(AlpacaConnectionState state) {
    AlpacaConnectionState previous;
    bool changed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        previous = state_;
        changed = previous != state;
        state_ = state;
    }
    if (changed) {
        std::clog << "alpaca_connection_state=" << alpaca_connection_state_name(state) << "\n";
    }
}

AlpacaConnectionState AlpacaMarketDataSource::state() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_;
}

bool AlpacaMarketDataSource::validate_configuration() {
    if (config_.feed != "iex" && config_.feed != "sip" && config_.feed != "delayed_sip" && config_.feed != "test") {
        configuration_error_ = "unsupported Alpaca market-data feed: " + config_.feed;
        set_state(AlpacaConnectionState::Failed);
        return false;
    }
    if (!session_) {
        configuration_error_ = "Alpaca source requires a WebSocket session";
        set_state(AlpacaConnectionState::Failed);
        return false;
    }
    if (config_.queue_capacity == 0) {
        configuration_error_ = "Alpaca event queue capacity must be positive";
        set_state(AlpacaConnectionState::Failed);
        return false;
    }
    std::unordered_set<std::string> unique;
    if (config_.symbols.empty() || config_.symbols.size() > symbols_.capacity()) {
        configuration_error_ = "configured subscriptions exceed enabled symbol capacity";
        set_state(AlpacaConnectionState::Failed);
        return false;
    }
    for (const auto& symbol : config_.symbols) {
        if (symbol.empty() || !unique.insert(symbol).second) {
            configuration_error_ = "subscriptions contain an empty or duplicate ticker";
            set_state(AlpacaConnectionState::Failed);
            return false;
        }
        const auto slot = symbols_.lookup_symbol(symbol);
        if (!slot || !symbols_.is_enabled(*slot)) {
            configuration_error_ = "subscription ticker is not mapped to an enabled FPGA slot: " + symbol;
            set_state(AlpacaConnectionState::Failed);
            return false;
        }
    }
    return true;
}

bool AlpacaMarketDataSource::credentials_present(std::string& key, std::string& secret) const {
    const char* key_value = std::getenv(config_.api_key_environment.c_str());
    const char* secret_value = std::getenv(config_.api_secret_environment.c_str());
    if (key_value == nullptr || *key_value == '\0' || secret_value == nullptr || *secret_value == '\0') return false;
    key = key_value;
    secret = secret_value;
    return true;
}

bool AlpacaMarketDataSource::send_authentication() {
    std::string key;
    std::string secret;
    if (session_->requires_credentials() && !credentials_present(key, secret)) {
        last_error_ = "Alpaca credentials are missing from the configured environment variables";
        ++telemetry_.auth_failures;
        set_state(AlpacaConnectionState::Failed);
        return false;
    }
    const std::string message = session_->requires_credentials()
        ? "{\"action\":\"auth\",\"key\":\"" + json_escape(key) +
          "\",\"secret\":\"" + json_escape(secret) + "\"}"
        : "{\"action\":\"auth\"}";
    std::string error;
    if (!session_->send_text(message, error)) {
        last_error_ = "Alpaca authentication send failed: " + error;
        ++telemetry_.auth_failures;
        return false;
    }
    return true;
}

bool AlpacaMarketDataSource::send_subscription() {
    const auto tickers = string_array_json(config_.symbols);
    const std::string message = "{\"action\":\"subscribe\",\"quotes\":" + tickers +
                                ",\"trades\":" + tickers + "}";
    std::string error;
    if (!session_->send_text(message, error)) {
        last_error_ = "Alpaca subscription send failed: " + error;
        ++telemetry_.subscription_failures;
        return false;
    }
    return true;
}

bool AlpacaMarketDataSource::connect_and_subscribe() {
    if (stop_requested_.load()) return false;
    if (!configuration_error_.empty()) return false;

    std::string error;
    set_state(AlpacaConnectionState::Resolving);
    set_state(AlpacaConnectionState::TcpConnecting);
    set_state(AlpacaConnectionState::TlsHandshake);
    set_state(AlpacaConnectionState::WebSocketHandshake);
    if (!session_->connect(kAlpacaHost, "/v2/" + config_.feed, config_.timeouts, error)) {
        record_failure_and_backoff("Alpaca connection failed: " + error);
        return false;
    }

    set_state(AlpacaConnectionState::Authenticating);
    auth_acknowledged_ = false;
    subscription_acknowledged_ = false;
    if (!send_authentication()) return false;
    const auto auth_deadline = now_steady() + static_cast<std::uint64_t>(config_.timeouts.authentication.count()) * 1'000'000ULL;
    bool authenticated = false;
    while (!authenticated && !stop_requested_.load() && !deadline_reached() && now_steady() < auth_deadline) {
        std::string frame;
        bool timed_out = false;
        if (!session_->receive_text(frame, config_.timeouts, timed_out, error)) {
            if (timed_out) continue;
            ++telemetry_.auth_failures;
            record_failure_and_backoff("Alpaca authentication receive failed: " + error);
            return false;
        }
        if (timed_out) continue;
        if (!process_frame(frame, host::system_time_ns())) return false;
        authenticated = auth_acknowledged_;
    }
    if (deadline_reached()) {
        request_stop();
        return false;
    }
    if (!authenticated) {
        ++telemetry_.auth_failures;
        last_error_ = "Alpaca authentication timed out";
        set_state(AlpacaConnectionState::Failed);
        session_->close();
        return false;
    }

    set_state(AlpacaConnectionState::Subscribing);
    if (!send_subscription()) {
        record_failure_and_backoff(last_error_);
        return false;
    }
    const auto subscription_deadline = now_steady() + static_cast<std::uint64_t>(config_.timeouts.subscription.count()) * 1'000'000ULL;
    while (!stop_requested_.load() && !deadline_reached() && now_steady() < subscription_deadline) {
        std::string frame;
        bool timed_out = false;
        if (!session_->receive_text(frame, config_.timeouts, timed_out, error)) {
            if (timed_out) continue;
            ++telemetry_.subscription_failures;
            record_failure_and_backoff("Alpaca subscription receive failed: " + error);
            return false;
        }
        if (timed_out) continue;
        if (!process_frame(frame, host::system_time_ns())) return false;
        if (subscription_acknowledged_) {
            set_state(AlpacaConnectionState::Streaming);
            return true;
        }
    }
    if (deadline_reached()) {
        request_stop();
        return false;
    }
    ++telemetry_.subscription_failures;
    last_error_ = "Alpaca subscription acknowledgement timed out";
    record_failure_and_backoff(last_error_);
    return false;
}

bool AlpacaMarketDataSource::process_message(const AlpacaMessage& message) {
    if (message.kind == AlpacaMessageKind::Error) {
        last_error_ = "Alpaca provider error";
        if (message.error_code != 0) last_error_ += " code=" + std::to_string(message.error_code);
        if (!message.error_text.empty()) last_error_ += " message=" + message.error_text;
        return false;
    }
    if (state() == AlpacaConnectionState::Authenticating && message.kind == AlpacaMessageKind::Authenticated) {
        auth_acknowledged_ = true;
        std::clog << "alpaca_authentication=success\n";
        set_state(AlpacaConnectionState::Subscribing);
        return true;
    }
    if (state() == AlpacaConnectionState::Subscribing && message.kind == AlpacaMessageKind::Subscription) {
        subscription_acknowledged_ = true;
        std::clog << "alpaca_subscription=acknowledged symbols=" << config_.symbols.size() << "\n";
        set_state(AlpacaConnectionState::Streaming);
        return true;
    }
    if (state() == AlpacaConnectionState::Streaming && !message.events.empty()) {
        for (const auto& event : message.events) {
            if (queue_.size() >= config_.queue_capacity) {
                ++telemetry_.queue_overflow;
                continue;
            }
            queue_.push_back(event);
            telemetry_.queue_high_watermark = std::max<std::uint64_t>(telemetry_.queue_high_watermark, queue_.size());
        }
    }
    return true;
}

void AlpacaMarketDataSource::record_ingress(const AlpacaMessage& message, std::uint64_t normalization_start_ns) {
    if (message.provider_timestamp_ns != 0) {
        telemetry_.last_provider_timestamp_ns = message.provider_timestamp_ns;
        telemetry_.last_receive_steady_ns = now_steady();
    }
    const auto end = now_steady();
    const auto duration = end >= normalization_start_ns ? end - normalization_start_ns : 0;
    ++telemetry_.normalization_latency_samples;
    if (telemetry_.normalization_latency_samples == 1) {
        telemetry_.normalization_latency_min_ns = duration;
        telemetry_.normalization_latency_max_ns = duration;
    } else {
        telemetry_.normalization_latency_min_ns = std::min(telemetry_.normalization_latency_min_ns, duration);
        telemetry_.normalization_latency_max_ns = std::max(telemetry_.normalization_latency_max_ns, duration);
    }
    telemetry_.normalization_latency_sum_ns += static_cast<long double>(duration);
}

bool AlpacaMarketDataSource::process_frame(const std::string& frame, std::uint64_t receive_system_ns) {
    ++telemetry_.websocket_frames_received;
    const auto normalization_start = now_steady();
    const auto parsed = parser_.parse_frame(frame, receive_system_ns, telemetry_);
    for (const auto& message : parsed.messages) {
        if (message.kind == AlpacaMessageKind::Malformed) continue;
        if (!process_message(message)) {
            if (state() == AlpacaConnectionState::Authenticating) ++telemetry_.auth_failures;
            if (state() == AlpacaConnectionState::Subscribing) ++telemetry_.subscription_failures;
            set_state(AlpacaConnectionState::Failed);
            session_->close();
            return false;
        }
        record_ingress(message, normalization_start);
    }
    return true;
}

bool AlpacaMarketDataSource::receive_until_event() {
    while (queue_.empty() && !stop_requested_.load()) {
        if (deadline_reached()) {
            request_stop();
            return false;
        }
        std::string frame;
        std::string error;
        bool timed_out = false;
        if (!session_->receive_text(frame, config_.timeouts, timed_out, error)) {
            if (timed_out) continue;
            record_failure_and_backoff("Alpaca stream receive failed: " + error);
            return false;
        }
        if (timed_out) continue;
        if (!process_frame(frame, host::system_time_ns())) {
            if (state() == AlpacaConnectionState::Failed) return false;
            record_failure_and_backoff(last_error_);
            return false;
        }
    }
    return !queue_.empty();
}

bool AlpacaMarketDataSource::deadline_reached() const {
    if (config_.max_seconds.count() <= 0 || start_steady_ns_ == 0) return false;
    return now_steady() - start_steady_ns_ >= static_cast<std::uint64_t>(config_.max_seconds.count()) * 1'000'000'000ULL;
}

void AlpacaMarketDataSource::record_failure_and_backoff(const std::string& error) {
    last_error_ = error;
    if (session_) session_->close();
    if (stop_requested_.load()) {
        set_state(AlpacaConnectionState::Stopped);
        return;
    }
    ++telemetry_.reconnect_count;
    ++reconnect_attempt_;
    std::clog << "alpaca_reconnect_attempt=" << reconnect_attempt_ << "\n";
    set_state(AlpacaConnectionState::Backoff);
}

std::chrono::milliseconds AlpacaMarketDataSource::reconnect_delay(std::uint32_t attempt) {
    const std::uint32_t seconds = attempt == 0 ? 1U :
        (attempt <= 4 ? (1U << (attempt - 1)) : 15U);
    return std::chrono::seconds(seconds);
}

bool AlpacaMarketDataSource::next(MarketEvent& event) {
    if (start_steady_ns_ == 0) start_steady_ns_ = now_steady();
    if (stop_requested_.load() || deadline_reached() ||
        (config_.max_events != 0 && normalized_events_ >= config_.max_events)) {
        request_stop();
        return false;
    }
    while (!stop_requested_.load()) {
        if (!queue_.empty()) {
            event = queue_.front();
            queue_.pop_front();
            ++normalized_events_;
            return true;
        }
        const auto current = state();
        if (current == AlpacaConnectionState::Failed || current == AlpacaConnectionState::Stopped) return false;
        if (current == AlpacaConnectionState::Backoff) {
            const auto delay = reconnect_delay(reconnect_attempt_);
            const auto end = now_steady() + static_cast<std::uint64_t>(delay.count()) * 1'000'000ULL;
            while (!stop_requested_.load() && now_steady() < end) std::this_thread::sleep_for(std::chrono::milliseconds(50));
            if (stop_requested_.load() || deadline_reached()) {
                request_stop();
                return false;
            }
            set_state(AlpacaConnectionState::Disconnected);
        }
        if (state() == AlpacaConnectionState::Disconnected && !connect_and_subscribe()) {
            if (state() == AlpacaConnectionState::Failed || stop_requested_.load()) return false;
            continue;
        }
        if (state() == AlpacaConnectionState::Streaming && receive_until_event()) continue;
        if (state() == AlpacaConnectionState::Failed || stop_requested_.load()) return false;
    }
    return false;
}

void AlpacaMarketDataSource::request_stop() {
    const bool was_stopped = stop_requested_.exchange(true);
    if (session_) session_->close();
    set_state(AlpacaConnectionState::Stopped);
    if (!was_stopped) std::clog << "alpaca_shutdown=clean\n";
}

MarketDataTelemetry AlpacaMarketDataSource::telemetry() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return telemetry_;
}

} // namespace trading
