"""memchr family: find / rfind of 1..3 byte needles, and byte counting.

Algorithm parity target: BurntSushi/memchr 2.8.3 `arch/generic/memchr.rs`
(the routines ripgrep reaches through `memchr`, `memrchr`, `memchr_iter().count()`
and, via regex-automata, `memchr2/3` prefilters):

  * one unaligned chunk at the edge, then an aligned main loop unrolled
    4x (1 needle) or 2x (2-3 needles) whose vectors are OR-ed so only one
    movemask is taken per iteration,
  * an overlapping unaligned load for the tail instead of a byte loop.

Reverse search on x86, in NVOL's own assembly: from REV_LONG[V] iterations of the unrolled
loop, the loop first runs in the exit-first shape (`While.exit_first`: Clang's shape, which
predicts the exit of a long loop), then in the usual rotated one, which costs less for a few
iterations (PENDING item 25). The language backends ignore it.

Dispatch mirrors memchr's x86_64 front-end: len < 16 -> scalar,
len < 32 -> SSE2, otherwise AVX2 when the CPU has it.
"""
from __future__ import annotations

from ..ir import (NULLP, PTR, U8, U32, U64, USIZE, Builder, Function, Module, lor, prim)
from ..isa import ISA, movemask
from .dispatch import add_public

NAMES = {1: "memchr", 2: "memchr2", 3: "memchr3"}
# unrolled iterations from which the reverse loop runs exit-first (asm, x86), by vector bytes
REV_LONG = {32: 32, 16: 16}


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
            long = (length >= REV_LONG[V] * LOOP) if V in REV_LONG and isa.name != "neon" else None
            with b.while_(cur >= start + LOOP, exit_first=long):
                b.set(cur, cur - LOOP)
                unrolled_body(cur)
        with b.while_(cur >= start + V):
            b.set(cur, cur - V)
            search_chunk(cur)
        with b.if_(cur > start):
            search_chunk(start)
        b.ret(NULLP)
    return b.build()


def _swar_marks(b: Builder, w, splats, ty, exact: bool):
    """The bytes of the word `w` equal to a needle, as 0x80 in each such byte, OR-ed over the
    needles. `exact` marks only the equal bytes (memchr's `highest_zero_byte` shape, for the
    last match); otherwise the lowest set bit is the first equal byte and the bits above it may
    be borrow artefacts (`(x - 0x01..) & !x & 0x80..`, for the first match: a false bit of one
    needle only sits above that needle's own first match)."""
    bits = 32 if ty is U32 else 64
    lo, hi, ones = (0x0101010101010101, 0x8080808080808080, (1 << bits) - 1)
    lo, hi = lo & ones, hi & ones
    l7 = ones // 0xFF * 0x7F
    # the masks shared by every needle are applied once: OR of the per-needle (x - 0x01..) & !x,
    # then & 0x80..; for the exact marks, OR of !(y | l7) is !(AND of y | l7)
    m = None
    for s in splats:
        x = w ^ s
        t = (((x & l7).wadd(l7)) | x) if exact else (x.wsub(lo) & (x ^ ones))
        m = t if m is None else ((m & t) if exact else (m | t))
    m = ((m | l7) ^ ones) if exact else (m & hi)
    return b.let("m", m)


def find_scalar(n: int, reverse: bool) -> Function:
    """memchr's word-at-a-time fallback (`arch/all/memchr.rs`), with the short sizes in a few
    loads instead of a loop: 1-3 bytes one by one (1-4 for three needles), 4-7 in two
    overlapping u32 loads, 8-16 in two overlapping u64 loads, longer haystacks (the path
    without SIMD) one aligned u64 per iteration and an overlapping last load. This is the kernel the dispatch takes under 16
    bytes and on a target without SIMD (PENDING item 28)."""
    b = Builder(_fname(n, reverse, None), PTR,
                doc="word-at-a-time (SWAR) search; the dispatch's path under 16 bytes")
    needles, start, end = _header(b, n)
    length = b.let("len", end - start)

    def hit(x):
        c = x.eq(needles[0])
        for nd in needles[1:]:
            c = lor(c, x.eq(nd))
        return c

    short = 5 if n == 3 else 4      # three needles: 4 bytes are cheaper one by one than in words
    with b.if_(length < short):
        if n == 2:
            # two needles: straight-line checks. In each ISA entry, where this kernel is
            # inlined, the byte loop landed across a 64-byte line (+2.6 cycles at 2 bytes);
            # for one and three needles the straight-line block overflows a uop-cache window
            # instead (memchr 2 B: 1.5 cycles of DSB-to-MITE switch per call), so they keep it
            for k in range(short - 1):
                with b.if_(length.eq(k)):
                    b.ret(NULLP)
                at = (end - (k + 1)) if reverse else (start + k if k else start)
                with b.if_(hit(at.load8())):
                    b.ret(at)
        elif not reverse:
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

    def splats(ty, k):
        return [b.let(f"s{k}{i + 1}", nd.cast(ty).wmul(0x0101010101010101 & ((1 << (32 if ty is U32 else 64)) - 1)))
                for i, nd in enumerate(needles)]

    def check(at, ty, ss):
        w = b.let("w", prim("load32u" if ty is U32 else "load64", at))
        m = _swar_marks(b, w, ss, ty, exact=reverse)
        with b.if_(m.ne(0)):
            name = "last_offset" if reverse else "first_offset"
            off = prim(name if ty is U32 else name + "64", m) >> 3
            b.ret(at + off)

    with b.if_(length < 8):
        s32 = splats(U32, "h")
        for at in ((end - 4, start) if reverse else (start, end - 4)):
            check(at, U32, s32)
        b.ret(NULLP)
    s64 = splats(U64, "w")
    check(end - 8 if reverse else start, U64, s64)
    with b.if_(length <= 16):
        check(start if reverse else end - 8, U64, s64)
        b.ret(NULLP)
    if not reverse:
        q = b.var("q", start + (8 - (start.addr() & 7)))
        with b.while_(q <= end - 8):
            check(q, U64, s64)
            b.set(q, q + 8)
        with b.if_(q < end):
            check(end - 8, U64, s64)
    else:
        q = b.var("q", end - (end.addr() & 7))
        with b.while_(q >= start + 8):
            b.set(q, q - 8)
            check(q, U64, s64)
        with b.if_(q > start):
            check(start, U64, s64)
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
