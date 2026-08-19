#!/usr/bin/env python3
"""Annotated call-sequence tracer for Mach-O x86_64 binaries.

For a given function, disassembles it and prints every `call` in order with the
demangled target name - the fastest way to recover an orchestration sequence
(e.g. which Exec* commands CCanoDR::StartScan issues, in what order).

Usage:
    tools/trace_calls.py <binary> <symbol_substring> [--full] [--bytes N]
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_64

sys.path.insert(0, str(Path(__file__).parent))
from disasm_macho import parse  # noqa: E402


def demangle_all(names: list[str]) -> dict[str, str]:
    out = subprocess.run(["c++filt"], input="\n".join(names),
                         capture_output=True, text=True).stdout.splitlines()
    return dict(zip(names, out))


def main() -> int:
    args = sys.argv[1:]
    full = "--full" in args
    if full:
        args.remove("--full")
    budget = 8000
    if "--bytes" in args:
        i = args.index("--bytes")
        budget = int(args[i + 1])
        args = args[:i] + args[i + 2:]

    path = Path(args[0])
    data, text, syms, segs = parse(path)
    vmaddr, foff, size = text
    text_end = vmaddr + size

    sym_at: dict[int, str] = {}
    for n, v in syms:
        sym_at.setdefault(v, n)
    func_starts = sorted({v for _, v in syms if vmaddr <= v < text_end})

    dem = demangle_all(sorted(set(sym_at.values())))
    md = Cs(CS_ARCH_X86, CS_MODE_64)

    for target in args[1:]:
        matches = [(n, v) for n, v in syms
                   if target in n and vmaddr <= v < text_end]
        if not matches:
            print(f"### {target}: not found")
            continue
        for name, val in sorted(set(matches), key=lambda t: t[1]):
            nxt = next((s for s in func_starts if s > val), val + budget)
            span = min(nxt - val, budget)
            code = data[foff + (val - vmaddr): foff + (val - vmaddr) + span]
            print(f"\n### {dem.get(name, name)}  @0x{val:x}  ({span}B)")
            for insn in md.disasm(code, val):
                is_call = insn.mnemonic == "call"
                if full or is_call or insn.mnemonic in ("jmp",):
                    label = ""
                    if is_call or insn.mnemonic == "jmp":
                        try:
                            tgt = int(insn.op_str, 16)
                            s = sym_at.get(tgt)
                            if s:
                                label = "  -> " + dem.get(s, s)
                        except ValueError:
                            pass
                    print(f"0x{insn.address:x}:  {insn.mnemonic:6} "
                          f"{insn.op_str}{label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
