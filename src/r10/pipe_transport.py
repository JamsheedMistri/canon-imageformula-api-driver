"""Filesystem-pipe transport for the Canon R10 (the CONFIRMED protocol).

Reverse engineering of the onboard ``ONTOUCHL.exe`` (see ``docs/protocol.md``
sections 3.2-3.3) showed the R10 is driven not by raw SCSI passthrough but by
reading/writing two pipe files on its removable FAT volume:

  * A 28-byte **command block** (SCSI CDB at offset 0x0c) is written to
    ``transfer.dat``.
  * The host polls ``transfer.dat`` offset 0x18 until the firmware changes it
    from ``0xFFFFFFFF`` to a status code (0 == success).
  * Response/image **data** is read back from ``INDATA.dat``.

macOS's FAT driver won't push writes to the exact sectors the firmware watches,
so this transport talks to the **raw block device** (``/dev/rdiskNsM``) while
the volume is unmounted, replicating the Windows app's
``FILE_FLAG_NO_BUFFERING`` sector-exact I/O. This requires **root**.

This mirrors ``CCeiFileIOLite::ExecRead/ExecWrite/ExecNone`` and is verified
against the real device (INQUIRY returns peripheral type 0x06 = scanner,
``CANON``/``R10``/``2.02``).
"""
from __future__ import annotations

import os
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .errors import R10Error, TransportError

SECTOR = 512

# Command-block header constants (from ExecRead @ 0x4026c0 / builder @ 0x402ff0).
_HDR_B3 = 0x14
_HDR_B5 = 0x01
_HDR_B6 = 0x90
_STATUS_OFF = 0x18
_STATUS_PENDING = 0xFFFFFFFF
_CDB_OFF = 0x0C
_DATA_OUT_OFF = 0x28


@dataclass
class Bpb:
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    root_entries: int
    sectors_per_fat: int

    @property
    def root_dir_sectors(self) -> int:
        return ((self.root_entries * 32) + self.bytes_per_sector - 1) // self.bytes_per_sector

    @property
    def data_start_sector(self) -> int:
        return self.reserved_sectors + self.num_fats * self.sectors_per_fat + self.root_dir_sectors

    @property
    def root_dir_start_sector(self) -> int:
        return self.reserved_sectors + self.num_fats * self.sectors_per_fat

    def cluster_to_sector(self, cluster: int) -> int:
        return self.data_start_sector + (cluster - 2) * self.sectors_per_cluster


def _parse_bpb(boot: bytes) -> Bpb:
    return Bpb(
        bytes_per_sector=struct.unpack_from("<H", boot, 0x0B)[0],
        sectors_per_cluster=boot[0x0D],
        reserved_sectors=struct.unpack_from("<H", boot, 0x0E)[0],
        num_fats=boot[0x10],
        root_entries=struct.unpack_from("<H", boot, 0x11)[0],
        sectors_per_fat=struct.unpack_from("<H", boot, 0x16)[0],
    )


def _fat_name(entry: bytes) -> str:
    name = entry[0:8].decode("latin1").rstrip()
    ext = entry[8:11].decode("latin1").rstrip()
    return f"{name}.{ext}" if ext else name


def _roundup(n: int, m: int = SECTOR) -> int:
    return ((n + m - 1) // m) * m


class RawPipeTransport:
    """Sector-exact command/data pipe over the raw block device (macOS, root).

    Typical use::

        with RawPipeTransport() as pipe:
            status, data = pipe.exec_read(bytes([0x12, 0, 0, 0, 0x24, 0]), 36)
    """

    def __init__(self, slice_dev: str = "disk8s1", *, timeout_s: float = 8.0,
                 remount_on_close: bool = True) -> None:
        s = slice_dev.replace("/dev/", "").replace("rdisk", "disk")
        self._slice = s
        self._raw = f"/dev/r{s}"
        self._timeout_s = timeout_s
        self._remount = remount_on_close
        self._fd: Optional[int] = None
        self._bpb: Optional[Bpb] = None
        self._in_off = 0
        self._tr_off = 0

    @staticmethod
    def _sh(*args: str) -> str:
        return subprocess.run(args, capture_output=True, text=True).stdout.strip()

    # -- lifecycle --------------------------------------------------------
    def open(self) -> "RawPipeTransport":
        self._sh("diskutil", "unmount", f"/dev/{self._slice}")
        try:
            self._fd = os.open(self._raw, os.O_RDWR)
        except PermissionError as exc:
            raise TransportError(
                f"cannot open {self._raw} for writing - run as root (sudo)."
            ) from exc
        except FileNotFoundError as exc:
            raise TransportError(f"{self._raw} not found (pass the correct slice).") from exc
        self._map_fat()
        return self

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        if self._remount:
            self._sh("diskutil", "mount", f"/dev/{self._slice}")

    def __enter__(self) -> "RawPipeTransport":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- FAT mapping ------------------------------------------------------
    def _map_fat(self) -> None:
        assert self._fd is not None
        boot = os.pread(self._fd, SECTOR, 0)
        bpb = _parse_bpb(boot)
        self._bpb = bpb
        root_off = bpb.root_dir_start_sector * bpb.bytes_per_sector
        root_bytes = bpb.root_dir_sectors * bpb.bytes_per_sector
        data = os.pread(self._fd, root_bytes, root_off)
        found = {}
        for i in range(0, len(data), 32):
            entry = data[i:i + 32]
            if not entry or entry[0] in (0x00, 0xE5):
                continue
            attr = entry[11]
            if attr & 0x08 or (attr & 0x0F) == 0x0F:
                continue
            name = _fat_name(entry).upper()
            if name in ("INDATA.DAT", "TRANSFER.DAT"):
                start = struct.unpack_from("<H", entry, 0x1A)[0]
                found[name] = start
        if "INDATA.DAT" not in found or "TRANSFER.DAT" not in found:
            raise TransportError(f"pipe files not found on {self._raw}: {found}")
        self._in_off = bpb.cluster_to_sector(found["INDATA.DAT"]) * bpb.bytes_per_sector
        self._tr_off = bpb.cluster_to_sector(found["TRANSFER.DAT"]) * bpb.bytes_per_sector

    @property
    def indata_lba(self) -> int:
        return self._in_off // SECTOR

    @property
    def transfer_lba(self) -> int:
        return self._tr_off // SECTOR

    # -- command primitives ----------------------------------------------
    def _build_block(self, cdb: bytes, data: Optional[bytes]) -> bytes:
        if len(cdb) > 12:
            raise R10Error("CDB must be <= 12 bytes")
        size = _DATA_OUT_OFF + len(data) if data else _DATA_OUT_OFF
        buf = bytearray(_roundup(max(size, SECTOR)))
        buf[3] = _HDR_B3
        buf[5] = _HDR_B5
        buf[6] = _HDR_B6
        buf[_CDB_OFF:_CDB_OFF + len(cdb)] = cdb
        struct.pack_into("<I", buf, _STATUS_OFF, _STATUS_PENDING)
        if data:
            struct.pack_into(">I", buf, 0x1C, len(data) + 8)
            buf[0x21] = 0x02
            buf[0x22] = 0xB0
            buf[_DATA_OUT_OFF:_DATA_OUT_OFF + len(data)] = data
        return bytes(buf)

    def _send_and_wait(self, block: bytes) -> int:
        assert self._fd is not None
        os.pwrite(self._fd, block, self._tr_off)
        os.fsync(self._fd)
        deadline = time.time() + self._timeout_s
        status = _STATUS_PENDING
        while time.time() < deadline:
            sec = os.pread(self._fd, SECTOR, self._tr_off)
            status = struct.unpack_from("<I", sec, _STATUS_OFF)[0]
            if status != _STATUS_PENDING:
                return status
            time.sleep(0.05)
        raise TransportError("command timed out (status stayed 0xFFFFFFFF)")

    def _read_data(self, n: int) -> bytes:
        assert self._fd is not None
        out = bytearray()
        off = self._in_off
        remaining = n
        while remaining > 0:
            want = _roundup(min(remaining, SECTOR * 4096))  # <= 2 MiB pipe window
            chunk = os.pread(self._fd, want, off)
            out.extend(chunk)
            remaining -= len(chunk)
            if len(chunk) < want:
                break
        return bytes(out[:n])

    def clear_data(self, n: int = SECTOR) -> None:
        """Zero the first ``n`` bytes of INDATA.dat so a stale payload can't be
        mistaken for a fresh response when probing."""
        assert self._fd is not None
        os.pwrite(self._fd, b"\x00" * _roundup(n), self._in_off)
        os.fsync(self._fd)

    # -- public API (mirrors CCeiFileIOLite Exec*) ------------------------
    def exec_none(self, cdb: bytes) -> int:
        """Issue a command with no data phase; returns the status word."""
        return self._send_and_wait(self._build_block(cdb, None))

    def exec_read(self, cdb: bytes, data_len: int) -> Tuple[int, bytes]:
        """Issue a data-in command; returns (status, data[:data_len])."""
        status = self._send_and_wait(self._build_block(cdb, None))
        data = self._read_data(data_len) if data_len else b""
        return status, data

    def exec_write(self, cdb: bytes, data: bytes) -> int:
        """Issue a data-out command (payload embedded in the block)."""
        return self._send_and_wait(self._build_block(cdb, data))
