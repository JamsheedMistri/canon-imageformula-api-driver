#!/usr/bin/env python3
"""ctypes bridge to Canon's real LLiPm image-processing module.

Rationale (docs/protocol.md 6.6): CaptureOnTouch's pristine output comes from
running the raw sensor image through Canon's ``LLiPmDRP215`` module - background/
shadow removal, gamma, despeckle, adaptive processing. Reimplementing that by
hand is guesswork; instead we load Canon's ACTUAL x86_64 library (pulled off the
device) under Rosetta and call it on our captured bytes for identical results.

Prereq: run ``tools/setup_llipm.sh`` once to stage the libraries in /tmp/cotfw,
then run this module under an x86_64 interpreter:

    tools/setup_llipm.sh
    arch -x86_64 /usr/bin/python3 tools/llipm_bridge.py <front_raw.bin> <w> <h>

``tagCEIIMAGEINFO`` layout was recovered by disassembling llipm (_GetHistogram,
to_gray_image, _IsGrayImage); see docs/protocol.md 6.6:

    off 0x00 u64  struct/header size (0x68)
    off 0x08 u64  pixel data pointer
    off 0x20 u64  width  (pixels)
    off 0x28 u64  height (lines)
    off 0x30 u64  bytes per line (stride)
    off 0x38 u64  total buffer length in bytes
    off 0x40 u64  (resolution / bit context - under study)
    off 0x48 u64  bytes per pixel (1 = gray, 3 = packed RGB)
    off 0x50 u32  color-packing flag (1 = planar)
"""
from __future__ import annotations

import ctypes
import platform
import sys

LIBDIR = "/tmp/cotfw"
CEIIMAGEINFO_SIZE = 0x68


class CEIIMAGEINFO(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint64),        # 0x00
        ("data", ctypes.c_void_p),        # 0x08
        ("_pad10", ctypes.c_uint64),      # 0x10
        ("_pad18", ctypes.c_uint64),      # 0x18
        ("width", ctypes.c_uint64),       # 0x20
        ("height", ctypes.c_uint64),      # 0x28
        ("stride", ctypes.c_uint64),      # 0x30
        ("length", ctypes.c_uint64),      # 0x38
        ("res", ctypes.c_uint64),         # 0x40
        ("bpp", ctypes.c_uint64),         # 0x48
        ("packflag", ctypes.c_uint32),    # 0x50
        ("_pad54", ctypes.c_uint32),      # 0x54
        ("_pad58", ctypes.c_uint64),      # 0x58
        ("_pad60", ctypes.c_uint64),      # 0x60 (mode, read by RemoveShadow)
    ]


assert ctypes.sizeof(CEIIMAGEINFO) >= CEIIMAGEINFO_SIZE


def load() -> ctypes.CDLL:
    """Load Canon's LLiPm (must be x86_64 interpreter; see setup_llipm.sh)."""
    if platform.machine() != "x86_64":
        raise RuntimeError(
            "run under x86_64 Rosetta: arch -x86_64 /usr/bin/python3 ...")
    for dep in ("pafcv2", "rdd20"):
        ctypes.CDLL(f"{LIBDIR}/{dep}", mode=ctypes.RTLD_GLOBAL)
    return ctypes.CDLL(f"{LIBDIR}/LLiPmDRP215")


class REMOVE_SHADOW_INFO(ctypes.Structure):
    """tagREMOVE_SHADOW_INFO, recovered from InitRemoveShadowInfo (6.6).

    All the "param" fields fall back to Canon's defaults when 0, and every length
    is rescaled by image dpi / 300 internally, so a zeroed struct with just the
    size and lines-to-process set reproduces COT's default RemoveShadow.
    """
    _fields_ = [
        ("size", ctypes.c_uint32),      # 0x00  >=0x20 to enable last field
        ("_pad04", ctypes.c_int32),     # 0x04
        ("paramA", ctypes.c_int32),     # 0x08  default 850 when <=0
        ("paramB", ctypes.c_int32),     # 0x0c  default 500 when <=0
        ("paramC", ctypes.c_int32),     # 0x10  default 15 when <=0
        ("mode", ctypes.c_int32),       # 0x14  -> proc_info[4]
        ("lines", ctypes.c_int32),      # 0x18  lines-to-process (range checked)
        ("scale", ctypes.c_int32),      # 0x1c  default 2000 when 0 (needs size>=0x20)
    ]


def make_gray_image(buf, width: int, height: int, dpi: int = 300) -> CEIIMAGEINFO:
    """Wrap an 8-bit gray buffer as a tagCEIIMAGEINFO."""
    info = CEIIMAGEINFO()
    info.size = CEIIMAGEINFO_SIZE
    info.data = ctypes.cast(buf, ctypes.c_void_p)
    info.width = width
    info.height = height
    info.stride = width
    info.length = width * height
    info.res = 8
    info.bpp = 1
    info.packflag = 1
    info._pad60 = dpi  # 0x60: resolution, used to rescale shadow lengths
    return info


def run_remove_shadow(llipm, in_path: str, out_path: str, width: int,
                      height: int, dpi: int, lines: int) -> int:
    data = bytearray(open(in_path, "rb").read())
    assert len(data) >= width * height, (len(data), width * height)
    buf = (ctypes.c_uint8 * len(data)).from_buffer(data)
    img = make_gray_image(buf, width, height, dpi)
    info = REMOVE_SHADOW_INFO()
    info.size = 0x20
    info.lines = lines if lines > 0 else height
    fn = llipm.RemoveShadow
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    st = fn(ctypes.byref(img), ctypes.byref(info))
    print(f"RemoveShadow(lines={info.lines}) -> status=0x{st & 0xffffffff:08x}")
    if st == 0:
        open(out_path, "wb").write(bytes(buf))
        print(f"wrote {out_path} ({len(data)} bytes, in-place processed)")
    return st


def main() -> int:
    llipm = load()
    if len(sys.argv) >= 6 and sys.argv[1] == "removeshadow":
        _, _, in_path, out_path, w, h = sys.argv[:6]
        dpi = int(sys.argv[6]) if len(sys.argv) > 6 else 300
        lines = int(sys.argv[7]) if len(sys.argv) > 7 else 0
        return run_remove_shadow(llipm, in_path, out_path, int(w), int(h), dpi, lines)
    print("LLiPm loaded; exported RemoveShadow:", hasattr(llipm, "RemoveShadow"),
          "CustomColorGamma:", hasattr(llipm, "CustomColorGamma"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
