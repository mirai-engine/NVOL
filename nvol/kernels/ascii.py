"""first_non_ascii: index of the first byte >= 0x80.

Parity target: bstr 1.13 `ascii.rs::first_non_ascii_byte` (used by ripgrep's
UTF-8 handling through bstr). movemask of the raw bytes *is* the predicate, so
no compare is needed. Returns NONE when every byte is ASCII (bstr returns len).
"""
from __future__ import annotations

from ..ir import NULLP, PTR, Builder, Function, Module, prim
from ..isa import ISA, movemask
from .dispatch import add_public
from .memchr import _or_tree


def _hdr(b):
    """Raw signature (start, end); also binds len = end - start."""
    start, end = b.param("start", PTR), b.param("end", PTR)
    return start, end, b.let("len", end - start)


def scalar() -> Function:
    b = Builder("nvol_first_non_ascii_scalar", PTR, doc="byte-at-a-time fallback")
    start, end, _ = _hdr(b)
    p = b.var("p", start)
    with b.while_(p < end):
        with b.if_(p.load8() >= 0x80):
            b.ret(p)
        b.set(p, p + 1)
    b.ret(NULLP)
    return b.build()


def vector(isa: ISA) -> Function:
    V, LOOP = isa.bytes, 4 * isa.bytes
    b = Builder(f"nvol_first_non_ascii_{isa.name}", PTR, isa=isa.name,
                doc=f"requires len >= {V}")
    start, end, length = _hdr(b)

    def hi(v):
        # x86 movemask reads the msb of raw bytes directly; NEON's vshrn movemask
        # is only exact on 0x00/0xFF lanes, so normalize first
        return prim("msb_lanes", v) if isa.lane_bits == 4 else v

    def chunk(at):
        m = b.let("mask", movemask(isa, hi(prim("load_u", at))))
        with b.if_(m.ne(0)):
            b.ret(at + prim("first_offset", m))

    chunk(start)
    cur = b.var("cur", start + (V - (start.addr() & isa.align_mask)))
    with b.if_(length >= LOOP):
        with b.while_(cur <= end - LOOP):
            ld = [b.let("ld", prim("load_a", cur + k * V if k else cur)) for k in range(4)]
            anyv = _or_tree(ld, "vor")
            with b.if_(movemask(isa, hi(anyv)).ne(0)):
                for k in range(4):
                    m = b.let("mask", movemask(isa, hi(ld[k])))
                    at = cur + k * V if k else cur
                    with b.if_(m.ne(0)):
                        b.ret(at + prim("first_offset", m))
            b.set(cur, cur + LOOP)
    with b.while_(cur <= end - V):
        chunk(cur)
        b.set(cur, cur + V)
    with b.if_(cur < end):
        b.set(cur, end - V)
        chunk(cur)
    b.ret(NULLP)
    return b.build()


def build(isas: list, mod: Module) -> None:
    mod.functions.append(scalar())
    impls = {"scalar": "nvol_first_non_ascii_scalar"}
    for isa in isas:
        f = vector(isa)
        mod.functions.append(f)
        impls[isa.name] = f.name
    add_public(mod, "nvol_first_non_ascii", [], impls,
               "Index of the first byte >= 0x80 in hay[0..len), or SIZE_MAX/usize::MAX.", kind="find")
    mod.api.append(("ascii", "nvol_first_non_ascii", 0))
