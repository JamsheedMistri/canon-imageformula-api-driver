#!/usr/bin/env python3
"""Replay CaptureOnTouch's EXACT scan sequence, byte-for-byte, from a sniffer
trace (captures/traces/cot_trace.jsonl produced by tools/pipe_sniffer.py).

The captured choreography (real COT scan, 2026-08-17 trace) has FOUR phases:

  1. OBJECT_POSITION 3101 feed.
  2. ~9 closed-loop AGC calibration cycles, each: READ 0x8c (measure) ->
     SET_ADJUST 0xE1 (gains evolve 0x80 -> 0x95/0x97 + 0x1fff white targets) ->
     SET_WINDOW x2 (window id 0 front / 1 back, 300 dpi, comp 0x05, 12-bit) ->
     DEFINE_SCAN_MODE pages 0e32/0e36 -> SCAN (windows 00 01) ->
     READ one 183744-byte band -> OBJECT_POSITION 3100 (step).
  3. 77x READ 0x3B of 8 KiB from firmware 0x10080000 (shading/correction
     tables) - saved for host-side use.
  4. Final scan: SET_WINDOW x2 (comp 0x05, 8-bit, ULy = -472 pre-roll,
     length 0x3a7e) -> DEFINE_SCAN_MODE 300e/320e-60/360e -> SCAN 00 01 ->
     READ 1 MiB chunks until end-of-scan sense -> REQUEST_SENSE + status reads.

This tool reconstructs every command ISSUANCE from the trace - a new CDB, or a
distinct fresh data-out payload under an unchanged CDB (that is how the two
SET_WINDOWs and three DEFINE pages appear) - and re-issues them verbatim.
The only constant not lifted straight from a captured byte is SCAN's 2-byte
window list 00 01, whose clean capture appears at the final SCAN (t=32.410);
during cycles the firmware overwrites that slot with measured values before we
can sample it.

Usage:
    sudo .venv/bin/python tools/cot_replay.py --slice disk8s1 \
        --trace captures/traces/cot_trace.jsonl --out captures/replays/cot_replay
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from r10.pipe_transport import RawPipeTransport  # noqa: E402
from r10.cot_scan import _is_end_sense, scsi_status  # noqa: E402


def request_sense(pipe) -> tuple:
    st, data = pipe.exec_read(bytes([0x03, 0, 0, 0, 0x12, 0]), 0x12)
    if len(data) >= 14:
        return data[2] & 0x0F, data[12], data[13], data
    return None, None, None, data

# data-out length per opcode, from the captured CDBs:
#   0x24 SET_WINDOW  cdb[8]=0x34, 0xE1 SET_ADJUST cdb[8]=0x28,
#   0xD6 DEFINE_SCAN_MODE cdb[4]=0x14, 0x1B SCAN cdb[4]=0x02.
def dataout_len(cdb: bytes) -> int:
    if cdb[0] in (0x24, 0xE1):
        return cdb[8]
    if cdb[0] in (0xD6, 0x1B):
        return cdb[4]
    return 0


def read_len(cdb: bytes) -> int:
    return (cdb[6] << 16) | (cdb[7] << 8) | cdb[8]


def load_issuances(trace: Path) -> list[dict]:
    """Rebuild the ordered command-issuance list for one scan.

    The sniffer logs every distinct (cdb, status, data[0x28:0x200]) state of
    transfer.dat's command sector. Consecutive states sharing a CDB form an
    occurrence; within it, each data state that differs from the occurrence's
    initial (stale) data is one fresh payload write = one issuance. Commands
    with no data-out yield one issuance per occurrence."""
    rows = [json.loads(l) for l in trace.open()
            if '"src": "transfer"' in l or '"transfer"' in l]
    rows = [r for r in rows if r.get("src") == "transfer"]
    if not rows:
        raise SystemExit("empty trace")

    occs: list[dict] = []
    for r in rows:
        if not occs or occs[-1]["cdb"] != r["cdb"]:
            occs.append({"cdb": r["cdb"], "op_name": r["op_name"],
                         "t": r["t"], "states": []})
        occs[-1]["states"].append(r)

    feed = next((i for i, o in enumerate(occs)
                 if o["cdb"].startswith("3101")), None)
    if feed is None:
        raise SystemExit("no OBJECT_POSITION feed (3101) in trace - did the "
                         "scan run while the sniffer was active?")
    # COT issues INQUIRY / TUR / status / calibration-page reads (0x84, 0x8b,
    # 0x8c page 1 + page 0) between its startup archive dump and the feed.
    # Vendor reads can arm firmware state, so include that whole pre-feed
    # tail verbatim (everything after the last 0x3B / REQUEST_SENSE).
    tail_start = 0
    for i in range(feed - 1, -1, -1):
        if occs[i]["cdb"].startswith(("3b", "03")):
            tail_start = i + 1
            break
    # phase ends when idle polling (INQUIRY / TEST_UNIT_READY) resumes
    # after the feed
    end = next((i for i in range(feed + 1, len(occs))
                if occs[i]["cdb"].startswith(("12", "00"))), len(occs))
    feed = tail_start

    issuances: list[dict] = []
    for o in occs[feed:end]:
        cdb = bytes.fromhex(o["cdb"])
        plen = dataout_len(cdb)
        # NOTE: SCAN's 2-byte window list is NOT constant. Calibration cycles
        # scan the internal reference windows - 0xff,0xff (dark) or 0xfe,0xfe
        # (white) - which do NOT move the paper; only the final scan uses the
        # real document windows 00,01, which starts the feed. Use the fresh
        # captured payload per occurrence like every other data-out command.
        if not plen:
            issuances.append({**o, "payload": None})
            continue
        base = o["states"][0]["data_full"]
        fresh: list[str] = []
        for s in o["states"][1:]:
            d = s["data_full"]
            if d != base and (not fresh or d != fresh[-1]):
                fresh.append(d)
        if not fresh:  # payload write landed before the CDB was first sampled
            fresh = [o["states"][-1]["data_full"]]
        for d in fresh:
            issuances.append({**o, "payload": bytes.fromhex(d)[:plen]})
    return issuances


def clear_unit_attention(pipe) -> None:
    for _ in range(3):
        st = pipe.exec_none(bytes(12))  # TEST UNIT READY
        if scsi_status(st) == 0:
            return
        request_sense(pipe)


def replay(pipe, seq: list[dict], out_prefix: str, *,
           no_step: bool = False, pace: float = 0.02) -> None:
    image = bytearray()
    bands = bytearray()
    shading = bytearray()
    drained = False
    for r in seq:
        cdb = bytes.fromhex(r["cdb"])
        op, name = cdb[0], r["op_name"]
        if no_step and r["cdb"].startswith("3100"):
            # Diagnostic: COT's paper sits still through all calibration
            # cycles, yet in our replay the page is gone by cycle 2. Skipping
            # the inter-cycle OBJECT_POSITION 3100 isolates whether that
            # command releases/ejects the sheet when we issue it.
            print("  OBJECT_POSITION  3100 SKIPPED (--no-step)")
            continue
        if op == 0x03:
            key, asc, ascq, _ = request_sense(pipe)
            print(f"  REQUEST_SENSE    -> key={key} asc={asc} ascq={ascq}")
        elif op == 0x12:
            st, data = pipe.exec_read(cdb, cdb[4])
            print(f"  INQUIRY          cdb={cdb.hex()} -> st=0x{st:08x} "
                  f"data[:16]={data[:16].hex()}")
        elif op == 0x31:
            st = pipe.exec_none(cdb)
            print(f"  {name:16} cdb={cdb.hex()} -> st=0x{st:08x}")
            if scsi_status(st) != 0:
                key, asc, ascq, sense = request_sense(pipe)
                print(f"    sense key={key} asc={asc} ascq={ascq} "
                      f"raw={sense[:14].hex()}")
                if cdb[1] == 0x01:
                    raise SystemExit("feed failed - is a page loaded in the "
                                     "feeder?")
        elif op == 0x3B:
            n = read_len(cdb)
            st, data = pipe.exec_read(cdb, n)
            shading.extend(data)
            if len(shading) in (n, 20 * n) or cdb[4] == 0xFE:
                print(f"  SHADING READ     off=0x{cdb[2]:02x}{cdb[3]:02x}"
                      f"{cdb[4]:02x}{cdb[5]:02x} +{n}B st=0x{st:08x}")
        elif op == 0x28 and cdb[2] == 0x00:
            band = read_len(cdb)
            if band >= 0x100000:
                if drained:
                    continue
                print(f"  final image drain, {band}B chunks ...")
                got = _drain_image(pipe, cdb, band, image)
                drained = True
                print(f"  image total {got} bytes")
            else:
                # NOTE: no clear_data - COT never writes INDATA.dat, and the
                # firmware sees every host sector write; stay byte-faithful.
                st, data = pipe.exec_read(cdb, band)
                sense = ""
                if scsi_status(st) != 0:
                    key, asc, ascq, _ = request_sense(pipe)
                    sense = f" sense={key}/{asc:#x}/{ascq:#x}"
                bands.extend(data)
                print(f"  calib band READ  {band}B st=0x{st:08x} "
                      f"head={data[:8].hex()}{sense}")
        elif op == 0x28:
            n = read_len(cdb) or 0x80
            st, data = pipe.exec_read(cdb, n)
            print(f"  {name:16} cdb={cdb.hex()} -> st=0x{st:08x} "
                  f"data[:16]={data[:16].hex()}")
        elif r["payload"] is not None:
            st = pipe.exec_write(cdb, r["payload"])
            sense = ""
            if scsi_status(st) != 0:
                key, asc, ascq, _ = request_sense(pipe)
                sense = f" sense={key}/{asc:#x}/{ascq:#x}"
            print(f"  {name:16} data={r['payload'].hex()} -> "
                  f"st=0x{st:08x}{sense}")
        else:
            st = pipe.exec_none(cdb)
            sense = ""
            if scsi_status(st) != 0:
                key, asc, ascq, _ = request_sense(pipe)
                sense = f" sense={key}/{asc:#x}/{ascq:#x}"
            print(f"  {name:16} cdb={cdb.hex()} -> st=0x{st:08x}{sense}")
        time.sleep(pace)

    Path(out_prefix).parent.mkdir(parents=True, exist_ok=True)
    for suffix, buf in (("_raw.bin", image), ("_calib_bands.bin", bands),
                        ("_shading.bin", shading)):
        p = Path(out_prefix + suffix)
        p.write_bytes(bytes(buf))
        print(f"saved {len(buf):>9} bytes -> {p}")


def _drain_image(pipe, cdb: bytes, band: int, image: bytearray) -> int:
    total = 0
    for i in range(1, 20000):
        st, data = pipe.exec_read(cdb, band)
        stat = scsi_status(st)
        nz = len(data.rstrip(b"\x00"))
        if stat != 0:
            key, asc, ascq, sense = request_sense(pipe)
            end = _is_end_sense(sense)
            valid = nz
            if key is not None and (sense[2] & 0x20) and (sense[0] & 0x80):
                residue = int.from_bytes(sense[3:7], "big")
                if 0 <= residue <= band:
                    valid = band - residue
            image.extend(data[:valid])
            total += valid
            print(f"    READ #{i} st=0x{st:08x} valid={valid} END={end}")
            if end:
                break
        else:
            image.extend(data)
            total += len(data)
            print(f"    READ #{i} st=0x{st:08x} full band")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="disk8s1")
    ap.add_argument("--trace", default="captures/traces/cot_trace.jsonl")
    ap.add_argument("--out", default="captures/replays/cot_replay")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the reconstructed issuance list and exit")
    ap.add_argument("--no-step", action="store_true",
                    help="skip inter-cycle OBJECT_POSITION 3100 commands "
                         "(diagnostic: suspected paper-release)")
    ap.add_argument("--pace", type=float, default=0.02,
                    help="seconds to sleep between commands (COT paces its "
                         "cycles ~0.15 s apart)")
    args = ap.parse_args()

    seq = load_issuances(Path(args.trace))
    print(f"replaying {len(seq)} issuances from {args.trace}:")
    n3b = sum(1 for r in seq if r["cdb"].startswith("3b"))
    for r in seq:
        if r["cdb"].startswith("3b"):
            continue
        pl = r["payload"].hex() if r.get("payload") else ""
        print(f"  [{r['t']:8.3f}] {r['op_name']:16} {r['cdb']}  {pl}")
    print(f"  (+ {n3b} shading 0x3B reads elided from listing)\n")
    if args.dry_run:
        return 0
    # paper-motion commands (OBJECT_POSITION steps, the final drain) can hold
    # the pipe pending far longer than the 8 s transport default.
    with RawPipeTransport(args.slice, timeout_s=90.0) as pipe:
        clear_unit_attention(pipe)
        replay(pipe, seq, args.out, no_step=args.no_step, pace=args.pace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
