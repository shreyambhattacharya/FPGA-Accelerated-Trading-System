"""Independent, dependency-free reference for the 32-byte SPI packet."""

from __future__ import annotations

import struct

PACKET_BYTES = 32
SYNC_VERSION = 0xA1
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


def crc8(packet_without_checksum: bytes) -> int:
    if len(packet_without_checksum) != PACKET_BYTES - 1:
        raise ValueError("CRC input must contain bytes 0..30")
    crc = 0
    for value in packet_without_checksum:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def pack(*, message_type: int, symbol_id: int, timestamp_ns: int, data: int,
         quantity: int, side_status: int, sequence: int, flags: int,
         sync_version: int = SYNC_VERSION) -> bytes:
    body = struct.pack(
        ">BBHQQIBIH",
        sync_version, message_type, symbol_id, timestamp_ns, data,
        quantity, side_status, sequence, flags,
    )
    assert len(body) == PACKET_BYTES - 1
    return body + bytes([crc8(body)])


def status_for(raw: bytes) -> int:
    if len(raw) != PACKET_BYTES:
        raise ValueError("packet must be 32 bytes")
    if raw[0] != SYNC_VERSION:
        return STATUS_BAD_SYNC
    if raw[31] != crc8(raw[:31]):
        return STATUS_BAD_CHECKSUM
    if raw[1] != MSG_LOOPBACK:
        return STATUS_BAD_TYPE
    return STATUS_OK


if __name__ == "__main__":
    sample = pack(
        message_type=MSG_LOOPBACK,
        symbol_id=0x1234,
        timestamp_ns=0x0102030405060708,
        data=0xD00D000000000000,
        quantity=42,
        side_status=0,
        sequence=0,
        flags=0,
    )
    print(sample.hex())
    print(f"crc=0x{sample[-1]:02x} status=0x{status_for(sample):02x}")
