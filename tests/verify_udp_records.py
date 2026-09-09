#!/usr/bin/env python3
"""Validate the receiver's native-endian, fixed-size sample records."""

import argparse
import struct
from pathlib import Path


RECORD = struct.Struct("=QqqqqIIHHI")


def csv_integers(value: str) -> list[int]:
    try:
        return [int(item, 10) for item in value.split(",") if item]
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--flow-id", type=int, required=True)
    parser.add_argument("--sequences", type=csv_integers, required=True)
    parser.add_argument("--packet-length", type=int, required=True)
    args = parser.parse_args()

    contents = args.path.read_bytes()
    expected_size = len(args.sequences) * RECORD.size
    if len(contents) != expected_size:
        raise SystemExit(
            f"record file size {len(contents)}, expected {expected_size}"
        )

    records = [RECORD.unpack_from(contents, offset)
               for offset in range(0, len(contents), RECORD.size)]
    sequences = [record[0] for record in records]
    if sequences != args.sequences:
        raise SystemExit(f"sequences {sequences}, expected {args.sequences}")

    for index, record in enumerate(records):
        sequence, tx_ns, mono_ns, real_ns, hw_ns, flow_id, length, flags, reserved16, reserved32 = record
        if flow_id != args.flow_id:
            raise SystemExit(f"record {index}: flow {flow_id}, expected {args.flow_id}")
        if length != args.packet_length:
            raise SystemExit(
                f"record {index}: packet length {length}, expected {args.packet_length}"
            )
        if tx_ns <= 0 or mono_ns <= 0 or real_ns <= 0:
            raise SystemExit(f"record {index}: invalid software timestamp")
        if reserved16 != 0 or reserved32 != 0:
            raise SystemExit(f"record {index}: reserved fields are nonzero")
        if flags & ~0x3:
            raise SystemExit(f"record {index}: unknown flags {flags:#x}")
        if not flags & 0x1 and hw_ns != 0:
            raise SystemExit(f"record {index}: HW timestamp lacks valid flag")

    print(f"records: {len(records)} sample(s), flow/key/length/layout verified")


if __name__ == "__main__":
    main()
