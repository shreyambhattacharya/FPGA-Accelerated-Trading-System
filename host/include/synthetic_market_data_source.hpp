#pragma once

#include "market_data_source.hpp"

#include <cstddef>

namespace trading {

class SyntheticMarketDataSource final : public MarketDataSource {
public:
    explicit SyntheticMarketDataSource(std::size_t event_count = 20);

    bool next(MarketEvent& event) override;
    std::size_t emitted() const { return index_; }
    MarketDataTelemetry telemetry() const override;

private:
    std::size_t event_count_;
    std::size_t index_ = 0;
};

} // namespace trading
