"""Byte counting (ripgrep: line counting via `memchr_iter(b'\\n', ..).count()`).

Two vector strategies are generated and both are verified:

  * `parity`: memchr 2.8.3 `count_raw` -- per vector, popcnt(movemask(cmpeq)).
  * `sad`   : NVOL's improvement -- accumulate `0 - cmpeq` (= +1 per match)
              in u8 lanes for up to 63 unrolled iterations, then fold with
              psadbw into u64 lanes. One horizontal reduction per ~8 KiB
              instead of one movemask+popcnt per vector. The tail is a single
              overlapping load whose mask is shifted to drop counted bytes.

The public `nvol_count` uses `sad`; `nvol_count_parity` is kept for benchmarks.
"""
from __future__ import annotations

from ..ir import PTR, U8, U32, USIZE, lift, Builder, Call, Function, Module, land, prim
from ..isa import ISA, mask_ty, movemask
from .dispatch import add_public


def _hdr(b: Builder):
    """Raw signature (n1, start, end); also binds len = end - start."""
    n1, start, end = b.param("n1", U8), b.param("start", PTR), b.param("end", PTR)
    return n1, start, end, b.let("len", end - start)


def count_scalar() -> Function:
    b = Builder("nvol_count_scalar", USIZE, doc="byte-at-a-time fallback")
    n1, start, end, length = _hdr(b)
    c = b.var("count", 0, USIZE)
    i = b.var("i", 0, USIZE)
    with b.while_(i < length):
        with b.if_((start + i).load8().eq(n1)):
            b.set(c, c + 1)
        b.set(i, i + 1)
    b.ret(c)
    return b.build()


def count_parity(isa: ISA) -> Function:
    V, LOOP = isa.bytes, 4 * isa.bytes
    b = Builder(f"nvol_count_parity_{isa.name}", USIZE, isa=isa.name,
                doc=f"memchr-2.8.3-style count; requires len >= {V}")
    n1, start, end, length = _hdr(b)
    v1 = b.let("v1", prim("splat", n1))
    cur = b.var("cur", start + (V - (start.addr() & isa.align_mask)))
    count = b.var("count", 0, USIZE)
    p = b.var("p", start)
    with b.while_(p < cur):
        with b.if_(p.load8().eq(n1)):
            b.set(count, count + 1)
        b.set(p, p + 1)
    with b.if_(length >= LOOP):
        with b.while_(cur <= end - LOOP):
            for k in range(4):
                ld = prim("load_a", cur + k * V if k else cur)
                b.set(count, count + prim("popcnt", movemask(isa, prim("cmpeq", ld, v1))))
            b.set(cur, cur + LOOP)
    with b.while_(cur <= end - V):
        b.set(count, count + prim("popcnt", movemask(isa, prim("cmpeq", prim("load_u", cur), v1))))
        b.set(cur, cur + V)
    with b.while_(cur < end):
        with b.if_(cur.load8().eq(n1)):
            b.set(count, count + 1)
        b.set(cur, cur + 1)
    b.ret(count)
    return b.build()


def count_sad(isa: ISA) -> Function:
    V = isa.bytes
    UNROLL = 4
    LOOP = UNROLL * V
    MAX_ITERS = 255 // UNROLL          # u8 lanes may grow by UNROLL per iter
    b = Builder(f"nvol_count_sad_{isa.name}", USIZE, isa=isa.name,
                doc=f"SAD-accumulated count; requires len >= {V}")
    n1, start, end, length = _hdr(b)
    v1 = b.let("v1", prim("splat", n1))
    cur = b.var("cur", start)
    count = b.var("count", 0, USIZE)

    def rest():
        """The vector loop and the overlapping tail, from cur."""
        with b.while_(cur <= end - V):
            b.set(count, count + prim("popcnt", movemask(isa, prim("cmpeq", prim("load_u", cur), v1))))
            b.set(cur, cur + V)
        with b.if_(cur < end):
            # overlapping final load; drop the lanes that were already counted
            rem = b.let("rem", end - cur)
            m = b.let("mask", movemask(isa, prim("cmpeq", prim("load_u", end - V), v1)))
            lanes = lift(V, USIZE) - rem
            if isa.lane_bits == 4:
                lanes = lanes << 2
            shift = b.let("shift", lanes.cast(mask_ty(isa)))
            b.set(count, count + prim("popcnt", m >> shift))

    # below one SAD block: no accumulator to clear and no horizontal sum, just the vector loop
    with b.if_(length < LOOP):
        rest()
        b.ret(count)
    acc64 = b.var("acc64", prim("vzero"))
    with b.while_((end - cur) >= LOOP):
        iters = b.var("iters", _div(end - cur, LOOP))
        with b.if_(iters > MAX_ITERS):
            b.set(iters, MAX_ITERS)
        acc8 = b.var("acc8", prim("vzero"))
        with b.while_(iters > 0):
            for k in range(UNROLL):
                ld = prim("load_u", cur + k * V if k else cur)
                b.set(acc8, prim("vsub8", acc8, prim("cmpeq", ld, v1)))
            b.set(cur, cur + LOOP)
            b.set(iters, iters - 1)
        b.set(acc64, prim("vadd64", acc64, prim("sad0", acc8)))
    rest()
    b.ret(count + prim("hsum64", acc64))
    return b.build()


def _div(x, k: int):
    """x / k for power-of-two k, as a shift (keeps the IR division-free)."""
    assert k & (k - 1) == 0
    return x >> (k.bit_length() - 1)


def build(isas: list, mod: Module) -> None:
    mod.functions.append(count_scalar())
    for strat, mk in (("parity", count_parity), ("sad", count_sad)):
        impls = {"scalar": "nvol_count_scalar"}
        for isa in isas:
            f = mk(isa)
            mod.functions.append(f)
            impls[isa.name] = f.name
        pub = "nvol_count" if strat == "sad" else "nvol_count_parity"
        add_public(mod, pub, [("n1", U8)], impls,
                   f"Number of bytes in hay[0..len) equal to n1 ({strat} strategy).", kind="count")
        mod.api.append(("count", pub, 1))
