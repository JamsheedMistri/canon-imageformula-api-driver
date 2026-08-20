# Canon imageFORMULA R10 - Protocol Reference

This document records everything we know (confirmed) and everything we infer
(unconfirmed, needs capture) about how to drive the R10 without Canon's
software. Treat "inferred" rows as hypotheses to validate in Phase 2/4.

## 1. Device identity (CONFIRMED on hardware)

Observed via `ioreg -p IOUSB -l` and `lsusb -v` (sane-devel archive) on this unit:

| Field                | Value                                   |
| -------------------- | --------------------------------------- |
| Product string       | `CANON   R10` (note the padding spaces) |
| `idVendor`           | `0x1083` (Canon Electronics, Inc.)      |
| `idProduct`          | `0x167f`                                |
| `bcdDevice`          | `2.02`                                  |
| `bDeviceClass`       | `0` (per-interface)                     |
| `bNumConfigurations` | `1`                                     |
| `bNumInterfaces`     | `1`                                     |
| `bInterfaceClass`    | `8` (Mass Storage)                      |
| `bInterfaceSubClass` | `6` (SCSI transparent command set)      |
| `bInterfaceProtocol` | `80` / `0x50` (Bulk-Only Transport)     |
| Bulk IN endpoint     | `0x81`, 512-byte max packet             |
| Bulk OUT endpoint    | `0x02`, 512-byte max packet             |
| Power                | Bus powered, 500 mA                     |

The device always enumerates as **USB Mass Storage**. Unlike the Canon P-208 /
P-215 (which have a physical mode switch that re-enumerates them as a native
scanner ID such as `0x165f`, supported by SANE `canon_dr`), the R10 has **no
scanner mode and no SANE entry**. This is why we must drive it ourselves.

## 2. On-device software layout (CONFIRMED)

When powered on, the R10 mounts two volumes:

- `ONTOUCHLITE` (FAT16, `/dev/disk8s1` on the test Mac) - the Windows autorun
  payload. Key files:
  - `ONTOUCHL.exe` - Windows launcher
  - `TOUCHDRL.ini` - launcher config (see below)
  - `INDATA.dat` (2,097,152 bytes) - command pipe
  - `transfer.dat` (2,097,152 bytes) - image/data transfer pipe
  - `SYSTEM.dat`, `autorun.inf`, `manual.url`
- `CaptureOnTouch Lite for Mac` (HFS, `/dev/disk9`) - the macOS launcher app
  `CaptureOnTouch Lite Launcher.app`.

`TOUCHDRL.ini` (CONFIRMED contents):

```
[Launcher]
StartAddress=275775488
FileCount=2
LoadNumber0=0
LoadNumber1=2
CmdFileSize=2097152
Scanner=R10
ExecAppName=TouchDR.exe
UpdateCheckerName=update_checker.exe
```

`CmdFileSize = 2097152` (2 MiB) matches the size of both `.dat` pipe files.
`StartAddress = 275775488` is the base address used by the command framing (it
is far larger than the 8.4 MB pseudo-disk, so it is a protocol address, not a
literal LBA on the FAT volume).

## 3. Command mechanism (CONFIRMED from binary strings)

`strings` on `CaptureOnTouch Lite Launcher` reveals the driver class hierarchy:

- `CCeiSimpleDriver::LoadDevice() CeiUSBInitialize Failed!`
- `CCeiFileIOLite::LoadDevice OpenSession returns %u`
- `CCeiFileIOLite::ExecRead sense code %x, invalid len = %x`
- `CCeiFileIOLite::ExecWrite sense code %x, invalid len = %x`
- `CCeiFileIOLite::ExecNone sense code %x, invalid len = %x`
- `CCeiFileIOLite::GetTransferLimit is called`
- `CCeiFileIOLite::SetTransferTimeout ...`
- classes `CDocScanner`, `CCanoFileScanner`, `CCeiFileIO`

Interpretation: the high-level API issues **SCSI CDBs** and branches on the
returned **SCSI sense data**. `ExecRead` / `ExecWrite` / `ExecNone` correspond
to the three data directions (device->host, host->device, no data). But the
_low-level delivery_ is NOT raw SCSI passthrough - see section 3.2: the CDB is
wrapped in a command block and written to `INDATA.dat`, with the response read
from `transfer.dat`.

## 3.2 Low-level command framing (CONFIRMED from ONTOUCHL.exe disassembly)

Disassembling the scanner's bundled Windows launcher `ONTOUCHL.exe` pins
down exactly how `CCeiFileIOLite` implements the Exec\* calls. It does **not**
use `DeviceIoControl`/`SCSI_PASS_THROUGH`; it uses `CreateFileW` + overlapped,
unbuffered `ReadFile`/`WriteFile` on the two pipe files of the removable volume.

The device is located by scanning drive letters for a `DRIVE_REMOVABLE`
(`GetDriveTypeW == 2`) volume that also has **512-byte sectors and 0 free
clusters** (`GetDiskFreeSpaceW`), then `INDATA.dat` and `transfer.dat` are
opened by name (`LoadDevice`, VA `0x402b50`) with
`FILE_FLAG_OVERLAPPED | FILE_FLAG_NO_BUFFERING` (`0x60000000`). `transfer.dat`
is verified to be exactly 2 MiB. All pipe I/O is overlapped, at **file offset
0**, in **512-byte-aligned** transfers.

**Pipe roles (CONFIRMED by resolving the import address table):**

- `transfer.dat` = **command + status** channel. The host `WriteFile`s the
  command block here (`0x402910` -> `WriteFile`, handle `this+0x04`), then polls
  it (`0x402ed0` -> `ReadFile`) for the firmware to change the status dword.
- `INDATA.dat` = **data** channel. Response/image bytes are `ReadFile`d from
  here (`0x4029d0` -> `ReadFile`, handle `this+0x08`).
- At boot the firmware pre-populates `transfer.dat` at offset `0x1c` with the
  INQUIRY identity (`CANON`/`R10`/`2.02`); `LoadDevice` reads it directly to
  verify the device, without sending any command.

**Completion handshake (the key to driving it):** the command block sets the
dword at offset `0x18` to `0xFFFFFFFF`. After the write, the host re-reads
`transfer.dat` and waits (`GetTickCount` timeout) until `[0x18] != 0xFFFFFFFF`.
`[0x18] == 0` means success; any other value is an error/sense code (the
"cmd is sent, but status is not changed" path sets `0x80fe0004` on timeout).

`ExecRead` (VA `0x4026c0`) / the shared builder (`0x402ff0`) build this 28-byte
command block and write it to `transfer.dat`:

| Offset         | Value        | Meaning                                                            |
| -------------- | ------------ | ------------------------------------------------------------------ |
| `0x00`         | `0x00`       | (zero)                                                             |
| `0x03`         | `0x14`       | constant (also = response data offset, 20)                         |
| `0x05`         | `0x01`       | constant                                                           |
| `0x06`         | `0x90`       | constant                                                           |
| `0x0C`..`0x17` | SCSI CDB     | the CDB, **max 12 bytes** (enforced: `cmp cdb_len, 0xc; ja error`) |
| `0x18`         | `0xFFFFFFFF` | status/completion dword (firmware overwrites it)                   |
| rest           | `0x00`       | zero-filled to the sector/transfer size                            |

The whole buffer is zeroed first (`memset(buf, 0, this[0x7c])`), then the fields
above are set. The write is sector-aligned (`len = (len + 0x1ff) & ~0x1ff`).

For **data-out** commands the shared builder (`0x402ff0`) also sets: `[0x1c..0x1f]`
= big-endian data length, `[0x21] = 2`, `[0x22] = 0xB0`, and copies the payload
to offset `0x28`; the total written length is `data_len + 0x1c`.

The boot **identity** block sits in `transfer.dat` at offset `0x1c`:

```
transfer.dat (boot identity, read directly by LoadDevice):
  0x1C        standard INQUIRY data: periph type (0x00) ...
  0x1C+8      vendor  "CANON   "
  0x1C+16     product "R10             "
  0x1C+32     revision "2.02"
```

(NB: an early attempt wrote the command to `INDATA.dat` and polled
`transfer.dat` - reversed - and got no response. The corrected direction is
what the transport does now.)

`this` (the `CCeiFileIOLite` object) field map recovered from the code:

| Field                  | Meaning                                         |
| ---------------------- | ----------------------------------------------- | -------- |
| `+0x04`                | `INDATA.dat` file handle (command write)        |
| `+0x08`                | `transfer.dat` file handle (response read)      |
| `+0x0C`                | command buffer                                  |
| `+0x10`                | response buffer                                 |
| `+0x18` (in cmd block) | `0xFFFFFFFF`                                    |
| `+0x40`                | "device open" flag                              |
| `+0x44`                | max response length                             |
| `+0x58`                | last error code (e.g. `0x80fe0002`, `0x80ff0000 | winerr`) |
| `+0x5C`                | `CRITICAL_SECTION` guarding each Exec           |
| `+0x74`                | OVERLAPPED event handle                         |
| `+0x7C`                | command-buffer size                             |

### 3.3 macOS filesystem-pipe test result (CONFIRMED)

An early probe replicated the above with `os.open` + `F_NOCACHE` + `fsync`
on the mounted `/Volumes/ONTOUCHLITE`. Findings:

- `transfer.dat` already contains a valid INQUIRY identity block at offset
  `0x14` (leftover from the last real session / boot handshake). Parsing at the
  confirmed `0x14` offset gives `CANON` / `R10` / `2.02` correctly.
- **Zeroing `transfer.dat`, writing the INQUIRY command block to `INDATA.dat`,
  and polling did NOT regenerate the response.** macOS's FAT driver does not
  push our `INDATA.dat` writes to the exact device sectors the firmware watches
  the way Windows `FILE_FLAG_NO_BUFFERING` does (writes are cached/coalesced,
  and possibly a session-open handshake is required first).

Conclusion: on macOS the pipe must be driven with **sector-exact I/O**, i.e.
either (a) raw `WRITE(10)`/`READ(10)` over libusb/BOT to the LBAs backing
`INDATA.dat`/`transfer.dat`, or (b) writes to the raw block device
(`/dev/diskN`) while the volume is unmounted. The 28-byte command-block format
above is what goes in the payload either way.

### 3.1 Demangled driver API (CONFIRMED from symbol table)

The Mach-O symbol table of the bundled Mac driver exposes the real class API.
The mangled names
demangle to these signatures, which pin down the exact call shapes we mirror in
`r10/scsi.py` and `r10/scanner.py`:

`CCanoFileScanner` (the SCSI transport):

```
OpenSession()
CloseSession()
ExecRead (const unsigned char* cdb, unsigned long cdb_len, unsigned char* data, unsigned long data_len)
ExecWrite(const unsigned char* cdb, unsigned long cdb_len, const unsigned char* data, unsigned long data_len)
ExecNone (const unsigned char* cdb, unsigned long cdb_len)
ExecRequestSense(unsigned int* key, unsigned char* buf, unsigned int* len)
ReadData (void* buf, unsigned int* got, unsigned int want, unsigned int flags)
WriteData(void* buf, unsigned int len, unsigned int want, unsigned int flags)
CreateScannerList(SScannerDesc*)
```

`CDocScanner` (the scan state machine) - note the `tagScanParam*` argument:

```
OpenSession() / CloseSession()
StartScan (tagScanParam*)
ScanPage  (tagScanParam*)
ReadLines (unsigned char* buf, unsigned int n, unsigned int* got)
ScanComplete()
FinishScan()
AbortScan()
GetStatus (const char*, void*)
GetState()
```

`CCeiSimpleDriver` (bootstrap / bulk file transfer):

```
LoadDevice() / UnloadDevice()
ReadFileHeaderFromScanner()
ReadDataFromScanner(unsigned char* buf, unsigned int off, unsigned int len)
ReadToArchiveProcess(unsigned int, CArchiveCore*)
```

This confirms the driver design directly:

1. `ExecRead/ExecWrite/ExecNone(cdb, cdb_len, data, data_len)` is a plain SCSI
   CDB pass-through - exactly our `bot.transfer()` signature.
2. `ExecRequestSense(key, buf, len)` matches our REQUEST SENSE handling.
3. `StartScan -> ScanPage -> ReadLines (loop) -> ScanComplete -> FinishScan`
   is the scan session shape our `Scanner.scan()` implements.
4. `tagScanParam` is the on-wire parameter block we build in
   `scsi.build_set_scan_param()` (fields to be confirmed from the capture).

## 4. Bulk-Only Transport framing (STANDARD, CONFIRMED by spec)

The interface protocol byte `0x50` means USB Mass Storage Bulk-Only Transport
(BOT). Every command is a 31-byte Command Block Wrapper (CBW) on EP `0x02`,
an optional data phase, then a 13-byte Command Status Wrapper (CSW) on EP
`0x81`.

CBW (little-endian):

| Offset | Size | Field                  | Notes                                 |
| ------ | ---- | ---------------------- | ------------------------------------- |
| 0      | 4    | dCBWSignature          | `0x43425355` ("USBC")                 |
| 4      | 4    | dCBWTag                | echoed back in CSW                    |
| 8      | 4    | dCBWDataTransferLength | bytes in data phase                   |
| 12     | 1    | bmCBWFlags             | `0x80` = IN (dev->host), `0x00` = OUT |
| 13     | 1    | bCBWLUN                | low nibble, `0`                       |
| 14     | 1    | bCBWCBLength           | valid bytes of CDB (1-16)             |
| 15     | 16   | CBWCB                  | the SCSI CDB                          |

CSW:

| Offset | Size | Field           | Notes                               |
| ------ | ---- | --------------- | ----------------------------------- |
| 0      | 4    | dCSWSignature   | `0x53425355` ("USBS")               |
| 4      | 4    | dCSWTag         | must equal CBW tag                  |
| 8      | 4    | dCSWDataResidue | undelivered byte count              |
| 12     | 1    | bCSWStatus      | `0`=pass, `1`=fail, `2`=phase error |

On `bCSWStatus == 1` we issue `REQUEST SENSE` (0x03) to read the sense key /
ASC / ASCQ, exactly as the binary does.

## 4.1 Hardware bring-up results (CONFIRMED on real R10)

An early USB/BOT hardware probe, run **as root** on macOS, successfully claimed
the interface and exchanged real SCSI. Confirmed:

| Command                  | Result                                                            |
| ------------------------ | ----------------------------------------------------------------- |
| Claim interface 0        | Works, but **only as root** on macOS (see below)                  |
| INQUIRY (0x12)           | type `0x00`, vendor `CANON`, product `R10`, rev `2.02`            |
| TEST UNIT READY (0x00)   | ready                                                             |
| REQUEST SENSE (0x03)     | key `0x00` asc `0x00` ascq `0x00` (no error)                      |
| READ CAPACITY(10) (0x25) | last LBA `16383`, block `512` -> **8 MB LUN 0**                   |
| READ(10) LBA 0           | valid FAT16 **MBR**: partition type `0x04`, start LBA `63`, ~6 MB |
| READ(10) LBA 538624      | **STALL / pipe error**                                            |

Key conclusions:

1. The BOT + SCSI stack we built at the time was correct against real silicon:
   standard commands and `READ(10)` by LBA work. (That direct-USB path was
   later abandoned; see `docs/PROCESS.md` §3.1.)
2. INQUIRY reports peripheral type `0x00` (direct-access / disk), **not** `0x06`
   (scanner) - because in AutoStart mode the R10 presents as mass storage. So
   "is this an R10 scanner" must be decided by the vendor/product strings, not
   the peripheral type. (The driver identifies the R10 by its product string.)
3. **`StartAddress` (263 MiB) is NOT a directly addressable LBA on LUN 0**: a
   `READ(10)` at `StartAddress/512` stalls because it is beyond the 8 MB medium.
   The command pipe is therefore reached either via (a) `READ(10)`/`WRITE(10)`
   to the specific LBAs backing `INDATA.dat` / `transfer.dat` inside the FAT,
   which the firmware intercepts, or (b) a vendor opcode. This is the remaining
   unknown - see section 5/6.
4. `transfer.dat` currently contains, at file offset `0x14`, a standard INQUIRY
   response block (`CANON   R10 ... 2.02`), confirming it is the device->host
   response pipe.

### macOS claiming reality (CONFIRMED)

- Without root: `libusb_claim_interface` -> `EACCES`. The
  `IOUSBMassStorageDriver` owns the interface even after both volumes are
  unmounted.
- With root: libusb (1.0.30) captures + re-enumerates the device and the claim
  succeeds.
- Side effect: after a root capture the mass-storage driver does not
  automatically re-attach, so the disks disappear until the scanner is power
  cycled. Plan hardware sessions accordingly (or run on Linux, where
  `usb-storage` can be unbound per-device without this dance).

## 5. SCSI command set (CONFIRMED vocabulary from ONTOUCHL.exe)

`ONTOUCHL.exe` contains the driver's command-name table: a fixed 36-byte-stride
array of UTF-16 names at file offset `0x1d384`, indexed by an internal
command enum. In table order:

```
TestUnitReady, RequestSense, Inquiry, ModeSelect, ReserveUnit, ReleaseUnit,
ModeSense, Scan, Diagnostic, SetWindow, GetWindow, Read, Send, ObjectPosition,
GetScannerStatus, GetScanMode, DefineScanMode, StopBatch, SetFunctionKey,
GetFunctionKey, UnknownCommand
```

This is the **SCSI-2 scanner device command set** (the standard scanner opcodes)
plus Canon vendor extensions. The standard opcodes are well-defined by the
SCSI-2 scanner spec; the vendor ones (`GetScannerStatus`, `GetScanMode`,
`DefineScanMode`, `StopBatch`, `Set/GetFunctionKey`) use Canon-private opcodes
whose exact byte values are not printed in the name table (the enum-index-to-
opcode map lives in the dispatcher and needs deeper RE or a capture).

| Name               | Std opcode | Dir    | Purpose                                     |
| ------------------ | ---------- | ------ | ------------------------------------------- |
| TestUnitReady      | `0x00`     | none   | is the scanner ready                        |
| RequestSense       | `0x03`     | in     | read sense data after a failure             |
| Inquiry            | `0x12`     | in     | vendor/product/firmware id (CONFIRMED live) |
| ModeSelect         | `0x15`     | out    | set options                                 |
| ReserveUnit        | `0x16`     | none   | claim the unit                              |
| ReleaseUnit        | `0x17`     | none   | release the unit                            |
| ModeSense          | `0x1a`     | in     | read options                                |
| Scan               | `0x1b`     | none   | begin a scan / feed a page                  |
| Diagnostic         | `0x1d`     | out    | SEND DIAGNOSTIC                             |
| SetWindow          | `0x24`     | out    | set window: dpi, mode, duplex, geometry     |
| GetWindow          | `0x25`     | in     | read window descriptor                      |
| Read               | `0x28`     | in     | read image data                             |
| Send               | `0x2a`     | out    | send data                                   |
| ObjectPosition     | `0x31`     | none   | feed / eject a page (ADF)                   |
| GetScannerStatus   | vendor     | in     | bytes available / feed / hopper status      |
| GetScanMode        | vendor     | in     | current scan mode                           |
| DefineScanMode     | vendor     | out    | define scan mode                            |
| StopBatch          | vendor     | none   | stop the batch/feed                         |
| Set/GetFunctionKey | vendor     | in/out | panel key state                             |

> Standard opcodes are confirmed by name; the exact CDB byte layouts (esp.
> `SetWindow`'s window descriptor block) and the vendor opcode values still need
> confirmation from a capture or deeper dispatcher RE.

## 6. Scan session (CONFIRMED on hardware)

The full sequence below executed successfully against the real device
(an early raw-pipe scan at 150 dpi gray) and produced a readable page image:

```
TEST UNIT READY (0x00)  -> clear power-on UNIT ATTENTION (REQUEST SENSE drains)
GET WINDOW (0x25)       -> 8-byte header + 44-byte WDB template
SET WINDOW (0x24)       -> patched WDB: X/Y dpi @2-5, width/length in 1/1200"
                           @14/18, brightness @22, composition @25, bpp @26
SCAN (0x1b)             -> 1-byte window list [0]; paper feeds immediately
loop:
  READ (0x28)           -> pull image strips (1 MiB requests work)
until CHECK CONDITION with a definitive end sense
```

### 6.1 Image payload format (gray, confirmed)

Raw 8-bit grayscale, no compression, no per-strip header, plain raster
top-to-bottom. One line = `width_px` rounded up to **even** bytes: at
8.5" / 150 dpi that is 1276 bytes/line (1275 px + 1 pad). Confirmed exactly:
a full letter page came back as 2,105,400 bytes = 1276 x 1650 =
8.5" x 11" @ 150x150 dpi. (An earlier "even/odd interleave at pitch 2552"
hypothesis was wrong - 2552 was just the two-line harmonic; adjacent lines
correlate as strongly as adjacent pixels.)

Output is severely underexposed (paper ~ 40/255) with visible per-pixel
vertical streaking - the classic signature of a CIS sensor with **no shading
correction** and no gamma applied. Autocontrast recovers a readable image;
the real calibration protocol was recovered later (6.5-6.7).

### 6.2 READ termination semantics (confirmed on hardware)

The image ends with exactly this signature (observed live):

1. **Final short strip**: CHECK CONDITION, sense `f0 00 20 00 <residue:4> 06 ...`
   - key 0x00 (NO SENSE) with **ILI (0x20)** set and the Information field
     (bytes 3-6) holding the residue. Valid bytes = requested - residue.
2. **Any READ after that**: CHECK CONDITION, sense key **0x05 ILLEGAL
   REQUEST, asc 0x2c (command sequence error)** - the scan session is over
   and READ is no longer a legal command. No data is transferred (the
   Information field is zero and must not be interpreted as residue).

The firmware only writes as many bytes to `INDATA.dat` as it actually has:
the read window must be zeroed before each READ or stale sectors from the
previous strip pollute the tail.

## 6.3 Image quality: sensor gain & host processing (measured)

The raw payload is **near-raw CIS sensor data with very low gain**. Measured on
a white letter page @ 150 dpi gray:

- White paper reads only **~40/255**; the entire image occupies 0..76 (77 of
  256 levels).
- Noise decomposes into **~3.4 levels fixed-pattern (per-column)** + **~2.2
  levels random**. Fixed-pattern is removable (shading); random is not.
- Because paper is at ~40, any pipeline must apply ~6x gain to reach white,
  which amplifies the 2.2-level random noise to ~14 visible levels -> the
  "grain". This is a hardware-SNR limit, not a rendering bug.

CaptureOnTouch avoids this by calibrating sensor gain/exposure and downloading
shading correction so paper reads ~230-250 natively. That calibration logic is
**not in the onboard `ONTOUCHL.exe`/launcher** - those are only a bootstrap
that reads a compressed archive from the device at `StartAddress` and launches
the real app (`ExecAppName=TouchDR.exe` on Windows, `CaptureOnTouch Lite.app`
on macOS). The gain/shading commands live in that firmware-served app; the
copied `SYSTEM.dat`/`INDATA.dat` are just `0xAA` filler, so recovering them
needs either the archive-read protocol (`ReadToArchiveProcess`) or an empirical
probe of the gain levers (`SetWindow` brightness/contrast, `ModeSelect`, a
vendor calibrate).

**8-bit only:** `SET WINDOW` with bits/pixel = 0x10 is rejected (status
CHECK CONDITION; `GET WINDOW` readback still shows 0x08) - the device will not
emit 16-bit raster, so extra bit depth cannot fix the quantization grain. The
only lever is raising the captured signal itself (gain/LED via 0xE1, 6.5).

Interim (an early approach): a software reconstruction pass produced a cleaner
document image (per-pixel background division to flatten gain/shading/
fixed-pattern, denoise, tone map). Oversampling (scan at 300-600 dpi,
downscale) averages down the random noise. This yields a legible, flattened
page but not full pristine quality until the hardware gain is raised - which is
why the final design calibrates on-device instead (6.5-6.7).

## 6.4 CaptureOnTouch's actual window (recovered)

Running `GET WINDOW` immediately after a CaptureOnTouch "Auto" scan returns the
window COT left in the device - i.e. exactly what it used for a pristine
(clean white background, crisp text) result:

```
res X/Y = 0x012c = 300 dpi     (we had used 150)
width   = 0x27e0 (~8.5"),  length = 0x3a7e (~12.5", auto-detected)
brightness = 0x80
image composition (WDB[25]) = 0x05     (we had used 0x02 = plain gray)
bits/pixel (WDB[26])        = 0x08
```

The decisive difference is **composition 0x05 vs our 0x02** - and 0x05 turned
out to be the whole answer (CONFIRMED on hardware):

- **0x05 @ 8bpp is the DUPLEX slow-feed capture**, not "color" or a single
  enhanced-gray frame. Each scan line is **three concatenated 8-bit segments of
  the fixed 2552-byte sensor stride**: `[front | back | empty]`, so the line
  stride is `2552 * 3 = 7656` and the payload is ~3x a plain `0x02` scan
  (25,245,000 B for letter @ 300 dpi = 7656 x ~3297).
- Byte layout confirmed by lag analysis: the three segments are **planar within
  the line** (segment 0 = full front line, then full back line, then a zero
  segment), NOT pixel-interleaved RGB. Segment 0 (front) reads bright
  (paper p50~166, p99~217); segment 1 (back) is the faint show-through
  (p50~107); segment 2 is all zero.
- Crucially, selecting 0x05 makes the scanner run **both sensors and feed the
  page slowly** - the same slow feed the user observed in CaptureOnTouch. The
  long per-line exposure is what lifts the front segment to ~217 natively (vs
  ~89 in the fast `0x02` mode), which is the real source of pristine quality:
  photons, not post-processing.

An early enhanced-scan path implemented this end to end: detect the true 7656
stride, keep the front segment, flat-field it with the on-board white
reference, and apply a light unsharp mask. The back/empty segments are
discarded (front-only, as desired). This was superseded by the full verified
choreography (6.7), which the current `render.py` pipeline consumes.

Earlier we mis-tested 0x05 (read as 1-byte gray, before the 0xE1 gain write
worked) and wrongly concluded it was 24-bit color; the size ratio is identical
(3x) so the two are easy to confuse without the segment/lag analysis above.

## 6.5 Pristine quality: the calibration protocol (RECOVERED from firmware app)

The firmware-served app was pulled off the device (its archive file 1 is a
gzip of `CaptureOnTouch Lite.app.tar`) and disassembled. The
pristine result is produced by an **analog-gain calibration** the "Lite"
onboard path never runs. The logic lives in two Mach-O binaries inside the app:

- `.../Frameworks/R10Lite.ds/Contents/MacOS/R10Lite` - the TWAIN data source;
  class `CCanoDR` builds every SCSI command and orchestrates calibration
  (`CCanoDR::AdjustLight()`).
- `.../R10Lite.ds/.../LLiPmDRP215.framework/.../LLiPmDRP215` - host image
  library; `Cei::LLiPm::DRHachi::CAdjustLight` computes the gain register and
  shading tables (`Shading.cpp`, `AdjustLight.cpp`). ("DRHachi" = the R10's
  internal model codename; "anapro" = the sensor's analog front-end.)

### Command containers (confirmed from `CCanoDR::SetupCommandContainer`)

Every command block is 256 B. The first 8 B are a fixed header, then the SCSI
CDB starts at **offset 0x0C**:

```
command block:  00 00 00 14 00 01 90 00 | 00 00 00 00 | <CDB @0x0C ...>
data-out block: 00 00 00 <len> 00 02 b0 00 | <payload ...>   (dir=0x02, 0xb0 pipe)
```

This matches the 28-byte block we already drive over the raw pipe.

### Commands used by calibration (opcodes confirmed in the disassembly)

| Exec fn                         | opcode         | CDB layout                                                        | purpose                                                      |
| ------------------------------- | -------------- | ----------------------------------------------------------------- | ------------------------------------------------------------ |
| `ExecGetMemory(addr,len,buf)`   | **0x3B**       | `3B 00 [addr BE32 @2] [len BE24 @6] 00`                           | read device memory (same as archive read); chunked at 0x2000 |
| `ExecSetAdjustData(front,back)` | **0xE1**       | `E1 00 00 00 00 03 00 00 28 ...` + 52-B data-out                  | **write analog gain/offset registers**                       |
| `ExecSetWindow`                 | 0x24           | standard SET WINDOW                                               | scan window                                                  |
| `ExecScan` / `ExecRead`         | 0x1B / 0x28    | standard                                                          | feed + read strips                                           |
| `ExecSend`                      | 0x2A           | `2A 00 8C ...`                                                    | send tables (e.g. shading)                                   |
| `ExecDefineScanMode`            | 0xE-reg writes | data-out header `00 00 00 1C 00 02 B0 00`, regs 0xE30/0xE32/0xE36 | scan-mode registers                                          |
| `Get/SetShadingData`            | -              | via `__CFData`                                                    | shading table exchange                                       |

### Calibration flow (`CCanoDR::AdjustLight`)

1. The firmware auto-captures a **white/dark sensor reference into device memory
   at 0x10080000** at power-on (no paper, uses the internal reference). The host
   does **not** trigger a scan for this - it just reads it.
2. `ExecGetMemory(0x10080000, 0x80000)` -> 512 KB reference image (front[+back]).
3. `AdjustLightFix` / `CAdjustLight::AdjustAnaproGain` measures `maxData` (the
   brightest pixel of the reference) and computes a new per-color analog gain:

   ```
   target  = LIGHT_ADJUST_GAIN_ADJ_TARGET_LIST[mode]   # = 2730 (0xAAA) for the normal modes
   newGain = clamp( 79 - trunc( (79 - curGain) * (maxData / target) ), 0, 255 )
   ```

   written per color to `tagADJUSTINFO+0x24` (front R,G,B) and `+0x38` (back).
   79 (0x4F) is the register pivot; the step is a one-shot feedback solve so the
   reference max lands on `target`. Result is cached and only recomputed when the
   window/mode changes.

4. `ExecSetAdjustData(0xE1)` writes the gain+offset registers to the sensor
   (front+back, 20-byte `SAdjustInfo` each: bytes [0..2]=gain RGB, [4..6]=offset
   RGB, words [8],[0xA],[0xC] big-endian = per-color aux).
5. `ExecSetWindow` + `ExecDefineScanMode`, then normal `ExecScan`/`ExecRead`.
   Because gain is now raised in hardware, strips come back well-exposed
   (white ~ target, not ~40), and residual shading/fixed-pattern is removed on
   the host (`makeShadingData` / `ShadingGrayCore_SIMD`).

This is the missing piece: **we currently skip steps 2-4**, so the sensor runs
at its default low gain. Implementing the `0x3B` reference read + gain solve +
`0xE1` write reproduces COT's native exposure.

## 6.6 Host post-processing: Canon's own LLiPm module (bridge VALIDATED)

The capture side is solved (0x05 slow-feed duplex + 0xE1 gain, 6.4/6.5), but the
final rendering gap vs CaptureOnTouch is its **host-side document processing**,
which lives in `LLiPmDRP215` ("Low-Level image Processing module", pulled off
the device with the rest of the app archive). Its exports are the exact
pipeline COT applies: `_RemoveShadow` (multi-stage background/shadow removal:
InitRemoveShadowInfo -> GetShadowEdge -> MedianFilter -> CorrectShadowLine ->
CorrectDocumentLine -> ExtendDocumentLine -> CheckShadowLine -> paint),
`_CustomColorGamma`, `_EraseDot` (despeckle), `_ReduceMoire`,
`_BrightnessToSlicelevel`, `_DetectColorMode`, `_DetectSlantAndSize*` (deskew),
`_IsBlankPageEx2`, `_RotateImage`, resolution converters, etc.

Rather than reimplement these from disassembly (guesswork), we call Canon's
actual code:

- The module is x86_64; it loads fine under **Rosetta** (`arch -x86_64
/usr/bin/python3` + ctypes). VALIDATED.
- Its install names are `@executable_path/...`-relative; `tools/setup_llipm.sh`
  copies `pafcv2`, `rdd20`, `LLiPmDRP215` to `/tmp/cotfw/`, rewrites the
  references to absolute paths (`install_name_tool`) and ad-hoc re-signs them.
  After that `ctypes.CDLL` resolves `RemoveShadow`/`CustomColorGamma`/etc.
  VALIDATED (`tools/llipm_bridge.py`).
- `tagCEIIMAGEINFO` (every function's image descriptor), recovered from
  `_GetHistogram` / `to_gray_image` / `_IsGrayImage` disassembly:

  | off  | type | meaning                                   |
  | ---- | ---- | ----------------------------------------- |
  | 0x00 | u64  | struct/header size = 0x68                 |
  | 0x08 | u64  | pixel data pointer                        |
  | 0x20 | u64  | width (pixels)                            |
  | 0x28 | u64  | height (lines)                            |
  | 0x30 | u64  | bytes per line (stride)                   |
  | 0x38 | u64  | total buffer length (bytes)               |
  | 0x40 | u64  | bits/sample context (x bpp compared to 7) |
  | 0x48 | u64  | bytes per pixel (1 gray, 3 packed RGB)    |
  | 0x50 | u32  | packing flag (checked ==1 for color)      |
  | 0x60 | u32  | resolution/mode (RemoveShadow: min-lines) |

- Each processing entry also takes a small settings struct
  (`tagREMOVE_SHADOW_INFO`, `tagCUSTOMCOLORGAMMAINFO`, ...) whose layout is
  being recovered the same way. `_RemoveShadow(image*, shadow_info*)`:
  `shadow_info[0x00] u32 >= 8` (struct size), `shadow_info[0x18] s32` =
  lines-to-process (must be >= dpi-scaled minimum and <= image height minus
  margin); remaining fields feed `InitRemoveShadowInfo`.

### Pipeline layers (who does what)

- **Acquisition** (in `R10Lite.ds`, calling `LLiPmDRP215`): `CAdjustLight`
  (`AdjustLightFirst` -> `AdjustAnaproGain/Offset` -> `AdjustDecideData`) solves
  analog gain/offset from the white/dark reference and writes it via `0xE1`
  (we emulate the endpoint). `AdjustLightCurve(in, out, adjinfo, side, ref, len)`
  applies the per-line shading curve using the on-board reference;
  `FilterSimplex`/`NormalFilterSimplex` is the edge-preserving enhancement.
- **Output** (in the app, via `LLiPm-turbo`): `LLiPm_ImageProcess(in, out,
jsonConfig, len)` runs a picojson-configured chain of `IP*` stages
  (`IPCvtColor`, `IPBrightness`, `IPResCon`, `IPBinarize`, `IPStraighten`,
  `IPAlignment`, `IPRotate`).

### Acquisition constants (exact, from disassembly)

Every stage below is now recovered byte-exact from the binaries, not tuned:

- **Analog dark target = 0x60 (96 counts).** `CAdjustLight::AdjustAnaproOffset`
  (llipm `0x35c4`) reads the measured dark level, computes `measured - 0x60`, and
  nudges the sensor offset register to zero that out. So every capture's
  calibrated black floor is 96. Our raw floor is ~94-96 -> already correct;
  `DARK_TARGET = 96` in `cot_pipeline.py` is this value, not a guess.
- **Per-column shading.** `AdjustLightCurve`'s core (llipm `0x1d5da`) is, per
  pixel: `out = ref + ((raw-ref)*gain + D/2)/D` for `raw>ref`, else `ref` — i.e.
  `(raw-dark)/(white-dark)` normalization with a per-column dark `ref` and gain.
  `shade()` reproduces this: subtract 96, divide by the per-column paper white.
- **Gray tone curve = `DRHachi::GammaBuilderImp::calcGrayGamma` (llipm `0x1fa46`),
  ported exactly** in `gray_gamma_lut()`. It is a linear toe
  `tt*(bright+a-x2)+y2` for shadows joined to a `pow((bright+a)/255, 1/2.2) *
(T3*422) + T4` highlight segment. Coefficient tables (index 1..7 = contrast
  preset; 4 is neutral) dumped from `0xa08b0/0xa08f0/0xa0930/0xa1130/0xa13f0`;
  brightness is `(b-128)*128/127`; the pow exponent is `1/2.2` (const `0x9ec30`).
  This replaces the earlier guessed `makeGammaDataforFC` black/white points.
  `makeGammaDataforFC` (the levels LUT) is still provided but is the FC path, not
  the R10 gray path.

`src/r10/cot_pipeline.py` now runs this exact chain (shade -> FilterSimplex-style
guided denoise -> `calcGrayGamma`), no Canon libs at runtime.

### Why our output still isn't COT-crisp (root cause, evidenced)

Feeding our numbers through the _exact_ curve settles the question: our faint
print reads 135 vs paper 166 (reflectance ~0.56 after the 96 dark floor), and
`calcGrayGamma` maps 0.56-reflectance input to ~190-210 at **every** contrast
preset — the curve is photographic (tone-preserving) by design and cannot turn
faint print black. With the exact tone, `contrast=6` already matches COT's white
fraction (92.9% vs 92.6% >220) but our dark fraction is lower (<64 0.29% vs
0.69%) because the strokes are lighter.

The gap is **capture quality, not tone**, and the raw shows why directly:

- A native-resolution crop of the raw front segment shows soft, low-contrast grid
  lines buried in strong **vertical fixed-pattern (column) noise**; mean local
  gradient is 9.97 counts (noise) vs COT's 1.15 (smooth). Paper pixel noise std
  is ~11.6 counts while the text signal is only ~30 counts above paper -> SNR ~3.
- The device's 16-bit white reference (`0x10080000`) does **not** correlate with
  our extracted per-column paper profile at any shift or reversal (best |r|~0.14),
  so the 0x05 front segment is not a clean sensor-column-ordered raster we can
  FPN-correct with the on-board reference the way `AdjustLightCurve` does live.

**A capture-side experiment (superseded by 6.7):** raise the analog gain via
`0xE1` _before_ scanning so the sensor emits a well-exposed, higher-SNR frame.
An auto-exposure loop was built to try this:

- It writes `0xE1 SET ADJUST DATA` with a persisted gain, offset locked to the
  confirmed dark target `0x60` (96), and the LED, then scans.
- After the scan it measures the produced paper white (p97 of the front segment)
  and applies a proportional auto-exposure step `gain *= (target-96)/(white-96)`,
  persisting the result locally. Re-running converged paper white to a target in
  one or two passes.
- The exact firmware solve (`AdjustAnaproGain` 0x38e2 / `AdjustAnaproOffset`
  0x35c4, targets register `0x4f` and dark `0x60`) needs the device's internal
  gain-multiplier table and a fresh internal-white-strip image, neither reachable
  over the file pipe mid-feed - hence the model-free proportional loop, which
  drives the same target-white quantity `AdjustAnaproGain` does.

This experiment is what motivated abandoning host-side gain overrides (its
findings are below); the shipped path instead replays the firmware's own
closed-loop calibration verbatim (6.7).

### Calibration results (measured on the reference page, 5 passes)

The `0xE1` override **works**: gain 0x80..0xc1 moved the front-segment paper
white 208 -> 230 (firmware default was ~204), with `SET ADJUST` accepted every
run. Two empirical findings, both from matched-window measurements of the same
page at firmware-default exposure vs gain 0xc1 + LED 0xff:

1. **Noise improves.** Normalized (reflectance) paper noise fell 0.097 -> 0.072
   (~30% cleaner) - read/quantization noise is a fixed count so a bigger signal
   above the 96 floor genuinely raises SNR. The rendered background is visibly
   whiter and less mottled.
2. **Faint ink washes out.** In the darkest text window, ink reflectance rose
   0.10 (near-black at default exposure!) to 0.65 at max gain/LED; window text
   contrast fell 0.232 -> 0.176 and the rendered page is fainter overall. The
   extra illumination scatters into thin strokes and lifts them off black.

So maximum exposure is **not** the goal: the default exposure already captures
dark ink and its only defect is noise (handled in post). The loop now defaults
to `--target-white 210` (moderate), and stalls out with a saturation guard when
white stops responding to gain instead of chasing a clipped ceiling.

**LED isolation (measured):** a gain 0xc1 + LED 0x80 run settled it -
halving the LED changed neither the exposure (p50=194, same as LED 0xff) nor the
washout (real-text ink reflectance 0.77-0.83 vs 0.42-0.58 at default exposure in
the title and digit rows). The washout follows the **gain register**, not the
LED; the likely mechanism is a concave firmware tone LUT applied after gain, so
pushing the signal up the curve compresses ink toward paper. Conclusion: the
`0xE1` gain override is a dead end for quality - **the firmware's default
exposure is the best capture**. (Side finding: `offset=0x80` drops the dark
floor to ~17, so the offset register is inverse-acting around the 0x60 -> 96
mapping.)

### The pipeline that closes the gap (measured against the COT reference)

With capture fixed at firmware default, the remaining gap was noise, and it is
mostly _structured_: per-column FPN measures ~8 counts std (the dominant term)
vs ~1.9 row FPN and ~13 residual. Removing it robustly (per-column
median-of-paper normalization, then per-row - `destripe()` in
`cot_pipeline.py`) tightens the paper distribution from +-16 to +-6 counts,
which finally makes COT's own **FC levels stretch** (`makeGammaDataforFC`)
usable without mottling. The full recipe in `process()`:

1. `shade()` - (raw-96)/(white-96) per-column shading (AdjustLightCurve).
2. `destripe()` - column/row fixed-pattern removal (paper-median normalize).
3. `guided_denoise(radius=3, eps=40)` - small radius is critical; radius>=6
   smears 2-4 px strokes into blobs (verified: radius 8 destroyed legibility).
4. `make_gamma_data(150, 222)` - the FC stretch; the measured separation is
   denoised ink p5 ~137 vs destriped paper ~222 on the reference page.
5. 3x3 median + unsharp (r=1.2, 160%) - consolidate strokes, whiten paper.

Result on the reference page: white fraction 81% / dark 3.9% / deep-black 1.9%
against COT's 92.6 / 2.33 / 0.69, with the table digits fully legible and
near-black - by far the closest match yet. The photographic `calcGrayGamma`
path is _not_ used for documents: it is tone-preserving by design and leaves
faint print gray; COT's document look comes from noise removal + the FC
stretch. `calcGrayGamma` remains available for photo-mode rendering.

## 6.7 COT's exact end-to-end sequence (recovered from R10Lite.ds)

Traced statically from the extracted Mac driver `R10Lite.ds` with
`tools/trace_calls.py`. This is the full command choreography COT performs; the
commands in **bold** are ones the early raw-pipe scan did NOT issue (they were
recovered and are all reproduced by the shipped path, 6.7.2).

`CCanoDRDS::PrepareScan` (host-side, before any command):

- reads all capabilities, then calls **`_MakeGammaTable`** (per-channel tone
  LUT built on the host from the brightness/contrast/gamma UI values).

`CCanoDR::StartScan(tagScanParam*)`:

1. `ExecInquiry` -> `ExecTestUnitReady` -> `ExecRequestSense`/`DecodeSense`
2. `ExecInquiryEx` (SDeviceCapabilty)
3. `ExecObjectPosition` (feed/position the sheet)
4. `ExecRead` (a priming read)
5. **`AdjustLight()`** - the calibration pre-scan (see below)
6. `ExecSetWindow`
7. **`ExecDefineScanMode` x3** (opcode **0xD6**) - three parameter pages
   (modes 0,1,2) built from `tagScanParam` fields: mode 0 = image geometry,
   mode 1 = functional flags (deskew/duplex/dropout - bytes 0x34/0x30/0x31/
   0x3b/0x8a4/0x8a7/0x8a9 of tagScanParam), mode 2 = color/side.
8. `ExecRequestSense` -> `ProtectImageBuffer`

`CCanoDR::AdjustLight()` (opcode-level calibration, runs every scan):
`ExecGetMemory(0xD5)` -> **`ExecSetAdjustData(0xE1)`** -> `ExecSetWindow` ->
**`ExecDefineScanMode` x2** -> `ExecScan` -> `ExecRead` -> `ExecObjectPosition`.
So the sensor gain/offset the firmware uses is (re)solved live from a fresh
internal strip read each scan - our static `0xE1` guess can't match it.

`CCanoDR::ScanPage`: `ExecScan(0x1B)` -> `ExecRequestSense`/`DecodeSense`, then
`ReadImage`/`ReadLines` drain via `ExecRead(0x28)`.

Host image pipeline (`CCanoDR::ImageProcessing`, all in `LLiPmDRP215`, the
`Cei::LLiPm::DRHachi` namespace; symbolicated call order via `otool -tV`):
`CImg::createImg` (alloc) -> `AdjustLightFirst/Fix/Next/Last(tagADJUSTINFO,
  side, ref, reflen)` (shading from the device reference) ->
`FilterDuplexFirst/Middle/Last(tagFILTERDUPLEXINFO)` (duplex compositing) ->
`FilterSimplexFirst/Middle/Last` + `NormalFilterSimplex(tagFILTERSIMPLEXINFO,
  bool)` (the enhancement/denoise). `tagFILTERSIMPLEXINFO` embeds several
0x68-byte `tagIMAGEINFO` sub-blocks (offsets +8, +0x70, +0xd8, +0x188).

**Container wire format** (confirmed via `SetupCommandContainer`/
`SetupDataContainer`, matches our working SET WINDOW/SCAN path):

- command container: header `90 01 00 14 00 01 90 00`, opcode at byte 0x0c.
- data-out container: big-endian `(len+8)` at bytes 0..3, then `00 02 b0 00`,
  then the parameter page at byte 0x0c.

**Why the remaining exact bytes need a live capture, not more disassembly:** the
`0xD6` parameter pages and the `_MakeGammaTable` contents are computed at runtime
from capability/UI values (resolution, deskew, dropout, brightness curve), so
reading them off the binary would be guessing the inputs. `tools/pipe_sniffer.py`
reads the raw character device read-only while COT scans and logs every command
block and response verbatim - the ground-truth inputs.

### 6.7.1 Captured ground truth (tools/pipe_sniffer.py, real scan)

A live capture of one COT scan confirmed the exact
per-page sequence and, crucially, three deltas vs our scan:

1. **12-bit capture, not 8-bit.** COT's SET WINDOW byte 0x22 (bit-depth) = `0x0c`
   where ours is `0x08`, and the image READ returns **16-bit** samples. This is
   the dominant quality difference: 16x the tonal resolution, so the smooth
   gradients we were quantizing to graininess are actually captured. Width is
   `0x27e0` (=2552 px @ 300 dpi) vs our `0x27d8` (2550); window byte 8 = `0x01`.
2. **`OBJECT_POSITION` (0x31) feed control.** COT issues `31 01 ...` to feed the
   sheet before scanning and `31 00 ...` to eject after. We never sent it - this
   is also the likely cause of our faster/rougher feed.
3. **Per-scan calibration.** COT does `READ 0x8c` (returns adjust values incl.
   `0x00e1`, `0x0ca4`) then `SET_ADJUST 0xE1` (40-byte page) every scan.

The startup `0x3B` burst is **not** calibration - the INDATA responses decode to
`1f8b08...CaptureOnTouch Lite.app.tar` (gzip) and `PK..basicJ.ocr` (zip), i.e.
COT loading its OCR/app resources from firmware. Ruled out.

Exact per-page choreography (deduped):
`READ 0x8b` (status) -> `OBJECT_POSITION 31 01` (feed) -> `READ 0x8c` (calib)
-> `SET_ADJUST 0xE1` [40B] -> `SET_WINDOW 0x24` [52B, 12-bit] ->
`DEFINE_SCAN_MODE 0xD6` x3 [20B: markers 0e30, 0e32+`0201..40`, 0e36] ->
`SCAN 0x1B` (2-window list) -> `READ 0x28` image in `0x02cdc0`-byte bands ->
`OBJECT_POSITION 31 00` (eject).

Workflow to reproduce byte-exact: quit COT ->
`sudo .venv/bin/python tools/pipe_sniffer.py --slice disk8s1` (atomic payload
capture on the status-pending edge) -> run one scan in COT -> Ctrl-C -> then
`sudo .venv/bin/python tools/cot_replay.py` replays the captured command blocks
verbatim over our pipe and drains the 16-bit frame.

### 6.7.2 The complete choreography, VERIFIED by successful replay

`tools/cot_replay.py` now reproduces a full COT-quality scan end to end
(55,414,128-byte frame, output indistinguishable from the COT reference).
The scan has FOUR phases, and several beliefs from 6.7.1 are corrected here:

1. **Feed** `OBJECT_POSITION 31 01`. Does NOT move the paper to the sensor;
   the sheet sits still through all of phase 2.
2. **~9 closed-loop AGC calibration cycles** (~0.65 s each), paper stationary:
   `SET_ADJUST 0xE1` (evolving) -> `SET_WINDOW` x2 (window id 0 front / 1 back,
   300 dpi, comp 0x05, **12-bit** - the 12-bit depth is calibration-only) ->
   `DEFINE_SCAN_MODE` 0e32(`0201 0000 40`) + 0e36 -> `SCAN` -> one
   183,744-byte band READ -> `OBJECT_POSITION 31 00` (session close, NOT
   eject). **The decisive discovery: SCAN's 2-byte window list selects the
   target.** `ff ff` = internal DARK reference (LED off, measures the black
   floor -> analog offsets `0a/11`), `fe fe` = internal WHITE reference
   (backing strip, drives the gain search `80 -> 95/97 -> 92/95` and the
   per-channel white targets `085d/0b7c/08cf` front, `07a9/0b6a/082f` back).
   Neither moves the paper. Scanning `00 01` (the real document windows)
   starts the feed - which is why every replay before this discovery shot
   the page straight through.
3. **Shading readback**: 77x `READ 0x3B` of 8 KiB from firmware `0x10080000`
   (the per-column shading the firmware built from the white reference).
   Host-side data; our render does not need it - skippable.
4. **Final scan**: `SET_WINDOW` x2 (comp 0x05, **8-bit**, ULy = `0xfffffe28`
   = -472 pre-roll, length 0x3a7e = unlimited) -> `DEFINE_SCAN_MODE`
   0e30 + 0e32(`0201 0000 60` - slow feed) + 0e36 -> `SCAN 00 01` -> READ
   1 MiB chunks until end-of-scan sense. Paper feeds slowly through here.

**Final frame format** (measured on the 55,414,128-byte capture): line stride
15,312 bytes x 3,619 lines, each line = **three 5,104-px 8-bit segments = the
R/G/B channels of the front side at 600 dpi horizontal x 300 dpi vertical**.
Composition 0x05 is a 3-LED-phase color capture; COT's "300 dpi gray" is
channel-averaged and 2:1 horizontally downsampled from this. Raw ink contrast
is ~52% (vs ~18% in our uncalibrated scans) - the calibration, not host
post-processing, is what makes the output pristine.

Render recipe that matches the COT reference (implemented in
`src/r10/render.py`): gray = (R+G+B)/3 -> 2:1 horizontal downsample ->
scale paper to 235 -> `destripe()` -> `make_gamma_data(150, 225)` levels ->
median 3 -> unsharp (r=1.2, 140%) -> rotate -90.

### 6.7.3 Multi-page (ADF batch): one SCAN, one continuous stream (VERIFIED)

Ground truth: a sniffed 3-sheet COT batch (`captures/cot_trace_multipage.jsonl`,
388 states). The wire protocol is **not** scan-per-sheet:

1. **Setup runs once** and is byte-identical to the single-page trace:
   pre-feed reads -> `OBJECT_POSITION` feed -> 9 AGC calibration cycles
   (`ff ff`/`fe fe`, same evolving `SET_ADJUST` trajectory) -> shading
   readback -> final `SET_WINDOW`/`DEFINE_SCAN_MODE`.
2. **`SCAN 00 01` is issued exactly ONCE for the whole batch** (t=33.5 in the
   trace). The unlimited window length (0x3a7e) makes it a batch scan.
3. **All sheets stream back through one continuous run of 1 MiB `READ`s.**
   The firmware auto-feeds each next sheet; the host never issues another
   `SCAN`, `SET_WINDOW`, or `OBJECT_POSITION`. In the trace: page 1
   t=33.6-50.2, page 2 t=50.8-62.3, page 3 t=62.8-75.3.
4. **A page boundary** is a short `READ` (end sense) followed by
   `REQUEST_SENSE` + vendor status reads (`READ 0x80` offsets 4/0/1, 16 B
   each, + `READ 0xa1`, 1 B). The next 1 MiB `READ` then returns the next
   sheet's first chunk.
5. **End of batch**: the `READ` after the last page's status reads is
   REJECTED - CHECK CONDITION with sense key 5 (ILLEGAL REQUEST), asc `0x3a`
   (media not present; later retries return asc `0x2c`) and **no data
   transferred** (the 2 MB data region still holds the previous page's stale
   bytes - do not count them). COT then does `REQUEST_SENSE` + `READ 0x84` +
   `READ 0x8b` (paper now absent) and returns to idle polling. Contrast with
   a normal page boundary: sense key 0, ILI flag `0x20`, residue bytes 3-6
   giving the exact short-read count. All verified on hardware in our
   3-sheet run.

The single-page trace's tail is just a one-page cut of this same pattern -
its `_page_seq` (SCAN -> drain -> sense -> 0x80/0xa1 status reads -> second
big READ -> sense -> 0x84/0x8b) contains every command the batch loop needs.
`scan_batch()` in `src/r10/cot_scan.py` slices it structurally: setup +
`SCAN 00 01` once, then per page: drain until end sense, yield, run the
inter-page status reads; stop when a drain returns < 1 MiB.

An earlier implementation re-issued `SCAN 00 01` per sheet; each re-SCAN
started a fresh feed cycle and one sheet passed through uncaptured between
pages (3 sheets in -> pages 1 and 3 out). The driver-binary call structure
(`StartScan` once / `ScanPage` per sheet) describes Canon's app layering,
not the wire protocol - the sniffer trace is authoritative.

### 6.7.4 Calibration caching (instant scan, VERIFIED on hardware)

The setup phase splits cleanly, and one structural fact enables caching: the
feed (`OBJECT_POSITION 01`, issuance 42 of 195) is the ONLY setup command
that needs paper. The 9 AGC cycles image the scanner's internal dark/white
references stationary, so the whole calibration block - cycles plus shading
readback (issuances 43-178) - runs fine with an EMPTY feeder, and the
converged register + shading state persists in the device for as long as it
stays powered and claimed. `CotScanner` slices the bundled sequence into:

- **arm** (0-41): the pre-feed arming reads (TUR / INQUIRY / `READ
  0x84/0x8b/0x8c` loops)
- **feed** (42): `OBJECT_POSITION 01`
- **calibration** (43-178): 9 AGC cycles + firmware shading readback
- **final window** (179-183): `SET_WINDOW` x2, `DEFINE_SCAN_MODE` x3
- **document scan** (184+): `SCAN 00 01` + the 1 MiB drain stream

`warm_calibrate()` runs arm + calibration with no feed;
`scan_batch(use_cached_calibration=True)` then runs arm -> feed -> final
window -> document scan, skipping the cycles and the ~600 KiB shading
readback entirely. Time-to-first-feed drops from ~60-90 s to a few seconds.

The HTTP service builds on this: a background calibrator claims the device
at startup and re-runs `warm_calibrate()` whenever the calibration is older
than `--calib-interval` seconds (default 300), so gains never drift far from
the LED's current operating point. `/scan` defaults to
`?calibration=cached` (falling back to the full choreography if nothing is
warm yet) and, if a background calibration is mid-flight, waits for it and
starts the moment it finishes; `?calibration=full` runs the complete
per-scan choreography (useful for A/B quality comparison - responses carry
`X-Calibration` / `X-Calibration-Age` headers). Between calibration cycles
the calibrator refreshes a host-side paper hint, so `/status` keeps
reporting the feeder state as if the scanner were idle while a calibration
holds the pipe.

Hardware verification: the firmware accepts the calibration block with an
empty feeder (startup warm calibration, ~60 s, `paper_present` false
throughout), and a subsequent cached-calibration scan fed immediately with
full output quality. Known drift consideration: gains move with LED
warm-up/temperature, which the periodic refresh bounds; a single-cycle
refresh variant (feed -> one dark + one white cycle -> scan) exists as a
fallback design should no-feed calibration ever misbehave.

## 7. Open questions / future experiments

1. **`SAdjustInfo` field semantics + current/default gain** (6.5 step 4): confirm
   which bytes are gain vs offset and the power-on default `curGain` before
   issuing the `0xE1` write. First validate by dumping the 0x10080000 reference
   read-only.
2. **Host shading** (6.5 step 5): port `makeShadingData` from the disassembly.
3. **Color / lineart framing**: only gray is confirmed; color plane order and
   1-bit packing unknown.
4. **Duplex back-side delivery** (end-of-scan sensing is confirmed, 6.2). Front
   simplex is confirmed exact (2,105,400 B = 1276x1650).

## 8. Reverse-engineering toolkit (this repo)

Active tools (product + the pipeline that produced the verified capture):

- `tools/pipe_sniffer.py` - non-invasive raw-block-device sniffer that logs
  every distinct state of `transfer.dat`'s command sector during a real COT
  scan. Produced the ground-truth trace (section 6.7.1/6.7.2).
- `tools/cot_replay.py` - replays the captured choreography byte-for-byte and
  drains the frame. It generated the bundled `src/r10/data/cot_sequence.json`
  that the product (`r10.cot_scan.CotScanner`) now ships.
- `tools/pipe_probe.py` - read-only probe over the raw pipe (INQUIRY, sense).
- `tools/disasm_macho.py` - Mach-O x86_64 symbol disassembler used to reverse
  `R10Lite.ds` / `LLiPmDRP215` (recovered the calibration protocol, §6.5).
- `tools/trace_calls.py` - annotated call-sequence tracer (which `Exec*`
  commands a function issues, in order); recovered the choreography in §6.7.
- `tools/llipm_bridge.py` + `tools/setup_llipm.sh` - `ctypes` bridge that loads
  Canon's `LLiPmDRP215` under Rosetta (validated `_RemoveShadow`, §6.6).

Removed tooling (superseded; described in `docs/PROCESS.md`): the direct-USB /
Bulk-Only Transport library and its tools, an exploratory scan/calibration
harness and gain/archive probes, and one-shot static-analysis helpers. Their
verified conclusions are distilled into `src/r10/cot_pipeline.py` and
`src/r10/render.py`. Re-deriving any of them only requires Canon's own bundled
binaries (which are not redistributed here) plus `tools/disasm_macho.py` +
`tools/trace_calls.py`, which retain the disassembly capability.

For the full narrative - what was tried, what failed, and why the final design
looks the way it does - see `docs/PROCESS.md`.
