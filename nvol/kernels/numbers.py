"""simdjson's eight-digit number parsing (PENDING 37): what its stage 2 runs on the digits of
a long integer or fraction, once is_made_of_eight_digits_fast says the next 8 bytes are
ASCII digits.

nvol_is_eight_digits(p) is 1 when p[0..8) are all '0'..'9', else 0 (simdjson's SWAR test):

    ((v & 0xF0..F0) | (((v + 0x06..06) & 0xF0..F0) >> 4)) == 0x33..33

nvol_parse_eight_digits(p) is the decimal value of p[0..8), which must be eight digits. Two
implementations, both from simdjson:
- SSE4.1 (haswell and westmere, on 128 bits): the digits minus '0', then pmaddubsw by
  (10, 1)*, pmaddwd by (100, 1)*, packusdw, pmaddwd by (10000, 1)*; lane 0 is the value.
  simdjson loads 16 bytes; here a movq loads the 8 the value needs, so the function reads
  no byte past p + 8;
- SWAR (fallback and arm64): (v & 0x0F..0F) * 2561 >> 8, then the pairs * 6553601 >> 16,
  then the quads * 42949672960001 >> 32.

The ISA functions are `NVOL_INLINE` in the C++ header, so a caller (simdjson's stage 2) can
call nvol_parse_eight_digits_sse42 or _scalar directly and have it inlined; the dispatched
entry is for C callers."""
from __future__ import annotations
from ..ir import PTR, U8, U32, U64, Builder, Function, Module, lift, prim
from ..isa import ISAS

DOC_PARSE = ("the decimal value of p[0..8), which must be eight ASCII digits "
             "(nvol_is_eight_digits); reads exactly 8 bytes")
DOC_IS = "1 when p[0..8) are eight ASCII digits '0'..'9', else 0; reads exactly 8 bytes"

F0 = 0xF0F0F0F0F0F0F0F0


def is_eight_digits() -> Function:
    b = Builder("nvol_is_eight_digits", U32, public=True, doc=DOC_IS)
    p = b.param("p", PTR)
    v = b.let("v", prim("load64", p))
    b.ret((((v & F0) | ((v.wadd(0x0606060606060606) & F0) >> 4)).eq(0x3333333333333333)).cast(U32))
    return b.build()


def parse_scalar() -> Function:
    b = Builder("nvol_parse_eight_digits_scalar", U32, doc=DOC_PARSE)
    p = b.param("p", PTR)
    v = b.let("v", prim("load64", p), mut=True)
    b.set(v, (v & 0x0F0F0F0F0F0F0F0F).wmul(2561) >> 8)
    b.set(v, (v & 0x00FF00FF00FF00FF).wmul(6553601) >> 16)
    b.ret(((v & 0x0000FFFF0000FFFF).wmul(42949672960001) >> 32).cast(U32))
    return b.build()


def _table(b, name, words, size):
    data = [x for w in words for x in w.to_bytes(size, "little")]
    return b.let(name, prim("vtable16", *[lift(x, U8) for x in data]))


def parse_sse42() -> Function:
    b = Builder("nvol_parse_eight_digits_sse42", U32, isa="sse42", doc=DOC_PARSE)
    p = b.param("p", PTR)
    m10 = _table(b, "m10", (10, 1) * 8, 1)
    m100 = _table(b, "m100", (100, 1) * 4, 2)
    m10000 = _table(b, "m10000", (10000, 1) * 4, 2)
    d = b.let("d", prim("vsub8", prim("load8v", p), prim("splat", lift(ord("0"), U8))))
    t1 = b.let("t1", prim("vmaddubs", d, m10))
    t2 = b.let("t2", prim("vmadd16", t1, m100))
    t3 = b.let("t3", prim("vpackus32", t2, t2))
    b.ret(prim("lane32_0", prim("vmadd16", t3, m10000)))
    return b.build()


def build(isas: list, mod: Module) -> None:
    """SSE4.2 (packusdw is SSE4.1), then SWAR on x86_64; SWAR on aarch64, as simdjson's arm64."""
    scalar = parse_scalar()
    mod.functions += [is_eight_digits(), scalar]
    if "neon" in [i.name for i in isas]:
        entries = [(None, scalar.name)]
    else:
        sse42 = parse_sse42()
        mod.functions.append(sse42)
        entries = [(prim("cpu_sse42"), sse42.name), (None, scalar.name)]
    rb = Builder("nvol_parse_eight_digits", U32, public=True, doc=DOC_PARSE)
    rb.param("p", PTR)
    f = rb.build()
    f.ifunc = entries
    mod.functions.append(f)
