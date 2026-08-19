#!/usr/bin/env python3
"""Test the multi-page scan path (CotScanner.scan_batch) end to end.

Load 2+ sheets in the feeder, then run (root needed for raw device access):

    sudo .venv/bin/python tools/scan_multipage.py

It scans every page in the feeder, renders each, and writes:
    captures/replays/multipage_pageNN_raw.bin   raw frame per page
    captures/replays/multipage_pageNN.png        rendered page
    captures/replays/multipage.pdf               all pages combined

Verbose per-command logging is on so the streaming batch behaviour (one SCAN,
firmware auto-feeds each sheet, page-boundary sense checkpoints) is visible.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from r10 import render                                   # noqa: E402
from r10.cot_scan import CotScanner, find_r10_slice      # noqa: E402
from r10.errors import NoPaperError, R10Error            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default=None,
                    help="R10 FAT slice (default: auto-detect ONTOUCHLITE)")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--pace", type=float, default=0.15)
    ap.add_argument("--skip-shading", action="store_true",
                    help="skip the 616 KiB shading readback (faster)")
    ap.add_argument("--format", default="pdf",
                    help="combined output format (pdf/tiff/png/jpeg)")
    ap.add_argument("--out", default="captures/replays/multipage")
    args = ap.parse_args()

    slice_dev = args.slice or find_r10_slice()
    if not slice_dev:
        print("R10 not found - is it plugged in and powered on?")
        return 1
    print(f"scanner slice: {slice_dev}\n")

    outdir = Path(args.out).parent
    outdir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(f"    {msg}")

    pages = []
    t0 = time.time()
    with CotScanner(slice_dev, pace=args.pace, skip_shading=args.skip_shading,
                    log=log) as sc:
        info = sc.device_info()
        print(f"device: {info['vendor']} {info['product']} rev {info['revision']}")
        print(f"paper present: {sc.paper_present()}\n")
        try:
            for i, raw in enumerate(sc.scan_batch(args.max_pages), 1):
                dt = time.time() - t0
                print(f"\n=== page {i} captured: {len(raw):,} bytes "
                      f"({dt:.1f}s elapsed) ===")
                rawp = Path(f"{args.out}_page{i:02d}_raw.bin")
                rawp.write_bytes(raw)
                im = render.render_page(raw)
                pages.append(im)
                pngp = Path(f"{args.out}_page{i:02d}.png")
                im.save(pngp)
                print(f"    rendered {im.size} -> {pngp}")
        except NoPaperError:
            print("feeder empty at start - load at least one sheet")
            return 1
        except R10Error as exc:
            print(f"\nscan error after {len(pages)} page(s): {exc}")
            if not pages:
                return 1

    if not pages:
        print("no pages captured")
        return 1

    data, mime, ext = render.encode(pages, args.format)
    combined = Path(f"{args.out}.{ext}")
    combined.write_bytes(data)
    print(f"\n{'='*56}")
    print(f"DONE: {len(pages)} page(s) in {time.time()-t0:.1f}s")
    print(f"combined -> {combined} ({mime}, {len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
