#include "trading_engine.hpp"

#include <utility>

namespace trading {

TradingEngine::TradingEngine(MarketDataSource& source,
                             SymbolRegistry& symbols,
                             FpgaClient& fpga,
                             SequenceManager& sequences,
                             TradingEngineConfig config)
    : source_(source), symbols_(symbols), fpga_(fpga), sequences_(sequences), config_(std::move(config)) {}

EngineStats TradingEngine::run() {
    EngineStats stats;
    MarketEvent event;
    while (source_.next(event)) {
        ++stats.events_received;
        if (stats.start_timestamp_ns == 0) stats.start_timestamp_ns = event.timestamp_ns;
        stats.last_event_timestamp_ns = event.timestamp_ns;

        if (event.symbol_id >= config_.num_symbols || !symbols_.lookup_slot(event.symbol_id)) {
            ++stats.events_rejected;
            ++stats.unknown_symbols;
            continue;
        }
        if (!symbols_.is_enabled(event.symbol_id)) {
            ++stats.events_rejected;
            continue;
        }

        try {
            event.sequence = sequences_.next(event.symbol_id);
            ++stats.sequence_assignments;
        } catch (const SequenceOverflow& error) {
            ++stats.events_rejected;
            stats.last_error = error.what();
            break;
        }

        const auto send_ns = host::steady_time_ns();
        const auto result = fpga_.send_market_event(event);
        const auto receive_ns = host::steady_time_ns();
        if (result.success) {
            ++stats.events_sent_to_fpga;
        } else {
            ++stats.transport_failures;
            stats.last_error = result.error;
        }
        latency_tracker_.record({result.sequence, send_ns, receive_ns, result.success, result.error});
    }
    return stats;
}

} // namespace trading
