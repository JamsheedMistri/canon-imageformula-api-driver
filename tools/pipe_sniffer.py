#!/usr/bin/env python3
"""Ground-truth sniffer for CaptureOnTouch's device traffic (macOS, root).

CaptureOnTouch drives the R10 through the very same firmware pipe we do:
it writes a command block (SCSI CDB at offset 0x0c) to ``transfer.dat`` and
reads the response/data from ``INDATA.dat`` (docs/protocol.md 3.2-3.3). Those
are virtual firmware pipes mapped to fixed LBAs, so reading the raw CHARACTER
device (``/dev/rdiskNsM``) returns the *live* pipe contents, bypassing the OS
buffer cache - which lets us observe exactly what COT sends without touching it.

Usage:
    # 1. quit CaptureOnTouch so the device is idle
    # 2. start the sniffer (read-only; it never writes):
    sudo .venv/bin/python tools/pipe_sniffer.py --slice disk8s1
    # 3. open CaptureOnTouch and run ONE scan
    # 4. Ctrl-C the sniffer; it writes captures/traces/cot_trace.jsonl

Each distinct state of the command region and the response region is logged with
a timestamp, so the resulting trace is the exact, byte-accurate command sequence
(SET WINDOW, DEFINE SCAN MODE 0xD6, gamma SEND, SCAN, READ, ...) that we can
then replay verbatim - no parameter guessing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from r10.pipe_transport import Bpb, _fat_name, _parse_bpb  # noqa: E402

SECTOR = 512
CDB_OFF = 0x0C
STATUS_OFF = 0x18
# SCSI opcodes we know, for readable logs.
OPNAMES = {
    0x00: "TEST_UNIT_READY", 0x03: "REQUEST_SENSE", 0x12: "INQUIRY",
    0x15: "MODE_SELECT", 0x16: "RESERVE_UNIT", 0x17: "RELEASE_UNIT",
    0x1A: "MODE_SENSE", 0x1B: "SCAN", 0x24: "SET_WINDOW", 0x25: "GET_WINDOW",
    0x28: "READ", 0x2A: "SEND", 0x31: "OBJECT_POSITION", 0x34: "GET_DBS",
    0x3B: "WRITE_BUFFER/ARCH", 0x3C: "READ_BUFFER", 0xD5: "GET_MEMORY",
    0xD6: "DEFINE_SCAN_MODE", 0xE0: "GET_SCANNER_STATUS", 0xE1: "SET_ADJUST",
    0xE4: "ERROR_CLEAR", 0xEA: "INQUIRY_EX",
}


def find_pipes(fd: int) -> tuple[Bpb, int, int]:
    boot = os.pread(fd, SECTOR, 0)
    bpb = _parse_bpb(boot)
    root_off = bpb.root_dir_start_sector * bpb.bytes_per_sector
    root = os.pread(fd, bpb.root_dir_sectors * bpb.bytes_per_sector, root_off)
    found: dict[str, int] = {}
    for i in range(0, len(root), 32):
        e = root[i:i + 32]
        if not e or e[0] in (0x00, 0xE5):
            continue
        if e[11] & 0x08 or (e[11] & 0x0F) == 0x0F:
            continue
        name = _fat_name(e).upper()
        if name in ("INDATA.DAT", "TRANSFER.DAT"):
            found[name] = struct.unpack_from("<H", e, 0x1A)[0]
    tr = bpb.cluster_to_sector(found["TRANSFER.DAT"]) * bpb.bytes_per_sector
    ind = bpb.cluster_to_sector(found["INDATA.DAT"]) * bpb.bytes_per_sector
    return bpb, tr, ind


def decode_cmd(block: bytes) -> dict:
    op = block[CDB_OFF]
    status = struct.unpack_from("<I", block, STATUS_OFF)[0]
    return {
        "op": op, "op_name": OPNAMES.get(op, f"0x{op:02x}"),
        "cdb": block[CDB_OFF:CDB_OFF + 12].hex(),
        "status": f"0x{status:08x}",
        "hdr": block[0:12].hex(),
        # bytes 0x1c-0x27: transfer-length / direction fields that our
        # transport synthesizes - capture COT's exact values to diff.
        "mid": block[0x1C:0x28].hex(),
        # data-out param page (SET WINDOW / DEFINE SCAN MODE / SEND payload)
        "data_out": block[0x28:0x28 + 256].hex(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="disk8s1",
                    help="R10 FAT slice, e.g. disk8s1 (see `diskutil list`)")
    ap.add_argument("--out", default="captures/traces/cot_trace.jsonl")
    ap.add_argument("--snap", type=int, default=4096,
                    help="bytes of each pipe to watch for changes")
    ap.add_argument("--with-indata", action="store_true",
                    help="also poll INDATA (slower loop; off by default so the "
                         "hot loop catches the brief command-pending edge)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="auto-stop after N seconds (0 = until Ctrl-C)")
    args = ap.parse_args()

    slice_dev = args.slice.replace("/dev/", "").replace("rdisk", "disk")
    raw = f"/dev/r{slice_dev}"
    try:
        fd = os.open(raw, os.O_RDONLY)
    except PermissionError:
        print(f"cannot open {raw} - run with sudo", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"{raw} not found - check `diskutil list` for the R10 slice",
              file=sys.stderr)
        return 1

    bpb, tr_off, in_off = find_pipes(fd)
    print(f"sniffing {raw}: transfer.dat LBA {tr_off // SECTOR}, "
          f"INDATA.dat LBA {in_off // SECTOR}")
    print("Now run ONE scan in CaptureOnTouch. Ctrl-C when the page is done.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fout = out.open("w")
    n = ((args.snap + SECTOR - 1) // SECTOR) * SECTOR
    last_key = None
    last_in = b""
    t0 = time.time()
    events = 0
    PENDING = 0xFFFFFFFF
    poll_in = args.with_indata
    ii = 0
    try:
        while True:
            if args.duration and time.time() - t0 > args.duration:
                break
            ii += 1
            # HOT PATH: read only the first sector of transfer.dat (CDB at 0x0c,
            # status at 0x18, and the data-out page from 0x28 up to 0x1ff - big
            # enough for the 52-byte window / 40-byte adjust / 20-byte scanmode
            # pages). One small read keeps the loop fast enough to catch the
            # ~1 ms window where status==PENDING (payload complete, firmware not
            # done) - which is the only moment the data-out page is un-aliased.
            tr = os.pread(fd, SECTOR, tr_off)
            cdb = tr[CDB_OFF:CDB_OFF + 12]
            status = struct.unpack_from("<I", tr, STATUS_OFF)[0]
            data = tr[0x28:SECTOR]
            key = (cdb, status, tr[0x1C:0x28], data)
            if key != last_key:
                last_key = key
                rec = {"t": round(time.time() - t0, 4), "src": "transfer",
                       "status_raw": status, "pending": status == PENDING,
                       **decode_cmd(tr), "data_full": data.hex()}
                fout.write(json.dumps(rec) + "\n")
                events += 1
                if events & 63 == 0:
                    fout.flush()
                if status == PENDING:
                    print(f"[{rec['t']:7.3f}] CMD  {rec['op_name']:16} "
                          f"cdb={rec['cdb']} data={data[:28].hex()}")
            # INDATA only when explicitly requested, and only every 4th loop so
            # it never starves the command hot path.
            if poll_in and (ii & 3) == 0:
                ind = os.pread(fd, n, in_off)
                if ind != last_in:
                    last_in = ind
                    h = hashlib.sha1(ind).hexdigest()[:8]
                    fout.write(json.dumps({"t": round(time.time() - t0, 4),
                                           "src": "indata", "sha": h,
                                           "head": ind[:512].hex()}) + "\n")
                    events += 1
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)
        fout.close()
    print(f"\ncaptured {events} state changes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
