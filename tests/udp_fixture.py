#!/usr/bin/env python3
"""Send one ACE UDP fixture without depending on the experiment sender."""

import argparse
import socket
import struct
import time


MAGIC = 0x41434531
VERSION = 1
HEADER_SIZE = 32


def unsigned_32(value: str) -> int:
    parsed = int(value, 10)
    if not 0 <= parsed <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("must fit in uint32")
    return parsed


def unsigned_64(value: str) -> int:
    parsed = int(value, 10)
    if not 0 <= parsed <= 0xFFFFFFFFFFFFFFFF:
        raise argparse.ArgumentTypeError("must fit in uint64")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--flow-id", type=unsigned_32, required=True)
    parser.add_argument("--sequence", type=unsigned_64, required=True)
    parser.add_argument("--payload-size", type=int, required=True,
                        help="bytes following the 32-byte ACE header")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    if not 0 <= args.payload_size <= 65507 - HEADER_SIZE:
        parser.error("UDP payload is outside IPv4 limits")

    header = struct.pack(
        "!IHHIIQQ",
        MAGIC,
        VERSION,
        HEADER_SIZE,
        args.flow_id,
        0,
        args.sequence,
        time.time_ns(),
    )
    packet = header + bytes(args.payload_size)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sent = sock.sendto(packet, ("127.0.0.1", args.port))
    if sent != len(packet):
        raise RuntimeError(f"short UDP send: {sent}/{len(packet)}")


if __name__ == "__main__":
    main()
