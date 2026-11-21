"""memchr family: find / rfind of 1..3 byte needles, and byte counting.

Algorithm parity target: BurntSushi/memchr 2.8.3 `arch/generic/memchr.rs`
(the routines ripgrep reaches through `memchr`, `memrchr`, `memchr_iter().count()`
and, via regex-automata, `memchr2/3` prefilters):

  * one unaligned chunk at the edge, then an aligned main loop unrolled
    4x (1 needle) or 2x (2-3 needles) whose vectors are OR-ed so only one
    movemask is taken per iteration,
  * an overlapping unaligned load for the tail instead of a byte loop.

Dispatch mirrors memchr's x86_64 front-end: len < 16 -> scalar,
len < 32 -> SSE2, otherwise AVX2 when the CPU has it.
"""
from __future__ import annotations

from ..ir import (NULLP, PTR, U8, USIZE, Builder, Function, Module, lor, prim)
from ..isa import ISA, movemask
from .dispatch import add_public

NAMES = {1: "memchr", 2: "memchr2", 3: "memchr3"}


def _fname(n: int, reverse: bool, isa: "ISA | None") -> str:
    base = NAMES[n].replace("memchr", "memrchr") if reverse else NAMES[n]
    return f"nvol_{base}_{isa.name if isa else 'scalar'}"


def _header(b: Builder, n: int):
    """Raw kernel signature, like memchr's `find_raw`: (needles.., start, end)."""
    needles = [b.param(f"n{i + 1}", U8) for i in range(n)]
    start = b.param("start", PTR)
    end = b.param("end", PTR)
    return needles, start, end




def _or_tree(xs, op="mask_or"):
    """Balanced OR reduction (short dependency chains, like memchr). `op` is
    "mask_or" for compare masks, "vor" for arbitrary bytes."""
    while len(xs) > 1:
        xs = [prim(op, xs[i], xs[i + 1]) if i + 1 < len(xs) else xs[i]
              for i in range(0, len(xs), 2)]
    return xs[0]


def find_vector(isa: ISA, n: int, reverse: bool) -> Function:
    V = isa.bytes
    unroll = 4 if n == 1 else 2
    LOOP = unroll * V
    b = Builder(_fname(n, reverse, isa), PTR, isa=isa.name,
                doc=f"pointer to the {'last' if reverse else 'first'} byte in [start, end) equal to any of "
                    f"{n} needle(s), or NULL; requires end - start >= {V}")
    needles, start, end = _header(b, n)
    length = b.let("len", end - start)
    vs = [b.let(f"v{i + 1}", prim("splat", nd)) for i, nd in enumerate(needles)]
    topos = "last_offset" if reverse else "first_offset"

    def eqs(chunk):
        e = prim("cmpeq", chunk, vs[0])
        for v in vs[1:]:
            e = prim("mask_or", e, prim("cmpeq", chunk, v))
        return e

    def search_chunk(at):
        # `at` must be side-effect free; it is printed twice.
        chunk = b.let("chunk", prim("load_u", at))
        mask = b.let("mask", movemask(isa, eqs(chunk)))
        with b.if_(mask.ne(0)):
            b.ret(at + prim(topos, mask))

    def unrolled_body(cur):
        loads = [b.let("ld", prim("load_a", cur + k * V if k else cur)) for k in range(unroll)]
        eq = [b.let("eq", eqs(x)) for x in loads]
        anyv = b.let("any", _or_tree(eq))
        with b.if_(prim("any_nonzero", anyv)):
            order = range(unroll - 1, -1, -1) if reverse else range(unroll)
            for j, k in enumerate(order):
                m = b.let("mask", movemask(isa, eq[k]))
                at = (cur + k * V) if k else cur
                if j == unroll - 1:   # `any` was non-zero, so this one must be
                    b.ret(at + prim(topos, m))
                else:
                    with b.if_(m.ne(0)):
                        b.ret(at + prim(topos, m))

    if not reverse:
        search_chunk(start)
        cur = b.var("cur", start + (V - (start.addr() & isa.align_mask)))
        with b.if_(length >= LOOP):
            with b.while_(cur <= end - LOOP):
                unrolled_body(cur)
                b.set(cur, cur + LOOP)
        with b.while_(cur <= end - V):
            search_chunk(cur)
            b.set(cur, cur + V)
        with b.if_(cur < end):
            b.set(cur, cur - (V - (end - cur)))
            search_chunk(cur)
        b.ret(NULLP)
    else:
        search_chunk(end - V)
        cur = b.var("cur", end - (end.addr() & isa.align_mask))
        with b.if_(length >= LOOP):
            with b.while_(cur >= start + LOOP):
                b.set(cur, cur - LOOP)
                unrolled_body(cur)
        with b.while_(cur >= start + V):
            b.set(cur, cur - V)
            search_chunk(cur)
        with b.if_(cur > start):
            search_chunk(start)
        b.ret(NULLP)
    return b.build()


def find_scalar(n: int, reverse: bool) -> Function:
    b = Builder(_fname(n, reverse, None), PTR, doc="byte-at-a-time fallback")
    needles, start, end = _header(b, n)

    def hit(x):
        c = x.eq(needles[0])
        for nd in needles[1:]:
            c = lor(c, x.eq(nd))
        return c

    if not reverse:
        p = b.var("p", start)
        with b.while_(p < end):
            with b.if_(hit(p.load8())):
                b.ret(p)
            b.set(p, p + 1)
    else:
        p = b.var("p", end)
        with b.while_(p > start):
            b.set(p, p - 1)
            with b.if_(hit(p.load8())):
                b.ret(p)
    b.ret(NULLP)
    return b.build()


def build(isas: list, mod: Module) -> None:
    for n in (1, 2, 3):
        for reverse in (False, True):
            impls = {"scalar": _fname(n, reverse, None)}
            mod.functions.append(find_scalar(n, reverse))
            for isa in isas:
                mod.functions.append(find_vector(isa, n, reverse))
                impls[isa.name] = _fname(n, reverse, isa)
            pub = (NAMES[n].replace("memchr", "memrchr") if reverse else NAMES[n])
            needles = [(f"n{i + 1}", U8) for i in range(n)]
            add_public(mod, f"nvol_{pub}", needles, impls,
                       f"{'Last' if reverse else 'First'} index in hay[0..len) equal to any of "
                       f"{n} needle byte(s), or SIZE_MAX/usize::MAX if none.",
                       kind="find")
            mod.api.append(("find", f"nvol_{pub}", n))
