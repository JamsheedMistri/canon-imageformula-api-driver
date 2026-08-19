"""Product scanner: CaptureOnTouch's EXACT verified choreography as a library.

This module drives a full COT-quality scan over the raw file-tunnel pipe
(``RawPipeTransport``). The command sequence is the byte-for-byte choreography
captured from a real CaptureOnTouch scan and VERIFIED by successful replay
(docs/protocol.md 6.7.2): pre-feed arming reads -> OBJECT_POSITION feed ->
9 stationary AGC calibration cycles against the internal dark (SCAN ``ff ff``)
and white (SCAN ``fe fe``) references -> shading readback -> final 8-bit
slow-feed document scan (SCAN ``00 01``) drained in 1 MiB chunks.

The sequence ships as package data (``data/cot_sequence.json``, generated once
from a sniffer trace by the replay tooling) so the library has no external
dependency. The SET_ADJUST gain trajectory in it is the closed-loop search COT
ran on this device; replaying it verbatim reproduces COT's capture quality.
Live AGC (recomputing gains from the READ 0x8C measurements) and cached
single-cycle calibration are future work - see protocol.md section 7.0.

Requires root (raw block-device access):

    sudo .venv/bin/python service/app.py
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from .errors import NoPaperError, ScsiError
from .pipe_transport import RawPipeTransport

_SEQUENCE_FILE = Path(__file__).parent / "data" / "cot_sequence.json"

IMG_CHUNK = 0x100000  # final-drain READ size used by COT

CDB_TEST_UNIT_READY = bytes(12)
CDB_REQUEST_SENSE = bytes([0x03, 0, 0, 0, 0x12, 0]) + bytes(6)
CDB_INQUIRY = bytes([0x12, 0, 0, 0, 0x60, 0]) + bytes(6)
# Vendor status page 0x8B: byte 0 = paper-in-feeder flag (01 loaded / 00 empty;
# observed flipping exactly at feed/eject in the COT trace).
CDB_PAPER_STATUS = bytes([0x28, 0, 0x8B, 0, 0, 0, 0, 0, 0x01, 0, 0, 0])


def scsi_status(word: int) -> int:
    return (word >> 24) & 0xFF


def _is_end_sense(sense: bytes) -> bool:
    """End-of-scan detection, confirmed on hardware (protocol.md 6.2)."""
    if len(sense) < 14:
        return False
    flags = sense[2]
    key = flags & 0x0F
    if flags & 0x60:           # EOM or ILI
        return True
    if key in (0x05, 0x08):    # ILLEGAL REQUEST (post-scan) / BLANK CHECK
        return True
    asc, ascq = sense[12], sense[13]
    if asc in (0x3A, 0x80):    # media not present / vendor end-of-paper
        return True
    if asc == 0x00 and ascq in (0x02, 0x04, 0x05, 0x06):
        return True
    return False


def find_r10_slice() -> Optional[str]:
    """Locate the R10's ONTOUCHLITE FAT slice (the disk number changes
    between plug-ins). Returns e.g. ``disk8s1`` or None if not attached."""
    out = subprocess.run(["diskutil", "list"], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        if "ONTOUCHLITE" in line:
            m = re.search(r"(disk\d+s\d+)\s*$", line)
            if m:
                return m.group(1)
    return None


def load_sequence() -> list[dict]:
    seq = json.loads(_SEQUENCE_FILE.read_text())
    for r in seq:
        r["cdb_bytes"] = bytes.fromhex(r["cdb"])
        r["payload_bytes"] = (bytes.fromhex(r["payload"])
                              if r.get("payload") else None)
    return seq


class CotScanner:
    """High-level scanner running the verified COT choreography.

    Holds the raw pipe open for its lifetime (the volume stays unmounted);
    use as a context manager or call :meth:`close` to remount.
    """

    def __init__(self, slice_dev: str = "disk8s1", *, pace: float = 0.15,
                 skip_shading: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.slice_dev = slice_dev
        self.pace = pace
        self.skip_shading = skip_shading
        self.log = log or (lambda s: None)
        self._pipe: Optional[RawPipeTransport] = None
        self._seq = load_sequence()
        # Split the flat single-page trace into setup vs page phases (verified
        # against the multi-page sniffer trace, docs/protocol.md 6.7.3): the
        # setup (pre-feed reads, OBJECT_POSITION feed, AdjustLight calibration
        # cycles, SET_WINDOW/DEFINE) and the document SCAN 00 01 each run ONCE
        # per batch; every sheet is then drained from one continuous READ
        # stream, with the firmware auto-feeding between pages. The document
        # scan is the last SCAN whose window list is 00 01; everything before
        # it is setup.
        doc = max(i for i, r in enumerate(self._seq)
                  if r["cdb_bytes"][0] == 0x1B
                  and r["payload_bytes"] == b"\x00\x01")
        self._setup_seq = self._seq[:doc]
        self._page_seq = self._seq[doc:]

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "CotScanner":
        if self._pipe is None:
            self._pipe = RawPipeTransport(self.slice_dev, timeout_s=90.0).open()
            self._clear_unit_attention()
        return self

    def close(self) -> None:
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None

    def __enter__(self) -> "CotScanner":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def pipe(self) -> RawPipeTransport:
        if self._pipe is None:
            raise RuntimeError("scanner is not open")
        return self._pipe

    # -- primitives --------------------------------------------------------

    def _sense(self) -> tuple:
        st, data = self.pipe.exec_read(CDB_REQUEST_SENSE, 0x12)
        if len(data) >= 14:
            return data[2] & 0x0F, data[12], data[13], data
        return None, None, None, data

    def _clear_unit_attention(self, tries: int = 5) -> None:
        for _ in range(tries):
            if scsi_status(self.pipe.exec_none(CDB_TEST_UNIT_READY)) == 0:
                return
            self._sense()
            time.sleep(0.2)

    # -- public queries ----------------------------------------------------

    def device_info(self) -> dict:
        st, data = self.pipe.exec_read(CDB_INQUIRY, 0x60)
        if scsi_status(st) != 0 or len(data) < 36:
            raise ScsiError(f"INQUIRY failed (status 0x{st:08x})")
        return {
            "vendor": data[8:16].decode("ascii", "replace").strip(),
            "product": data[16:32].decode("ascii", "replace").strip(),
            "revision": data[32:36].decode("ascii", "replace").strip(),
        }

    def paper_present(self) -> bool:
        st, data = self.pipe.exec_read(CDB_PAPER_STATUS, 1)
        if scsi_status(st) != 0:
            self._sense()
            return False
        return bool(data and data[0] == 0x01)

    # -- scanning ----------------------------------------------------------

    def _run(self, seq: list[dict], image: bytearray) -> None:
        """Execute one phase of the choreography, draining any document image
        into ``image``."""
        for r in seq:
            cdb: bytes = r["cdb_bytes"]
            op = cdb[0]
            if op == 0x3B and self.skip_shading:
                continue
            if op == 0x03:                       # REQUEST_SENSE checkpoints
                self._sense()
            elif op == 0x31:                     # OBJECT_POSITION
                st = self.pipe.exec_none(cdb)
                if scsi_status(st) != 0:
                    key, asc, ascq, _ = self._sense()
                    if cdb[1] == 0x01:
                        raise NoPaperError(
                            f"feed failed - feeder empty? "
                            f"(sense {key}/{asc:#x}/{ascq:#x})")
                self.log(f"OBJECT_POSITION {cdb[1]:02x}")
            elif op in (0x12, 0x3B):             # INQUIRY / shading read
                n = cdb[4] if op == 0x12 else (
                    (cdb[6] << 16) | (cdb[7] << 8) | cdb[8])
                self.pipe.exec_read(cdb, n)
            elif op == 0x28:
                n = (cdb[6] << 16) | (cdb[7] << 8) | cdb[8]
                if cdb[2] == 0x00 and n >= IMG_CHUNK:
                    if not image:                # drain once
                        self.log("draining image ...")
                        self._drain(cdb, n, image)
                elif cdb[2] == 0x00:             # calibration band
                    st, _ = self.pipe.exec_read(cdb, n)
                    if scsi_status(st) != 0:
                        self._sense()
                    self.log(f"calibration band ({n} B)")
                else:                            # vendor status page
                    self.pipe.exec_read(cdb, n or 0x80)
            elif r["payload_bytes"] is not None:  # SET_ADJUST/WINDOW/DEFINE/SCAN
                st = self.pipe.exec_write(cdb, r["payload_bytes"])
                if scsi_status(st) != 0:
                    key, asc, ascq, _ = self._sense()
                    raise ScsiError(f"{r['op']} rejected",
                                    sense_key=key, asc=asc, ascq=ascq)
                self.log(f"{r['op']} {r['payload'][:16]}")
            else:                                # TEST_UNIT_READY etc.
                if scsi_status(self.pipe.exec_none(cdb)) != 0:
                    self._sense()
            time.sleep(self.pace)

    def calibrate(self) -> None:
        """Run the once-per-batch setup phase (feed + AdjustLight calibration
        + window/mode setup). Leaves the first sheet staged, ready for the
        document SCAN."""
        self._run(self._setup_seq, bytearray())

    def scan_page(self) -> bytes:
        """Scan a single page (a batch of one - same streaming path as
        :meth:`scan_batch`).

        The raw frame is 15,312-byte lines of three 5,104-px 8-bit segments
        (R/G/B at 600x300 dpi) - decode with :mod:`r10.render`.
        Raises :class:`NoPaperError` if the feeder is empty.
        """
        pages = list(self.scan_batch(max_pages=1))
        if not pages:
            raise ScsiError("scan produced no image data")
        return pages[0]

    def _drain(self, cdb: bytes, chunk: int, image: bytearray) -> None:
        for i in range(1, 20000):
            st, data = self.pipe.exec_read(cdb, chunk)
            if scsi_status(st) != 0:
                key, asc, ascq, sense = self._sense()
                valid = len(data.rstrip(b"\x00"))
                if key == 0x05:
                    # ILLEGAL REQUEST: the READ was rejected outright (e.g.
                    # asc 0x3a media-not-present / 0x2c after the last sheet),
                    # so nothing was transferred - whatever sits in the data
                    # region is stale bytes from the previous page.
                    valid = 0
                # NO SENSE + ILI + valid residue gives the exact short count
                elif key is not None and (sense[2] & 0x20) and (sense[0] & 0x80):
                    residue = int.from_bytes(sense[3:7], "big")
                    if 0 <= residue <= chunk:
                        valid = chunk - residue
                image.extend(data[:valid])
                # log the raw sense: in ADF batches the end-of-PAGE sense may
                # differ from end-of-BATCH (asc/ascq distinguish them)
                self.log(f"READ #{i}: short ({valid} B), end={_is_end_sense(sense)} "
                         f"sense key={key} asc={asc:#04x} ascq={ascq:#04x} "
                         f"flags={sense[2]:#04x} raw={sense[:14].hex()}")
                if _is_end_sense(sense):
                    return
            else:
                image.extend(data)
                self.log(f"READ #{i}: {chunk} B")
            time.sleep(self.pace)

    def scan_batch(self, max_pages: int = 10) -> Iterator[bytes]:
        """Scan every sheet in the feeder (up to ``max_pages``), yielding one
        raw frame per sheet.

        Byte-for-byte the choreography COT uses for ADF batches (captured in
        ``captures/cot_trace_multipage.jsonl``): calibrate once, issue SCAN
        ``00 01`` ONCE, then read the batch as one continuous stream. The
        firmware auto-feeds each next sheet; a page boundary is a short READ
        (end sense) followed by REQUEST_SENSE + the vendor status reads
        (READ 0x80 x3, READ 0xa1), after which the next 1 MiB READ returns the
        next sheet's data. The stream is over when a page drain comes back
        (near-)empty. Re-issuing SCAN per sheet - the old behaviour - started
        a fresh feed cycle each time and ejected an uncaptured sheet between
        pages.
        """
        # Structural slices of the bundled page phase: it contains exactly two
        # 1 MiB document READs; between them sit the inter-page status reads,
        # and after the second comes the end-of-batch tail (sense + paper
        # check). COT's single-page trace is just a one-page cut of the same
        # streaming pattern.
        big = [i for i, r in enumerate(self._page_seq)
               if r["cdb_bytes"][0] == 0x28 and r["cdb_bytes"][2] == 0x00
               and ((r["cdb_bytes"][6] << 16) | (r["cdb_bytes"][7] << 8)
                    | r["cdb_bytes"][8]) >= IMG_CHUNK]
        scan_cmds = self._page_seq[:big[0]]              # SCAN 00 01, once
        drain_cdb = self._page_seq[big[0]]["cdb_bytes"]
        page_break = self._page_seq[big[0] + 1:big[1]]   # sense + 0x80/0xa1
        tail = self._page_seq[big[1] + 1:]               # final sense + paper

        self.calibrate()                 # feed sheet 1 + AGC calibration, once
        self._run(scan_cmds, bytearray())
        for page in range(max_pages):
            image = bytearray()
            self.log(f"page {page + 1}: draining stream ...")
            self._drain(drain_cdb, IMG_CHUNK, image)
            if len(image) < IMG_CHUNK:   # terminator read: no sheet was staged
                self.log(f"stream exhausted after {page} page(s) "
                         f"({len(image)} residual B)")
                break
            yield bytes(image)
            self._run(page_break, bytearray())
        self._run(tail, bytearray())
