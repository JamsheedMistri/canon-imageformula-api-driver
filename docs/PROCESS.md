# How we built a CaptureOnTouch-quality driver for the Canon R10

This is the end-to-end story of reverse-engineering the Canon imageFORMULA R10
and building an HTTP scanning API that produces output indistinguishable from
Canon's own CaptureOnTouch. It is written for someone picking this up cold: it
covers the goal, every approach we tried (including the dead ends and *why*
they were dead ends), the breakthroughs, the final architecture, and how to
extend it.

For the byte-level protocol facts, see `docs/protocol.md`. This document is the
"why" and the map; that one is the "what".

---

## 1. The problem

The Canon imageFORMULA R10 is a "plug-and-scan" portable duplex scanner. Unlike
most scanners it has:

- **No SDK.** Canon never shipped a developer API for it.
- **No TWAIN / WIA / SANE driver.** Nothing standard can talk to it.
- **No normal USB scanner interface.** It enumerates as a **USB Mass Storage
  device** (VID `0x1083`, PID `0x167f`). When you plug it in, a small FAT
  volume named `ONTOUCHLITE` mounts, containing a bundled `CaptureOnTouch Lite`
  app. You run that app; it drives the scan and hands you a file.

So the only "API" is: mount a fake disk, run Canon's app. Our goal was to drive
the scanner **directly from our own code**, as an API, at the **exact same
quality** as CaptureOnTouch (COT) - not "good enough", pixel-for-pixel COT.

---

## 2. How the scanner actually talks

The single most important discovery, and the thing every later step depends on:

**The R10 is controlled by a file-tunneling protocol over its own fake disk.**

Canon's app does not use a custom USB interface. Instead, the FAT volume
contains two special 2 MB files:

- `transfer.dat` - the **command pipe**. You write a SCSI command block here.
- `INDATA.dat` - the **data pipe**. The firmware writes responses/data here.

The firmware watches the raw sectors backing those files. The handshake
(recovered by disassembling `ONTOUCHL.exe`, `docs/protocol.md` §3.2) is:

1. Write a 12-byte SCSI CDB (plus any data-out payload) into `transfer.dat` at
   a fixed offset, and set the status word at `0x18` to `0xFFFFFFFF` (PENDING).
2. Poll that status word. The firmware flips it to a SCSI status code when the
   command completes.
3. Read the result from `INDATA.dat` (for data-in commands).

The commands themselves are a **vendor SCSI-2 scanner command set**:
`INQUIRY`, `SET WINDOW (0x24)`, `SCAN (0x1B)`, `READ (0x28)`,
`OBJECT_POSITION (0x31)`, plus vendor opcodes `SET_ADJUST (0xE1)`,
`DEFINE_SCAN_MODE (0xD6)`, and archive reads (`0x3B`).

This means we never needed a USB driver at all. We talk to the scanner by
reading and writing raw sectors of a mounted disk.

---

## 3. The approaches, in order

### 3.1 Dead end: direct USB / Bulk-Only Transport (removed)

Our first instinct was the "proper" way: claim the USB interface with libusb
and speak USB Mass Storage Bulk-Only Transport (BOT) - CBW command blocks out,
data, CSW status in. We built a full stack: `usb_transport.py`, `bot.py`,
`scsi.py`, a `Scanner` class, a CLI, and a mock transport with unit tests.

It half-worked - we could enumerate, reset, and read the volume - but it kept
**desyncing the firmware's mass-storage state machine**. macOS aggressively
claims the device as a disk, and every time we stole the interface with libusb
and re-enumerated, the firmware got a little more confused: `READ CAPACITY`
returned garbage, pipes stalled, and we had to power-cycle between runs.

**Why it's a dead end:** fighting the OS for a device it considers a disk is a
losing battle on macOS, and the firmware clearly expects the file-tunnel
handshake, not raw interface access. This code (the USB transport, BOT framing,
and SCSI builders) has since been removed; the SCSI command vocabulary it
encoded is documented in `docs/protocol.md` §5.

### 3.2 The unlock: raw-sector file tunnel (`src/r10/pipe_transport.py`)

Instead of claiming the USB interface, we:

1. Unmount the volume (but keep the raw block device `/dev/rdiskNs1`).
2. Parse the FAT16 filesystem ourselves to find the physical LBAs of
   `transfer.dat` and `INDATA.dat`.
3. Do the command handshake by `pread`/`pwrite` on those exact sectors.

This is `RawPipeTransport`. It needs `sudo` (raw device access) but it is
rock-solid: no interface stealing, no re-enumeration, no state-machine
corruption. **This is the foundation of everything that works.**

### 3.3 First real scans - and the quality wall

With the pipe working we could issue `SET WINDOW` -> `SCAN` -> `READ` and get a
page out. But the quality was unacceptable: grainy, dark, streaky, axes
swapped, sometimes duplicated. We spent a long stretch here guessing
parameters - resolution, bit depth, gain, LED, color composition - and running
the page through repeatedly. We reverse-engineered a lot of real facts this way
(`docs/protocol.md` §6.1-6.6):

- The image comes back as interleaved segments, not a plain raster.
- The scanner has front and back sensors; composition modes control how their
  data is laid out per line.
- Low sensor gain plus no shading/gamma correction was making everything
  grainy and flat.
- We even loaded Canon's own image-processing library (`LLiPmDRP215`) via a
  `ctypes` bridge under Rosetta to run its exact `RemoveShadow` /
  `makeGammaDataforFC` routines (§6.6).

But we kept plateauing below COT quality. **The lesson: guessing acquisition
parameters cannot win.** COT does a precise, closed-loop calibration before
every scan, and no amount of host-side post-processing recovers what a
mis-calibrated capture threw away.

### 3.4 The breakthrough: sniff COT, then replay it byte-for-byte

We stopped guessing and captured the ground truth. Two tools:

- **`tools/pipe_sniffer.py`** watches the raw command sector while *Canon's own
  app* runs a scan, logging every distinct `(CDB, status, data-out payload)`
  state - including the brief `PENDING` edge where the payload is complete but
  the firmware hasn't overwritten it yet. That edge is the only moment the
  command parameters are un-aliased, so catching it was essential. Its output
  is a JSON-lines trace of the scan.

- **`tools/cot_replay.py`** parses that trace, reconstructs every command
  *issuance* (a new CDB, or a fresh data-out payload under an unchanged CDB -
  that is how the two `SET_WINDOW`s and three `DEFINE_SCAN_MODE` pages appear),
  and re-issues them verbatim over our pipe.

This is what finally produced a pixel-for-pixel COT-quality capture. Getting
the replay faithful took several corrections, each a real lesson (see §4).

---

## 4. What the trace taught us (and the bugs we fixed)

Replaying COT exactly forced us to understand its choreography completely. The
full sequence is documented in `docs/protocol.md` §6.7.2; the key realizations:

### The scan has four phases

1. **Feed** (`OBJECT_POSITION 31 01`). Counter-intuitively this does *not* move
   the paper to the sensor - the sheet sits still through all of phase 2.
2. **~9 closed-loop AGC calibration cycles**, paper stationary. Each cycle:
   `SET_ADJUST 0xE1` (gains/offsets evolving) -> two `SET_WINDOW`s (front/back,
   300 dpi, 12-bit) -> two `DEFINE_SCAN_MODE` pages -> `SCAN` -> read one
   183,744-byte band -> `OBJECT_POSITION 31 00` (session step).
3. **Shading readback**: 77 x `READ 0x3B` of the per-column correction tables
   the firmware just built. (Host-side; our render doesn't need it.)
4. **Final scan**: `SET_WINDOW` x2 (8-bit, `ULy = -472` pre-roll, unlimited
   length) -> three `DEFINE_SCAN_MODE` pages (slow-feed) -> `SCAN 00 01` ->
   `READ` 1 MiB chunks until the end-of-scan sense. The paper feeds slowly here.

### The decisive discovery: SCAN's 2-byte window list selects the *target*

`SCAN`'s payload is a 2-byte window-id list, and it is **not constant**:

- `ff ff` = internal **dark** reference (LED off) -> calibrates the black floor
  (analog offsets converge to `0a`/`11`).
- `fe fe` = internal **white** reference (backing strip) -> drives the gain
  search (`80 -> 95/97 -> 92/95`) and per-channel white targets.
- `00 01` = the real **document** windows -> this is the only SCAN that moves
  the paper.

**This explains the single most confusing bug of the whole project:** every
early replay shot the page straight through the feeder during "calibration",
because we had hard-coded `SCAN 00 01`. Once we replayed the captured
`ff`/`fe`/`00` payloads faithfully, the paper sat perfectly still through
calibration and fed slowly through the final scan - exactly like COT.

### Other faithfulness bugs we hit

- **We were writing to `INDATA.dat`; COT never does.** Our replay called
  `clear_data` (zeroing the data pipe) before reads. COT's driver doesn't, and
  the firmware sees every host sector write. Removing those writes fixed flow
  control. Lesson: *be byte-faithful, including what you DON'T write.*
- **Missing pre-feed arming reads.** COT issues a specific
  `INQUIRY`/`READ 0x84/0x8b/0x8c` sequence before the feed that primes firmware
  state. We had to include that whole pre-feed tail verbatim.
- **Transport timeout too short.** Paper-motion commands hold the pipe
  `PENDING` far longer than our 8 s default; bumped to 90 s.
- **Sniffer missing commands.** A first "only log on the PENDING edge" sniffer
  missed `SET_ADJUST`/`SET_WINDOW`/`DEFINE`/`SCAN` entirely. Fix: log *every*
  distinct command-sector state and tag it `pending`, then let the replay pick
  the right payload per command.

### The final frame format

The 55,414,128-byte capture is **15,312-byte lines x 3,619 lines**, each line
being **three 5,104-px 8-bit segments = the R/G/B channels of the front side**
at 600 dpi horizontal x 300 dpi vertical. COT's "300 dpi gray" is just these
channels averaged and 2:1 horizontally downsampled. Critically, the calibrated
capture has **~52% ink contrast** vs the ~18% we got from uncalibrated scans -
that gap is *entirely* the calibration, not post-processing.

---

## 5. The final architecture

The product is small and lives entirely on the pipe path:

```
HTTP client
   |
service/app.py            FastAPI: /scan /status /release, format + multi-page
   |
r10.cot_scan.CotScanner   runs the verified choreography, per page
   |   |
   |   +-- src/r10/data/cot_sequence.json   the 195-command choreography,
   |                                        bundled (generated from the trace)
   |
r10.pipe_transport.RawPipeTransport   SCSI over the raw FAT file-tunnel
   |
Canon R10 firmware
   |
r10.render                decode 600x300 RGB frame -> COT tone pipeline -> file
   |
r10.cot_pipeline          the reimplemented COT tone math (destripe, levels)
```

### Module responsibilities

- **`src/r10/pipe_transport.py`** - the raw-sector SCSI transport (unmount,
  FAT-map, command handshake). `sudo` required.
- **`src/r10/cot_scan.py`** - `CotScanner`: opens the pipe, runs the bundled
  choreography (`scan_page`), loops over the feeder (`scan_batch`), and exposes
  `device_info()` / `paper_present()`. Auto-detects the disk slice via
  `find_r10_slice()` (the `diskutil` disk number changes between plug-ins).
- **`src/r10/data/cot_sequence.json`** - the choreography as data, so the
  product has **no dependency on any capture workspace**. Generated once from a
  sniffer trace by the replay tooling and then shipped in the package.
- **`src/r10/render.py`** - decode the raw frame, auto-crop the pre/post-roll
  bands, run the tone recipe, and encode to PDF/TIFF (multi-page) or PNG/JPEG.
- **`src/r10/cot_pipeline.py`** - the reimplemented COT tone math recovered from
  `LLiPmDRP215` (per-column shading, `makeGammaDataforFC` levels stretch,
  guided denoise, destripe).
- **`service/app.py`** - the FastAPI server (see below).

### The render recipe (verified against the COT reference)

```
gray = (R + G + B) / 3            # 3 LED-phase channels -> luma
gray = 2:1 horizontal downsample  # 600 -> 300 dpi, matching COT
autocrop dark pre/post-roll bands # window ULy=-472 starts before the page
scale paper level -> 235
destripe()                        # remove residual column/row fixed-pattern
make_gamma_data(150, 225)         # COT's piecewise-linear levels stretch
median(3) + unsharp(r=1.2, 140%)  # consolidate strokes, whiten paper
rotate -90                        # portrait
```

### The HTTP API

- `POST /scan` - scans every page in the feeder (up to `max_pages`, feeder
  holds 10) and returns the document bytes. `format=pdf` (default) or `tiff`
  combine all pages into one file; `png`/`jpeg` return a bare image for one
  page or a ZIP of `page_NN` files for several. Also `quality` (JPEG),
  `skip_shading` (faster).
- `GET /status` - device identity + whether paper is loaded + busy flag.
- `POST /release` - remount the volume so CaptureOnTouch can run; the next
  request reclaims the device automatically.

The device is claimed lazily and held (volume unmounted) for the server's
lifetime, with a lock serializing access to the single scanner.

Run it: `sudo .venv/bin/python service/app.py` (root for raw device access).

---

## 6. Why it looks the way it does (design decisions)

- **Pipe, not USB.** §3.1 vs §3.2 - the OS won't let go of a mass-storage
  device cleanly, and the firmware wants the file tunnel anyway.
- **Replay a captured choreography instead of computing one.** COT's AGC loop
  computes gains from live measurements; we don't reimplement that math. We
  ship the converged sequence COT ran on this unit, which reproduces its
  quality verbatim. Recomputing gains live (true closed-loop AGC) is a possible
  future improvement, but it isn't needed for COT-parity in practice.
- **Bundle the sequence as data.** The product must not depend on any capture
  workspace. `cot_sequence.json` is generated once and shipped in the package.
- **Reimplement the tone pipeline in NumPy, not via Canon's dylib.** The
  `ctypes`/Rosetta bridge to `LLiPmDRP215` was invaluable for *learning* the
  exact math (and validating our port), but shipping it would mean shipping
  Canon binaries and a fragile Rosetta dependency. `cot_pipeline.py` is a clean
  reimplementation.
- **Hold the device open for the server's life.** Remounting per request is
  slow (~1 s of `diskutil`); a lock plus a persistent claim is simpler and
  faster for a single-user desktop scanner.

---

## 7. How to extend this

### Add an output format
`src/r10/render.py`, `encode()`. PIL already handles the container formats;
add the branch and a MIME entry. Multi-page goes in the PDF/TIFF path or the
ZIP fallback.

### Change scan quality / tone
`src/r10/cot_pipeline.py` (the math) and `render.render_page()` (the recipe
parameters: `black_pt`, `white_pt`, `paper_target`, `sharpen`). The verified
defaults match a CaptureOnTouch reference scan; validate changes against a COT
scan of the same page.

### Instant scans (calibration caching)
The full choreography spends most of its ~60-90 s preamble on the 9-cycle
AGC calibration + shading readback. Because the feed is the only setup
command that needs paper, that whole block runs with an empty feeder and its
converged state persists in the powered device.
`CotScanner.warm_calibrate()` runs it standalone;
`scan_batch(use_cached_calibration=True)` then goes straight to feed ->
window setup -> document scan. The service keeps the calibration warm in the
background (at startup and every `--calib-interval` seconds, default 300),
`/scan?calibration=cached` (the default) rides it, and `?calibration=full`
runs the complete per-scan choreography when you want to compare quality.
Verified on hardware; details in `docs/protocol.md` §6.7.4.

### Re-capture the choreography (e.g. different DPI / color mode)
1. Quit CaptureOnTouch, load a page.
2. `sudo .venv/bin/python tools/pipe_sniffer.py --slice <diskNs1>`
3. Run ONE scan in COT with the settings you want; Ctrl-C when done.
4. `sudo .venv/bin/python tools/cot_replay.py` to verify it replays.
5. Regenerate `src/r10/data/cot_sequence.json` from the new trace (the replay
   tool's `load_issuances()` is the extractor).

### Duplex / back side
The frame carries both sensors; we currently render the front. The back
segment is present in the raw data - `render.decode_gray300` averages the three
front channels, so a back-side decode would parallel it. End-of-scan sensing is
already confirmed (§6.2).

---

## 8. Repository map

```
src/r10/
  cot_scan.py        product scanner: the verified choreography as a library
  render.py          raw frame -> PNG/JPEG/PDF/TIFF
  cot_pipeline.py    reimplemented COT tone math
  pipe_transport.py  SCSI over the raw FAT file-tunnel (sudo)
  data/cot_sequence.json   the bundled 195-command choreography
service/app.py       FastAPI HTTP server
tools/
  scan_multipage.py  end-to-end multi-page scan runner (sudo)
  pipe_sniffer.py    capture CaptureOnTouch's real traffic
  cot_replay.py      replay it byte-for-byte; regenerate the bundled sequence
  pipe_probe.py      read-only pipe probe
  disasm_macho.py    Mach-O disassembler (reversing Canon's binaries)
  trace_calls.py     call-sequence tracer
  llipm_bridge.py    ctypes bridge to Canon's LLiPmDRP215 (learning/validation)
  setup_llipm.sh     prepare Canon dylibs for the bridge
docs/
  PROCESS.md         this document (the "why" and the journey)
  protocol.md        the byte-level protocol reference (the "what")
tests/               product-path tests (choreography integrity, render/encode)
```

---

## 9. Timeline in one paragraph

We started trying to write a real USB driver, discovered macOS won't cleanly
yield the mass-storage device, and pivoted to Canon's own file-tunnel protocol
over the raw disk. We got pages out but couldn't match COT by guessing
acquisition parameters, so we reverse-engineered Canon's binaries
(`ONTOUCHL.exe`, `R10Lite.ds`, `LLiPmDRP215`) to learn the calibration and tone
math, and finally sniffed CaptureOnTouch's exact command stream and replayed it
byte-for-byte. That replay - once we got the SCAN window-id targets, the
pre-feed arming reads, and "don't touch INDATA.dat" right - produced a capture
indistinguishable from CaptureOnTouch. We then productized it: bundled the
choreography as data, wrapped it in a `CotScanner` library and a `render`
module, and exposed it as an HTTP API with multi-page PDF and format selection.
