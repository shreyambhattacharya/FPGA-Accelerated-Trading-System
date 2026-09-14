# Real-time Alpaca market-data streaming

The real-time input adapter is market-data-only. It does not contain Alpaca
Trading API calls, account/position reads, orders, fills, risk approval, or a
candidate-result packet. The FPGA still receives the unchanged 32-byte quote
and trade protocol packets.

## Endpoint, feed, and credentials

The optional networking build connects with TLS to:

```text
wss://stream.data.alpaca.markets/v2/<feed>
```

Supported feeds are `iex`, `sip`, `delayed_sip`, and `test`; the default is
`iex`. The selected feed is retained in `MarketDataTelemetry::feed` and is
printed as `market_feed`, so a test stream cannot be mistaken for real market
data. `test` is explicitly FAKEPACA/test-stream mode and should be reported as
`market_data_source=ALPACA` plus `market_feed=test`.

The application reads `ALPACA_API_KEY` and `ALPACA_API_SECRET` from the child
process environment. Values are never logged, included in errors, fixtures,
or source control. Synthetic mode does not require either variable. A test
WebSocket session can explicitly opt out of credentials without placing a
credential in a test fixture.

## TLS and WebSocket lifecycle

`websocket_client.cpp` uses Boost.Asio/Boost.Beast and OpenSSL. It enables
peer verification, loads the platform trust store, sets SNI to
`stream.data.alpaca.markets`, and enables hostname verification. There is no
insecure certificate bypass option.

`AlpacaMarketDataSource` exposes these states:

```text
DISCONNECTED -> RESOLVING -> TCP_CONNECTING -> TLS_HANDSHAKE
              -> WEBSOCKET_HANDSHAKE -> AUTHENTICATING -> SUBSCRIBING
              -> STREAMING
```

Failures enter `BACKOFF` and retry at 1, 2, 4, 8, then 15 seconds. A provider
authentication rejection is fatal for that source instance and is not treated
as successful authentication. Subscription failure is counted and retried
after the bounded backoff. Disconnects can lose events; this milestone does
not attempt historical gap filling.

DNS/connect, TLS handshake, WebSocket handshake, authentication,
subscription, and read-poll timeouts are configurable. A quiet market is not
an authentication failure: a streaming read timeout only returns to the
polling loop. `request_stop()` closes the session and the CLI also supports
`--max-events` and `--max-seconds`.

## Authentication and subscription

After the WebSocket handshake, the source accepts the provider `connected`
status, sends `auth`, waits for `authenticated`, then sends one subscription
message containing `quotes` and `trades` for the configured ticker vector.
Bars, news, options, and wildcard subscriptions are not requested. Every
configured ticker must already be mapped to an enabled `SymbolRegistry` slot;
duplicates, empty tickers, and capacity overflow are rejected before connect.

## Normalization

Frames are parsed as JSON objects or arrays of objects. Every object is
processed in provider order; a frame is never assumed to contain one event.

* A quote requires `S`, `t`, `bp`, `bs`, `ap`, and `as`. It becomes a BID
  `MarketEvent` followed by an ASK `MarketEvent`, with the same provider
  timestamp and no assigned sequence. Quote sizes are multiplied by 100 to
  preserve the existing historical Alpaca round-lot semantics.
* A trade requires `S`, `t`, `p`, and `s`. It becomes one event with quantity
  multiplier 1. The side remains the existing unknown/default protocol value;
  the live adapter does not infer aggressor direction from quotes.
* Prices are converted from decimal text to integer USD microdollars. The
  parser recovers the original numeric lexeme from the validated JSON record,
  avoiding a binary floating-point price round trip.
* Timestamps are strict UTC RFC3339 `Z` values with 0–9 fractional digits and
  are converted to Unix epoch nanoseconds without millisecond truncation.
* Unknown symbols are counted and discarded without inventing an FPGA slot.
  Missing fields, malformed JSON, negative sizes/prices, overflow, and invalid
  timestamps are counted as malformed messages. A crossed quote is not
  rejected by the parser.

The internal FIFO is bounded by `queue_capacity`; overflow is counted and
events are dropped rather than allowing unbounded memory growth. Provider
order is preserved, and quote BID-before-ASK ordering is deterministic.

## Telemetry and latency

The source reports WebSocket frames, provider messages, quote/trade/status/
subscription counts, ignored/malformed records, unknown symbols, normalized
events, reconnects, auth/subscription failures, queue high-watermark/overflow,
last provider timestamp, and last receive steady-clock timestamp.

Provider timestamp age uses the comparable UTC system clock. Local
normalization and FPGA transport durations use the monotonic steady clock.
These are deliberately reported as separate concepts; a steady-clock value is
never subtracted from a UTC epoch timestamp.

## Build and run

The core host remains independent of networking:

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/build
./host/build/trading_engine --source synthetic --sim --events 20
```

Networking is opt-in and fails configuration if dependencies are missing:

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release \
  -DFPGA_TRADER_ENABLE_NETWORKING=ON
cmake --build host/build
./host/build/trading_engine --source alpaca --feed iex --sim \
  --symbols SPY,QQQ,NVDA,AMD --max-events 100 --max-seconds 30
```

Required Linux/Raspberry Pi packages for the networking build are:

```bash
sudo apt update
sudo apt install -y cmake build-essential libboost-system-dev \
  libssl-dev nlohmann-json3-dev ca-certificates
```

The current Windows development environment has OpenSSL but does not have
Boost.Beast or nlohmann/json, so only the dependency-free targets were run
there. No real Alpaca connectivity is claimed unless the bounded command is
run with credentials and its result is reported separately.

## Future boundary

Dynamic ticker remapping remains outside this milestone. The future sequence
is unsubscribe, disable the FPGA slot, send `CLEAR_SYMBOL_STATE`, wait for the
real FPGA ACK, assign/enable the new ticker, and subscribe it. Candidate-result
egress remains `NOT_IMPLEMENTED`; it is the next FPGA/protocol milestone.
