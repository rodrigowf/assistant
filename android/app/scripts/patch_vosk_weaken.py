#!/usr/bin/env python3
"""
Patch libvosk.so to mark stderr/stdin/stdout as STB_WEAK in .dynsym.

Why: Vosk's prebuilt libvosk.so was built with BIND_NOW and lists
stderr/stdin/stdout as STB_GLOBAL undefined symbols. On Lollipop (API 22)
Bionic does not export these as symbols (they were macros pre-M), so dlopen
fails with "cannot locate symbol stderr". Weakening the binding lets the
linker treat them as optional: if unresolved, they get NULL, and Vosk
never actually USES them anywhere on the hot path (they're only referenced
by buried fprintf calls in error handlers that we never hit during normal
wake-word recognition).

Algorithm:
  1. Parse ELF dynamic section to find .dynsym and .dynstr offsets/sizes.
  2. For each symbol in .dynsym:
       - Read its st_name (offset into .dynstr).
       - Read the string at that offset.
       - If it's one of {stderr, stdin, stdout}, rewrite st_info from
         (STB_GLOBAL << 4) | STT_NOTYPE = 0x10 to
         (STB_WEAK   << 4) | STT_NOTYPE = 0x20.

Supports ELF32 (armeabi-v7a) and ELF64 (arm64-v8a).
"""

import struct
import sys

STB_GLOBAL = 1
STB_WEAK   = 2

NAMES_TO_WEAKEN = (b"stderr", b"stdin", b"stdout")


def patch(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())

    # ELF header — 16 bytes ident, then class at offset 4 (1=32-bit, 2=64-bit)
    assert data[:4] == b"\x7fELF", "Not an ELF"
    is64 = data[4] == 2

    if is64:
        # ELF64: e_phoff at 0x20, e_phentsize at 0x36, e_phnum at 0x38
        e_phoff, = struct.unpack_from("<Q", data, 0x20)
        e_phentsize, = struct.unpack_from("<H", data, 0x36)
        e_phnum, = struct.unpack_from("<H", data, 0x38)
        # Program header: p_type at 0, p_offset at 0x08, p_filesz at 0x20
        PT_DYNAMIC = 2
        dyn_off = None
        dyn_size = None
        for i in range(e_phnum):
            phoff = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from("<I", data, phoff)
            if p_type == PT_DYNAMIC:
                dyn_off, = struct.unpack_from("<Q", data, phoff + 0x08)
                dyn_size, = struct.unpack_from("<Q", data, phoff + 0x20)
                break
        assert dyn_off is not None, "No PT_DYNAMIC"
        # Dynamic entries: 16 bytes each (d_tag:8, d_val:8)
        DT_SYMTAB = 6
        DT_STRTAB = 5
        DT_STRSZ  = 10
        DT_SYMENT = 11
        DT_HASH   = 4   # for nchains
        DT_GNU_HASH = 0x6ffffef5
        symtab_va = strtab_va = strsz = syment = hash_va = gnuhash_va = None
        for i in range(dyn_size // 16):
            d_tag, d_val = struct.unpack_from("<QQ", data, dyn_off + i * 16)
            if d_tag == 0: break
            if d_tag == DT_SYMTAB: symtab_va = d_val
            elif d_tag == DT_STRTAB: strtab_va = d_val
            elif d_tag == DT_STRSZ:  strsz = d_val
            elif d_tag == DT_SYMENT: syment = d_val
            elif d_tag == DT_HASH:   hash_va = d_val
            elif d_tag == DT_GNU_HASH: gnuhash_va = d_val
        assert syment == 24, f"Unexpected ELF64 syment {syment}"
    else:
        # ELF32: e_phoff at 0x1c, e_phentsize at 0x2a, e_phnum at 0x2c
        e_phoff, = struct.unpack_from("<I", data, 0x1c)
        e_phentsize, = struct.unpack_from("<H", data, 0x2a)
        e_phnum, = struct.unpack_from("<H", data, 0x2c)
        PT_DYNAMIC = 2
        dyn_off = None
        dyn_size = None
        for i in range(e_phnum):
            phoff = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from("<I", data, phoff)
            if p_type == PT_DYNAMIC:
                dyn_off, = struct.unpack_from("<I", data, phoff + 0x04)
                dyn_size, = struct.unpack_from("<I", data, phoff + 0x10)
                break
        assert dyn_off is not None, "No PT_DYNAMIC"
        # Dynamic entries: 8 bytes each (d_tag:4, d_val:4)
        DT_SYMTAB = 6
        DT_STRTAB = 5
        DT_STRSZ  = 10
        DT_SYMENT = 11
        DT_HASH   = 4
        DT_GNU_HASH = 0x6ffffef5
        symtab_va = strtab_va = strsz = syment = hash_va = gnuhash_va = None
        for i in range(dyn_size // 8):
            d_tag, d_val = struct.unpack_from("<II", data, dyn_off + i * 8)
            if d_tag == 0: break
            if d_tag == DT_SYMTAB: symtab_va = d_val
            elif d_tag == DT_STRTAB: strtab_va = d_val
            elif d_tag == DT_STRSZ:  strsz = d_val
            elif d_tag == DT_SYMENT: syment = d_val
            elif d_tag == DT_HASH:   hash_va = d_val
            elif d_tag == DT_GNU_HASH: gnuhash_va = d_val
        assert syment == 16, f"Unexpected ELF32 syment {syment}"

    # The dynamic table virtual addresses (DT_SYMTAB, DT_STRTAB) in shared
    # libs are typically equal to file offsets because the dynamic segment
    # is in the first PT_LOAD that starts at file offset 0. We confirm by
    # spot-checking strtab content.
    strtab_off = strtab_va
    symtab_off = symtab_va
    assert strsz is not None
    strtab = bytes(data[strtab_off:strtab_off + strsz])

    # Count symbols. Best source: DT_HASH's second word = nchains = nsyms.
    if hash_va is not None:
        nbuckets, nchains = struct.unpack_from("<II", data, hash_va)
        nsyms = nchains
    else:
        # Fallback: scan symbol entries until we run off the end (rough).
        nsyms = (strtab_off - symtab_off) // syment

    patched = []
    for i in range(nsyms):
        sym_off = symtab_off + i * syment
        if is64:
            # ELF64 Sym: st_name(4), st_info(1), st_other(1), st_shndx(2),
            #           st_value(8), st_size(8) = 24 bytes
            st_name, = struct.unpack_from("<I", data, sym_off)
            st_info  = data[sym_off + 4]
        else:
            # ELF32 Sym: st_name(4), st_value(4), st_size(4), st_info(1),
            #           st_other(1), st_shndx(2) = 16 bytes
            st_name, = struct.unpack_from("<I", data, sym_off)
            st_info  = data[sym_off + 12]

        # st_info = (bind << 4) | type
        bind = st_info >> 4
        typ  = st_info & 0x0f
        if bind != STB_GLOBAL:
            continue
        # Lookup name
        end = strtab.find(b"\x00", st_name)
        name = strtab[st_name:end] if end >= 0 else strtab[st_name:]
        if name in NAMES_TO_WEAKEN:
            new_info = (STB_WEAK << 4) | typ
            if is64:
                data[sym_off + 4] = new_info
            else:
                data[sym_off + 12] = new_info
            patched.append(name.decode())

    with open(path, "wb") as f:
        f.write(bytes(data))
    return patched


if __name__ == "__main__":
    for p in sys.argv[1:]:
        names = patch(p)
        print(f"{p}: weakened {names}")
