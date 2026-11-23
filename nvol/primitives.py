"""Primitive catalogue: signature + contract + executable reference semantics.

Every primitive used by a kernel lives here. The `ref` function IS the
specification: the interpreter executes it, and each ISA lowering in
`nvol.isa` is tested against it. Vectors are modelled as tuples of `n` unsigned
bytes (lane 0 = lowest address / least significant), regardless of ISA.

Contracts (`pre`) are checked by the interpreter; a backend may assume them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .ir import BOOL, PTR, U32, U64, U8, USIZE, VEC, Ty

MASK32 = 0xFFFF_FFFF
MASK64 = 0xFFFF_FFFF_FFFF_FFFF


@dataclass(frozen=True)
class PrimDef:
    name: str
    params: tuple
    ret: Ty
    doc: str
    # ref(ctx, *args); ctx gives .nbytes (vector width) and .mem (Memory)
    ref: Callable
    pre: Optional[str] = None


def _u64_lanes(v):
    return [int.from_bytes(bytes(v[i:i + 8]), "little") for i in range(0, len(v), 8)]


def _from_u64_lanes(lanes):
    out = b"".join((x & MASK64).to_bytes(8, "little") for x in lanes)
    return tuple(out)


def _load(ctx, p, aligned):
    if aligned and p % ctx.nbytes:
        raise AssertionError(f"load_a: address {p:#x} not {ctx.nbytes}-aligned")
    return tuple(ctx.mem.read(p, ctx.nbytes))


def _mask_or(ctx, a, b):
    assert all(x in (0, 0xFF) for x in a + b), "mask_or: lanes must be 0x00 or 0xFF"
    return tuple(x | y for x, y in zip(a, b))


def _bmask64(ctx, *vs):
    lanes = sum(vs, ())
    assert len(lanes) == 64, "bmask64: the vectors must cover 64 bytes"
    assert all(x in (0, 0xFF) for x in lanes), "bmask64: lanes must be 0x00 or 0xFF"
    return sum(1 << i for i, x in enumerate(lanes) if x)


def _any_nonzero(ctx, a):
    assert all(x in (0, 0xFF) for x in a), "any_nonzero: lanes must be 0x00 or 0xFF"
    return any(a)


def _first(ctx, m):
    assert m != 0, "first_offset: mask must be non-zero"
    return ((m & -m).bit_length() - 1) >> _lane_shift(ctx)


def _last(ctx, m):
    assert m != 0, "last_offset: mask must be non-zero"
    return (m.bit_length() - 1) >> _lane_shift(ctx)


def _lane_shift(ctx):
    return 2 if getattr(ctx, "isa", None) == "neon" else 0


def _movemask(ctx, a):
    if getattr(ctx, "isa", None) == "neon":
        # vshrn_n_u16(u16 lanes, 4) then & 0x8888..: lane 2j -> bit 7 of byte 2j,
        # lane 2j+1 -> bit 3 of byte 2j+1 (exactly what the instruction does)
        m = 0
        for i, x in enumerate(a):
            bit = (x >> 7) & 1 if i % 2 == 0 else (x >> 3) & 1
            m |= bit << (4 * i + 3)
        return m
    return sum(((x >> 7) & 1) << i for i, x in enumerate(a))


_DEFS = [
    PrimDef("splat", (U8,), VEC, "every lane = x", lambda c, x: (x,) * c.nbytes),
    PrimDef("vzero", (), VEC, "every lane = 0", lambda c: (0,) * c.nbytes),
    PrimDef("load_a", (PTR,), VEC, "load nbytes from an nbytes-aligned address",
            lambda c, p: _load(c, p, True), pre="addr % nbytes == 0, [p, p+nbytes) readable"),
    PrimDef("load_u", (PTR,), VEC, "load nbytes from any address",
            lambda c, p: _load(c, p, False), pre="[p, p+nbytes) readable"),
    PrimDef("cmpeq", (VEC, VEC), VEC, "lane = 0xFF if a==b else 0x00",
            lambda c, a, b: tuple(0xFF if x == y else 0 for x, y in zip(a, b))),
    PrimDef("vor", (VEC, VEC), VEC, "lane-wise OR",
            lambda c, a, b: tuple(x | y for x, y in zip(a, b))),
    PrimDef("mask_or", (VEC, VEC), VEC,
            "lane-wise OR of two compare masks; a backend may lower it as unsigned max "
            "(Odin: keeps LLVM from folding the OR into the i1 domain)",
            _mask_or, pre="every lane of a and b is 0x00 or 0xFF"),
    PrimDef("vand", (VEC, VEC), VEC, "lane-wise AND",
            lambda c, a, b: tuple(x & y for x, y in zip(a, b))),
    PrimDef("vxor", (VEC, VEC), VEC, "lane-wise XOR",
            lambda c, a, b: tuple(x ^ y for x, y in zip(a, b))),
    PrimDef("vandnot", (VEC, VEC), VEC, "lane-wise a AND NOT b (NEON bic; x86 pandn takes b first)",
            lambda c, a, b: tuple(x & ~y & 0xFF for x, y in zip(a, b))),
    PrimDef("vsub8", (VEC, VEC), VEC, "lane-wise wrapping a-b (u8)",
            lambda c, a, b: tuple((x - y) & 0xFF for x, y in zip(a, b))),
    PrimDef("vsubs8", (VEC, VEC), VEC, "lane-wise saturating a-b (u8): max(a - b, 0)",
            lambda c, a, b: tuple(max(x - y, 0) for x, y in zip(a, b))),
    PrimDef("vmin8", (VEC, VEC), VEC, "lane-wise unsigned min (u8)",
            lambda c, a, b: tuple(min(x, y) for x, y in zip(a, b))),
    PrimDef("movemask", (VEC,), U32,
            "x86: bit i = msb of lane i (u32). NEON: vshrn-based u64, lane i at bit 4i+3 "
            "-- exact for 0x00/0xFF lanes only (odd lanes read bit 3 of the byte)",
            lambda c, a: _movemask(c, a)),
    PrimDef("bmask64x2", (VEC, VEC), U64,
            "64-byte block of two 32-byte vectors: bit i = msb of byte i (a holds bytes 0-31)",
            _bmask64, pre="every lane is 0x00 or 0xFF (NEON reads one bit of each byte)"),
    PrimDef("bmask64x4", (VEC, VEC, VEC, VEC), U64,
            "64-byte block of four 16-byte vectors: bit i = msb of byte i (a holds bytes 0-15)",
            _bmask64, pre="every lane is 0x00 or 0xFF (NEON reads one bit of each byte)"),
    PrimDef("sad0", (VEC,), VEC,
            "for each 8-lane group: u64 lane = sum of its 8 bytes (psadbw vs 0)",
            lambda c, a: _from_u64_lanes([sum(a[i:i + 8]) for i in range(0, len(a), 8)])),
    PrimDef("vadd64", (VEC, VEC), VEC, "wrapping add of u64 lanes",
            lambda c, a, b: _from_u64_lanes([x + y for x, y in zip(_u64_lanes(a), _u64_lanes(b))])),
    PrimDef("hsum64", (VEC,), USIZE, "sum of all u64 lanes",
            lambda c, a: sum(_u64_lanes(a)) & MASK64),
    PrimDef("first_offset", (U32,), USIZE, "index of least significant set bit (tzcnt)",
            _first, pre="mask != 0"),
    PrimDef("last_offset", (U32,), USIZE, "index of most significant set bit (31-lzcnt)",
            _last, pre="mask != 0"),
    PrimDef("popcnt", (U32,), USIZE, "number of set bits",
            lambda c, m: bin(m & MASK64).count("1")),
    PrimDef("load16u", (PTR,), U32, "unaligned little-endian u16 load, zero-extended",
            lambda c, p: int.from_bytes(c.mem.read(p, 2), "little"), pre="[p, p+2) readable"),
    PrimDef("load32u", (PTR,), U32, "unaligned little-endian u32 load",
            lambda c, p: int.from_bytes(c.mem.read(p, 4), "little"), pre="[p, p+4) readable"),
    PrimDef("load64", (PTR,), U64, "unaligned little-endian u64 load",
            lambda c, p: int.from_bytes(c.mem.read(p, 8), "little"), pre="[p, p+8) readable"),
    PrimDef("cpu_avx2", (), BOOL, "runtime: CPU and OS support AVX2",
            lambda c: c.cpu.get("avx2", True)),
]

def _lanes128(v):
    return [list(v[i:i + 16]) for i in range(0, len(v), 16)]


def _shuffle(c, a, idx):
    out = []
    for la, li in zip(_lanes128(a), _lanes128(idx)):
        if getattr(c, "isa", None) == "neon":       # vqtbl1q_u8: out of range -> 0
            out += [la[x] if x < 16 else 0 for x in li]
        else:                                        # pshufb
            out += [0 if x & 0x80 else la[x & 0x0F] for x in li]
    return tuple(out)


def _shr16(c, a, n):
    out = []
    for i in range(0, len(a), 2):
        w = (a[i] | (a[i + 1] << 8)) >> n
        out += [w & 0xFF, w >> 8]
    return tuple(out)


def _shift_in(k):
    # full width: bytes of a move up by k; the top k bytes of b enter at the bottom
    return lambda c, a, b: tuple(list(b[len(b) - k:]) + list(a[:len(a) - k]))


def _half_shift_in(k):
    # per 128-bit lane (AVX2 palignr semantics)
    def f(c, a, b):
        out = []
        for la, lb in zip(_lanes128(a), _lanes128(b)):
            out += lb[16 - k:] + la[:16 - k]
        return tuple(out)
    return f


def _unpack(hi):
    def f(c, a, b):
        out = []
        for la, lb in zip(_lanes128(a), _lanes128(b)):
            for i in (range(8, 16) if hi else range(8)):
                out += [la[i], lb[i]]
        return tuple(out)
    return f


def _lane64(k):
    def f(c, a):
        assert 8 * k + 8 <= len(a), f"lane64_{k} out of range for {len(a)}-byte vector"
        return int.from_bytes(bytes(a[8 * k:8 * k + 8]), "little")
    return f


def _skip_set16(c, s, x):
    assert len(s) == 16 and len(x) == 16, "skip_set16: 16-byte vectors only"
    members = set(s[:s.index(0)] if 0 in s else s)
    for i, v in enumerate(x):
        if v == 0 or v not in members:
            return i
    return 16


def _first64(c, m):
    assert m != 0, "first_offset64: mask must be non-zero"
    return (m & -m).bit_length() - 1


_DEFS += [
    PrimDef("vshr4", (VEC,), VEC, "each u16 lane logically shifted right by 4 (psrlw 4)",
            lambda c, a: _shr16(c, a, 4)),
    PrimDef("vshuffle", (VEC, VEC), VEC,
            "per 128-bit lane: out[i] = idx[i] & 0x80 ? 0 : a[idx[i] & 15] (pshufb)", _shuffle),
    PrimDef("shin1", (VEC, VEC), VEC, "shift a up 1 byte, top byte of b enters (full width)", _shift_in(1)),
    PrimDef("shin2", (VEC, VEC), VEC, "shift a up 2 bytes, top 2 of b enter (full width)", _shift_in(2)),
    PrimDef("shin3", (VEC, VEC), VEC, "shift a up 3 bytes, top 3 of b enter (full width)", _shift_in(3)),
    PrimDef("hshin1", (VEC, VEC), VEC, "per 128-bit lane shin1 (palignr 15)", _half_shift_in(1)),
    PrimDef("hshin2", (VEC, VEC), VEC, "per 128-bit lane shin2 (palignr 14)", _half_shift_in(2)),
    PrimDef("hshin3", (VEC, VEC), VEC, "per 128-bit lane shin3 (palignr 13)", _half_shift_in(3)),
    PrimDef("is_zero", (VEC,), BOOL, "every lane is 0", lambda c, a: not any(a)),
    PrimDef("lane64_0", (VEC,), U64, "u64 lane 0", _lane64(0)),
    PrimDef("lane64_1", (VEC,), U64, "u64 lane 1", _lane64(1)),
    PrimDef("lane64_2", (VEC,), U64, "u64 lane 2 (256-bit only)", _lane64(2)),
    PrimDef("lane64_3", (VEC,), U64, "u64 lane 3 (256-bit only)", _lane64(3)),
    PrimDef("load_half_bcast", (PTR,), VEC, "load 16 bytes into both 128-bit lanes (256-bit only)",
            lambda c, p: tuple(c.mem.read(p, 16)) * 2, pre="[p, p+16) readable"),
    PrimDef("swap_halves", (VEC,), VEC, "swap the two 128-bit lanes (256-bit only)",
            lambda c, a: tuple(list(a[16:]) + list(a[:16]))),
    PrimDef("unpacklo8", (VEC, VEC), VEC, "per 128-bit lane interleave of the low 8 bytes (a0 b0 a1 b1 ..)",
            _unpack(False)),
    PrimDef("unpackhi8", (VEC, VEC), VEC, "per 128-bit lane interleave of the high 8 bytes", _unpack(True)),
    PrimDef("first_offset64", (U64,), USIZE, "index of least significant set bit of a u64", _first64,
            pre="mask != 0"),
    PrimDef("last_offset64", (U64,), USIZE, "index of most significant set bit of a u64 (63-lzcnt)",
            lambda c, m: m.bit_length() - 1, pre="mask != 0"),
    PrimDef("popcnt64", (U64,), USIZE, "number of set bits of a u64",
            lambda c, m: bin(m & MASK64).count("1")),
    PrimDef("cpu_avx2_pclmul", (), BOOL,
            "runtime: CPU and OS support AVX2, and the CPU has PCLMULQDQ (every AVX2 CPU has it, "
            "but a hypervisor can hide it; one check keeps a single dispatch condition)",
            lambda c: c.cpu.get("avx2", True) and c.cpu.get("pclmul", True)),
    PrimDef("cpu_ssse3", (), BOOL, "runtime: CPU supports SSSE3", lambda c: c.cpu.get("ssse3", True)),
    PrimDef("cpu_sse42", (), BOOL, "runtime: CPU supports SSE4.2", lambda c: c.cpu.get("sse42", True)),
    PrimDef("skip_set16", (VEC, VEC), U32,
            "index of the first lane of x outside the set, 16 if none (pcmpistri, equal-any, "
            "negative polarity, implicit lengths): the set is the lanes of s before its first 0; "
            "a 0 lane of x counts as outside (16-byte vectors only)",
            _skip_set16),
    PrimDef("any_nonzero", (VEC,), BOOL, "some lane is non-zero (memchr movemask_will_have_non_zero)",
            _any_nonzero, pre="every lane is 0x00 or 0xFF (x86 lowers it to movemask != 0)"),
    PrimDef("vnib_hi", (VEC,), VEC, "per byte: high nibble (b >> 4)", lambda c, a: tuple(x >> 4 for x in a)),
    PrimDef("msb_lanes", (VEC,), VEC, "per byte: 0xFF if the msb is set, else 0",
            lambda c, a: tuple(0xFF if x & 0x80 else 0 for x in a)),
]

PRIMS = {p.name: p for p in _DEFS}
