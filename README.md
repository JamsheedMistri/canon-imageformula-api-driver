# canon-imageformula-api-driver

A userspace driver and HTTP API for the **Canon imageFORMULA R10** portable
document scanner.

The R10 has no TWAIN/WIA/SANE driver and no SDK: it ships as a USB Mass Storage
device whose bundled *CaptureOnTouch Lite* app drives scanning through
vendor-specific SCSI commands tunneled over two files on its own mounted
volume. This project reimplements that protocol in userspace so you can control
the scanner like an API - from Python or a small HTTP service.

> Status: **working, CaptureOnTouch-exact quality.** The scan path replays
> the byte-for-byte command choreography captured from a real CaptureOnTouch
> scan (feed, 9 stationary AGC calibration cycles against the internal
> dark/white references, shading readback, slow-feed 8-bit document pass) and
> renders with the reimplemented COT tone pipeline. See `docs/protocol.md`
> 6.7.2 for the verified protocol.

## How it works

```
HTTP client -> service/app.py (FastAPI) -> r10.cot_scan.CotScanner
            -> raw FAT pipe (transfer.dat / INDATA.dat on /dev/rdiskNs1)
            -> Canon R10 firmware (vendor SCSI over the file tunnel)
            -> r10.render (decode 600x300 RGB frame -> COT tone pipeline)
```

macOS claims the R10 as mass storage, so instead of fighting the kernel for
the USB interface, commands are exchanged through the scanner's own
file-tunneling protocol: SCSI CDBs written to `transfer.dat`, responses read
from `INDATA.dat`, at raw block-device level. See `docs/protocol.md` for the
full reverse-engineering notes.

## Scanning API (the product)

```bash
# root needed for raw block-device access; the R10 slice is auto-detected
sudo .venv/bin/python service/app.py            # -> http://127.0.0.1:8080
```

```bash
curl -X POST 'http://127.0.0.1:8080/scan' -o scan.pdf            # all pages -> 1 PDF
curl -X POST 'http://127.0.0.1:8080/scan?format=png' -o scan.png # single page
curl -X POST 'http://127.0.0.1:8080/scan?format=jpeg&quality=90&max_pages=3' -o out
curl 'http://127.0.0.1:8080/status'                              # paper / identity
curl -X POST 'http://127.0.0.1:8080/release'   # remount volume for CaptureOnTouch
```

- `POST /scan` scans **every page in the feeder** (up to `max_pages`, feeder
  holds 10) and returns the document in the response body. `pdf` (default)
  and `tiff` combine all pages into one file; `png`/`jpeg` return the bare
  image for one page or a ZIP of `page_NN` files for several.
- `GET /status` reports device identity and whether paper is loaded.
- `POST /release` remounts the volume so CaptureOnTouch can be used; the next
  API request reclaims the device automatically.
- Interactive docs at `http://127.0.0.1:8080/docs` (OpenAPI).

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Library usage

```python
from r10.cot_scan import CotScanner, find_r10_slice
from r10 import render

# root required (raw block-device access)
with CotScanner(find_r10_slice()) as sc:
    pages = [render.render_page(raw) for raw in sc.scan_batch(max_pages=10)]

data, mime, ext = render.encode(pages, "pdf")   # or "png" / "jpeg" / "tiff"
open(f"scan.{ext}", "wb").write(data)
```

The scanner needs raw block-device access, so run scripts with `sudo` (e.g.
`sudo .venv/bin/python your_script.py`). The R10's disk slice is auto-detected
via `find_r10_slice()`.

## Platform notes

Currently macOS-only: the transport talks to the scanner's file tunnel through
the raw block device (`/dev/rdiskNs1`) and uses `diskutil` to unmount the
auto-mounted volume. Porting to Linux means swapping those two pieces (raw
device path + unmount call) in `src/r10/pipe_transport.py`.

## Development

```bash
pip install -e ".[dev]"
pytest                            # choreography-integrity + render/encode tests
```

Hardware scans require the physical device and root, so they are run manually
(see the library usage above); the unit tests do not touch hardware.

## Legal / caveats

This is an unofficial, independent reimplementation created for
**interoperability and research**. It is **not affiliated with, authorized by,
or endorsed by Canon**. "Canon", "imageFORMULA", and "CaptureOnTouch" are
trademarks of Canon Inc., used here only nominatively to describe the hardware
this software interoperates with; all trademarks belong to their respective
owners. No Canon software or firmware is included or redistributed. Provided
"as is", without warranty; may break with firmware changes. Use at your own
risk and in accordance with the laws and agreements that apply to you.
