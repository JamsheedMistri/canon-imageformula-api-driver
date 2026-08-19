#!/usr/bin/env python3
"""Probe the R10 over the confirmed filesystem pipe (read-only commands).

Runs a battery of safe SCSI-2 scanner *read* commands and dumps the responses,
so we can learn the device's real window-descriptor layout, mode/geometry
defaults, and buffer-status format before attempting a scan. One sudo run.

    sudo .venv/bin/python tools/pipe_probe.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from r10.pipe_transport import RawPipeTransport  # noqa: E402


def hexdump(label: str, data: bytes, n: int = 128) -> None:
    print(f"--- {label} ({len(data)} bytes) ---")
    for off in range(0, min(len(data), n), 16):
        chunk = data[off:off + 16]
        h = chunk.hex(" ")
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {off:04x}  {h:<48}  {a}")


# name, cdb, data_len (0 => no data phase)
PROBES = [
    ("TEST UNIT READY", bytes([0x00, 0, 0, 0, 0, 0]), 0),
    ("REQUEST SENSE", bytes([0x03, 0, 0, 0, 0x12, 0]), 0x12),
    ("INQUIRY (std)", bytes([0x12, 0, 0, 0, 0x24, 0]), 0x24),
    ("INQUIRY (alloc 0x60)", bytes([0x12, 0, 0, 0, 0x60, 0]), 0x60),
    # MODE SENSE(6): PC=0, page code 0x3f (all pages), alloc 0xff
    ("MODE SENSE(6) all", bytes([0x1A, 0, 0x3F, 0, 0xFF, 0]), 0xFF),
    # GET WINDOW (0x25): 10-byte CDB, allocation length in bytes 6-8 (BE)
    ("GET WINDOW", bytes([0x25, 0, 0, 0, 0, 0, 0x00, 0x01, 0x00, 0]), 0x100),
    # GET DATA BUFFER STATUS (0x34): alloc length bytes 7-8
    ("GET DATA BUFFER STATUS", bytes([0x34, 0, 0, 0, 0, 0, 0, 0x00, 0x40, 0]), 0x40),
]


def main() -> int:
    slice_dev = sys.argv[1] if len(sys.argv) > 1 else "disk8s1"
    with RawPipeTransport(slice_dev) as pipe:
        print(f"pipe open: INDATA.dat LBA {pipe.indata_lba}, "
              f"transfer.dat LBA {pipe.transfer_lba}\n")
        for name, cdb, dlen in PROBES:
            try:
                if dlen == 0:
                    st = pipe.exec_none(cdb)
                    print(f"[{name}] cdb={cdb.hex()} -> status=0x{st:08x} (no data)")
                else:
                    pipe.clear_data(dlen)
                    st, data = pipe.exec_read(cdb, dlen)
                    print(f"[{name}] cdb={cdb.hex()} -> status=0x{st:08x}")
                    if any(data):
                        hexdump(name, data)
                    else:
                        print("   (all-zero response)")
            except Exception as exc:
                print(f"[{name}] cdb={cdb.hex()} -> ERROR: {exc}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
