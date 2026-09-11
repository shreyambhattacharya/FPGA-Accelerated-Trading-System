`ifndef PROTOCOL_DEFS_SVH
`define PROTOCOL_DEFS_SVH

`define PACKET_BYTES 32
`define PACKET_BITS  256

`define PROTOCOL_SYNC_VERSION 8'hA1

`define MSG_MARKET_QUOTE 8'h01
`define MSG_MARKET_TRADE 8'h02
`define MSG_CONTROL      8'h03
`define MSG_HEARTBEAT    8'h04
`define MSG_SIGNAL       8'h05
`define MSG_STATUS       8'h06
`define MSG_LOOPBACK     8'h07

`define STATUS_OK             8'h01
`define STATUS_BAD_SYNC       8'hE1
`define STATUS_BAD_CHECKSUM   8'hE2
`define STATUS_BAD_TYPE       8'hE3

`endif
