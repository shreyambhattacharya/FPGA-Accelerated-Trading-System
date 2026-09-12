"""Integer reference model for the FPGA market-data pipeline.

The model intentionally mirrors the normalized event interface and the
registered feature record in ``market_state_engine.sv``. It uses Python
integers only: prices are unsigned micro-dollars (USD * 1_000_000), and no
floating-point operation is needed to reproduce the RTL results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Optional

PACKET_BYTES = 32
SYNC_VERSION = 0xA1

MSG_MARKET_QUOTE = 0x01
MSG_MARKET_TRADE = 0x02
MSG_CONTROL = 0x03
MSG_HEARTBEAT = 0x04
MSG_SIGNAL = 0x05
MSG_STATUS = 0x06
MSG_LOOPBACK = 0x07

STATUS_OK = 0x01
STATUS_BAD_SYNC = 0xE1
STATUS_BAD_CHECKSUM = 0xE2
STATUS_BAD_TYPE = 0xE3
STATUS_BAD_SYMBOL = 0xE4
STATUS_DUPLICATE_SEQ = 0xE5
STATUS_STALE_SEQ = 0xE6
STATUS_BAD_SIDE = 0xE7

PRICE_SCALE = 1_000_000
BPS_SCALE = 10_000
BPS_OUTPUT_SCALE = 100
IMBALANCE_FRAC_BITS = 15
SIGNED_BPS_WIDTH = 32
IMBALANCE_NORMALIZED_WIDTH = 16


def crc8(body: bytes) -> int:
    """Return CRC-8/ATM for packet bytes 0..30."""

    if len(body) != PACKET_BYTES - 1:
        raise ValueError("CRC input must contain bytes 0..30")
    crc = 0
    for value in body:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _trunc_div_signed(numerator: int, denominator: int) -> int:
    """Divide signed integers toward zero without using floating point."""

    if denominator == 0:
        raise ZeroDivisionError("signed division by zero")
    magnitude = abs(numerator) // abs(denominator)
    return -magnitude if (numerator < 0) != (denominator < 0) else magnitude


def _saturate_signed(value: int, width: int) -> int:
    minimum = -(1 << (width - 1))
    maximum = (1 << (width - 1)) - 1
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class MarketEvent:
    message_type: int
    symbol_id: int
    timestamp_ns: int
    price: int
    quantity: int
    side: int
    sequence: int
    flags: int = 0

    def packet(self, *, sync_version: int = SYNC_VERSION) -> bytes:
        """Serialize one normalized event using the wire packet layout."""

        body = struct.pack(
            ">BBHQQIBIH",
            sync_version,
            self.message_type,
            self.symbol_id,
            self.timestamp_ns,
            self.price,
            self.quantity,
            self.side,
            self.sequence,
            self.flags,
        )
        return body + bytes([crc8(body)])


def decode_event(packet: bytes) -> MarketEvent:
    """Decode the fixed 32-byte packet without applying semantic checks."""

    if len(packet) != PACKET_BYTES:
        raise ValueError("packet must be 32 bytes")
    fields = struct.unpack(">BBHQQIBIH", packet[:31])
    _, message_type, symbol_id, timestamp_ns, price, quantity, side, sequence, flags = fields
    return MarketEvent(message_type, symbol_id, timestamp_ns, price, quantity, side, sequence, flags)


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol_id: int
    sequence: int
    bid_price: int
    bid_quantity: int
    ask_price: int
    ask_quantity: int
    spread: int
    spread_valid: bool
    midpoint: int
    midpoint_valid: bool
    momentum: int
    momentum_valid: bool
    rolling_volume: int
    imbalance_numerator: int
    imbalance_denominator: int
    imbalance_valid: bool
    vwap_sum_price_quantity: int
    vwap_sum_quantity: int
    vwap_valid: bool
    vwap: int
    vwap_quotient_valid: bool
    imbalance_normalized: int
    imbalance_normalized_valid: bool
    spread_bps_x100: int
    spread_bps_x100_valid: bool
    momentum_bps_x100: int
    momentum_bps_x100_valid: bool
    midpoint_minus_vwap: int
    midpoint_minus_vwap_valid: bool
    midpoint_minus_vwap_bps_x100: int
    midpoint_minus_vwap_bps_x100_valid: bool


@dataclass(frozen=True)
class ModelOutcome:
    """One observable result emitted by the dispatcher/engine boundary."""

    kind: str
    symbol_id: int
    sequence: int
    reason: int = 0
    feature: Optional[FeatureSnapshot] = None


@dataclass
class _SymbolState:
    bid_price: int = 0
    bid_quantity: int = 0
    ask_price: int = 0
    ask_quantity: int = 0
    bid_valid: bool = False
    ask_valid: bool = False
    sequence: int = 0
    sequence_valid: bool = False
    trade_history: list[int] = field(default_factory=list)
    trade_ptr: int = 0
    trade_count: int = 0
    rolling_volume: int = 0
    midpoint_history: list[int] = field(default_factory=list)
    midpoint_ptr: int = 0
    midpoint_count: int = 0
    vwap_price_quantity_history: list[int] = field(default_factory=list)
    vwap_quantity_history: list[int] = field(default_factory=list)
    vwap_ptr: int = 0
    vwap_count: int = 0
    vwap_sum_price_quantity: int = 0
    vwap_sum_quantity: int = 0


class MarketModel:
    """Reference implementation of the parameterized market-state engine."""

    def __init__(
        self,
        num_symbols: int = 4,
        trade_window: int = 32,
        momentum_window: int = 16,
        vwap_window: int = 32,
        price_scale: int = PRICE_SCALE,
        bps_scale: int = BPS_SCALE,
        bps_output_scale: int = BPS_OUTPUT_SCALE,
        imbalance_frac_bits: int = IMBALANCE_FRAC_BITS,
    ) -> None:
        if min(num_symbols, trade_window, momentum_window, vwap_window) < 1:
            raise ValueError("all model dimensions must be positive")
        if min(price_scale, bps_scale, bps_output_scale) < 1 or imbalance_frac_bits < 0:
            raise ValueError("scaling parameters must be positive and frac bits non-negative")
        self.num_symbols = num_symbols
        self.trade_window = trade_window
        self.momentum_window = momentum_window
        self.vwap_window = vwap_window
        self.price_scale = price_scale
        self.bps_scale = bps_scale
        self.bps_output_scale = bps_output_scale
        self.imbalance_frac_bits = imbalance_frac_bits
        self.symbols = [
            _SymbolState(
                trade_history=[0] * trade_window,
                midpoint_history=[0] * momentum_window,
                vwap_price_quantity_history=[0] * vwap_window,
                vwap_quantity_history=[0] * vwap_window,
            )
            for _ in range(num_symbols)
        ]

    def process_event(self, event: MarketEvent) -> ModelOutcome:
        """Apply one normalized event and return its feature or rejection."""

        if not 0 <= event.symbol_id < self.num_symbols:
            return ModelOutcome("dispatch_error", event.symbol_id, event.sequence, STATUS_BAD_SYMBOL)
        if event.message_type not in (MSG_MARKET_QUOTE, MSG_MARKET_TRADE):
            return ModelOutcome("reject", event.symbol_id, event.sequence, STATUS_BAD_TYPE)
        if event.side not in (0, 1):
            return ModelOutcome("reject", event.symbol_id, event.sequence, STATUS_BAD_SIDE)

        state = self.symbols[event.symbol_id]
        if state.sequence_valid and event.sequence <= state.sequence:
            reason = STATUS_DUPLICATE_SEQ if event.sequence == state.sequence else STATUS_STALE_SEQ
            return ModelOutcome("reject", event.symbol_id, event.sequence, reason)

        state.sequence_valid = True
        state.sequence = event.sequence

        if event.message_type == MSG_MARKET_QUOTE:
            if event.side == 0:
                state.bid_price = event.price
                state.bid_quantity = event.quantity
                state.bid_valid = True
            else:
                state.ask_price = event.price
                state.ask_quantity = event.quantity
                state.ask_valid = True
        else:
            self._push_trade(state, event.quantity)
            self._push_vwap(state, event.price * event.quantity, event.quantity)

        return ModelOutcome("feature", event.symbol_id, event.sequence, feature=self._feature(state, event))

    def process_packet(self, packet: bytes) -> Optional[ModelOutcome]:
        """Process only market packets; loopback/status traffic returns None.

        Bad-sync and bad-CRC packets are intentionally left to the existing
        loopback status path, just as they are in the dispatcher RTL.
        """

        if len(packet) != PACKET_BYTES:
            raise ValueError("packet must be 32 bytes")
        if packet == bytes(PACKET_BYTES):
            return None
        event = decode_event(packet)
        if packet[0] != SYNC_VERSION or packet[31] != crc8(packet[:31]):
            return None
        if event.message_type not in (MSG_MARKET_QUOTE, MSG_MARKET_TRADE):
            return None
        return self.process_event(event)

    def _push_trade(self, state: _SymbolState, quantity: int) -> None:
        if state.trade_count >= self.trade_window:
            state.rolling_volume -= state.trade_history[state.trade_ptr]
        else:
            state.trade_count += 1
        state.trade_history[state.trade_ptr] = quantity
        state.rolling_volume += quantity
        state.trade_ptr = (state.trade_ptr + 1) % self.trade_window

    def _push_vwap(self, state: _SymbolState, price_quantity: int, quantity: int) -> None:
        if state.vwap_count >= self.vwap_window:
            state.vwap_sum_price_quantity -= state.vwap_price_quantity_history[state.vwap_ptr]
            state.vwap_sum_quantity -= state.vwap_quantity_history[state.vwap_ptr]
        else:
            state.vwap_count += 1
        state.vwap_price_quantity_history[state.vwap_ptr] = price_quantity
        state.vwap_quantity_history[state.vwap_ptr] = quantity
        state.vwap_sum_price_quantity += price_quantity
        state.vwap_sum_quantity += quantity
        state.vwap_ptr = (state.vwap_ptr + 1) % self.vwap_window

    def _feature(self, state: _SymbolState, event: MarketEvent) -> FeatureSnapshot:
        quotes_valid = state.bid_valid and state.ask_valid and state.ask_price >= state.bid_price
        vwap_valid = state.vwap_sum_quantity != 0
        vwap = state.vwap_sum_price_quantity // state.vwap_sum_quantity if vwap_valid else 0

        if quotes_valid:
            spread = state.ask_price - state.bid_price
            midpoint = (state.bid_price + state.ask_price) // 2
            imbalance_numerator = state.bid_quantity - state.ask_quantity
            imbalance_denominator = state.bid_quantity + state.ask_quantity
            momentum_valid = state.midpoint_count >= self.momentum_window
            reference_midpoint = state.midpoint_history[state.midpoint_ptr] if momentum_valid else 0
            momentum = midpoint - reference_midpoint if momentum_valid else 0
            state.midpoint_history[state.midpoint_ptr] = midpoint
            state.midpoint_ptr = (state.midpoint_ptr + 1) % self.momentum_window
            state.midpoint_count = min(state.midpoint_count + 1, self.momentum_window)
        else:
            spread = 0
            midpoint = 0
            momentum = 0
            momentum_valid = False
            reference_midpoint = 0
            imbalance_numerator = 0
            imbalance_denominator = 0

        imbalance_normalized_valid = quotes_valid and imbalance_denominator != 0
        imbalance_normalized = (
            _saturate_signed(
                _trunc_div_signed(
                    imbalance_numerator * (1 << self.imbalance_frac_bits),
                    imbalance_denominator,
                ),
                IMBALANCE_NORMALIZED_WIDTH,
            )
            if imbalance_normalized_valid
            else 0
        )

        spread_bps_x100_valid = quotes_valid and midpoint != 0
        spread_bps_x100 = (
            _saturate_signed(
                spread * self.bps_scale * self.bps_output_scale // midpoint,
                SIGNED_BPS_WIDTH,
            )
            if spread_bps_x100_valid
            else 0
        )

        momentum_bps_x100_valid = momentum_valid and reference_midpoint != 0
        momentum_bps_x100 = (
            _saturate_signed(
                _trunc_div_signed(
                    momentum * self.bps_scale * self.bps_output_scale,
                    reference_midpoint,
                ),
                SIGNED_BPS_WIDTH,
            )
            if momentum_bps_x100_valid
            else 0
        )

        midpoint_minus_vwap_valid = quotes_valid and vwap_valid
        midpoint_minus_vwap = midpoint - vwap if midpoint_minus_vwap_valid else 0
        midpoint_minus_vwap_bps_x100_valid = midpoint_minus_vwap_valid and vwap != 0
        midpoint_minus_vwap_bps_x100 = (
            _saturate_signed(
                _trunc_div_signed(
                    midpoint_minus_vwap * self.bps_scale * self.bps_output_scale,
                    vwap,
                ),
                SIGNED_BPS_WIDTH,
            )
            if midpoint_minus_vwap_bps_x100_valid
            else 0
        )

        return FeatureSnapshot(
            symbol_id=event.symbol_id,
            sequence=event.sequence,
            bid_price=state.bid_price,
            bid_quantity=state.bid_quantity,
            ask_price=state.ask_price,
            ask_quantity=state.ask_quantity,
            spread=spread,
            spread_valid=quotes_valid,
            midpoint=midpoint,
            midpoint_valid=quotes_valid,
            momentum=momentum,
            momentum_valid=momentum_valid,
            rolling_volume=state.rolling_volume,
            imbalance_numerator=imbalance_numerator,
            imbalance_denominator=imbalance_denominator,
            imbalance_valid=quotes_valid,
            vwap_sum_price_quantity=state.vwap_sum_price_quantity,
            vwap_sum_quantity=state.vwap_sum_quantity,
            vwap_valid=vwap_valid,
            vwap=vwap,
            vwap_quotient_valid=vwap_valid,
            imbalance_normalized=imbalance_normalized,
            imbalance_normalized_valid=imbalance_normalized_valid,
            spread_bps_x100=spread_bps_x100,
            spread_bps_x100_valid=spread_bps_x100_valid,
            momentum_bps_x100=momentum_bps_x100,
            momentum_bps_x100_valid=momentum_bps_x100_valid,
            midpoint_minus_vwap=midpoint_minus_vwap,
            midpoint_minus_vwap_valid=midpoint_minus_vwap_valid,
            midpoint_minus_vwap_bps_x100=midpoint_minus_vwap_bps_x100,
            midpoint_minus_vwap_bps_x100_valid=midpoint_minus_vwap_bps_x100_valid,
        )


def make_packet(
    *,
    message_type: int,
    symbol_id: int,
    timestamp_ns: int,
    price: int,
    quantity: int,
    side: int,
    sequence: int,
    flags: int = 0,
    sync_version: int = SYNC_VERSION,
) -> bytes:
    """Convenience wrapper used by the deterministic generator."""

    return MarketEvent(
        message_type,
        symbol_id,
        timestamp_ns,
        price,
        quantity,
        side,
        sequence,
        flags,
    ).packet(sync_version=sync_version)


if __name__ == "__main__":
    sample = make_packet(
        message_type=MSG_MARKET_QUOTE,
        symbol_id=0,
        timestamp_ns=1,
        price=100_000_000,
        quantity=42,
        side=0,
        sequence=1,
    )
    print(sample.hex())
