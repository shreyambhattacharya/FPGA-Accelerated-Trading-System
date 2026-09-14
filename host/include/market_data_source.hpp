#pragma once

#include "market_event.hpp"

namespace trading {

class MarketDataSource {
public:
    virtual ~MarketDataSource() = default;
    virtual bool next(MarketEvent& event) = 0;
};

} // namespace trading
