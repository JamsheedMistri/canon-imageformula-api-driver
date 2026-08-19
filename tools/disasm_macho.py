#!/usr/bin/env python3
"""Disassemble named functions in a Mach-O x86_64 binary (capstone).

Used to reverse-engineer the CaptureOnTouch launcher's archive-read path so we
can pull the packed app off the device. Resolves symbol -> file offset via the
__TEXT,__text section and disassembles until the next symbol (or a byte budget).

Usage:
    tools/disasm_macho.py <binary> <mangled_symbol> [more_symbols...] [--bytes N]
    tools/disasm_macho.py <binary> --list           # list text symbols
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_64


def parse(path: Path):
    data = path.read_bytes()
    magic = struct.unpack_from("<I", data, 0)[0]
    assert magic in (0xFEEDFACF, 0xCFFAEDFE), f"not Mach-O 64: {magic:#x}"
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    text = None            # (vmaddr, fileoff, size)
    syms = []              # (name, value)
    segs = []              # (vmaddr, fileoff, filesize) for vaddr->fileoff mapping
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            segname = data[off + 8:off + 24].rstrip(b"\x00")
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from(
                "<QQQQ", data, off + 24)
            if filesize:
                segs.append((vmaddr, fileoff, filesize))
            nsects = struct.unpack_from("<I", data, off + 64)[0]
            so = off + 72
            for _ in range(nsects):
                sname = data[so:so + 16].rstrip(b"\x00")
                addr, size = struct.unpack_from("<QQ", data, so + 32)
                foff = struct.unpack_from("<I", data, so + 48)[0]
                if segname == b"__TEXT" and sname == b"__text":
                    text = (addr, foff, size)
                so += 80
        elif cmd == 0x2:  # LC_SYMTAB
            symoff, nsyms, stroff, strsize = struct.unpack_from(
                "<IIII", data, off + 8)
            for i in range(nsyms):
                n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from(
                    "<IBBHQ", data, symoff + i * 16)
                if n_strx == 0:
                    continue
                end = data.index(b"\x00", stroff + n_strx)
                name = data[stroff + n_strx:end].decode("latin1")
                if n_value:
                    syms.append((name, n_value))
        off += cmdsize
    return data, text, syms, segs


def vaddr_to_foff(segs, vaddr):
    for vm, fo, fs in segs:
        if vm <= vaddr < vm + fs:
            return fo + (vaddr - vm)
    return None


def main() -> int:
    args = sys.argv[1:]
    path = Path(args[0])
    data, text, syms, segs = parse(path)
    vmaddr, foff, size = text
    text_end = vmaddr + size
    md = Cs(CS_ARCH_X86, CS_MODE_64)

    if "--list" in args:
        for name, val in sorted(syms, key=lambda s: s[1]):
            if vmaddr <= val < text_end:
                print(f"0x{val:x}  {name}")
        return 0

    budget = 400
    if "--bytes" in args:
        i = args.index("--bytes")
        budget = int(args[i + 1])
        args = args[:i] + args[i + 2:]

    # --data 0xVADDR N : hexdump N bytes at a virtual address
    if "--data" in args:
        i = args.index("--data")
        va = int(args[i + 1], 0)
        n = int(args[i + 2], 0)
        fo = vaddr_to_foff(segs, va)
        if fo is None:
            print(f"vaddr 0x{va:x} not mapped")
            return 1
        chunk = data[fo:fo + n]
        print(f"### data @0x{va:x} ({n} bytes)")
        words = struct.unpack_from("<" + "i" * (n // 4), chunk, 0)
        print("int32:", list(words))
        print("hex:", chunk.hex())
        return 0

    # --addr 0xVADDR : disassemble at a raw virtual address
    if "--addr" in args:
        i = args.index("--addr")
        for a in args[i + 1:]:
            va = int(a, 0)
            fo = vaddr_to_foff(segs, va)
            if fo is None:
                print(f"vaddr 0x{va:x} not mapped")
                continue
            code = data[fo:fo + budget]
            print(f"\n### @0x{va:x} ({budget} bytes)")
            for insn in md.disasm(code, va):
                print(f"0x{insn.address:x}:  {insn.mnemonic:8} {insn.op_str}")
        return 0

    func_starts = sorted({v for _, v in syms if vmaddr <= v < text_end})

    for target in args[1:]:
        matches = [(n, v) for n, v in syms if target in n and vmaddr <= v < text_end]
        if not matches:
            print(f"\n### {target}: not found")
            continue
        for name, val in matches:
            nxt = next((s for s in func_starts if s > val), val + budget)
            span = min(nxt - val, budget)
            fo = foff + (val - vmaddr)
            code = data[fo:fo + span]
            print(f"\n### {name}  @0x{val:x}  ({span} bytes)")
            for insn in md.disasm(code, val):
                print(f"0x{insn.address:x}:  {insn.mnemonic:8} {insn.op_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
