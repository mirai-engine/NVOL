"""UTF-8 validation (PENDING 36): simdjson's utf8_lookup4 algorithm (Keiser and Lemire,
"Validating UTF-8 In Less Than One Instruction Per Byte", 2021), as its utf8_checker runs it
inside stage 1, plus a scalar validator for the tail and for CPUs without pshufb.

nvol_utf8_valid(p, len) returns 1 when p[0..len) is valid UTF-8 (RFC 3629: no overlong forms,
no surrogates, nothing above U+10FFFF), else 0; the same set Python's strict decoder accepts.

Per 64-byte block: a block without a byte >= 0x80 only adds the previous block's
"incomplete" flag to the error. Otherwise each vector v, with prev the vector before it:

    prev1..3 = v shifted up 1..3 bytes, prev's top bytes entering
    sc       = T1H[prev1 >> 4] & T1L[prev1 & 15] & T2H[v >> 4]        (check_special_cases)
    must23   = sat_sub(prev2, 0xE0 - 0x80) | sat_sub(prev3, 0xF0 - 0x80)
    error   |= (must23 & 0x80) ^ sc                                   (check_multibyte_lengths)

and the block is incomplete when its last bytes start a sequence it does not finish
(byte 63 >= 0xC0, byte 62 >= 0xE0 or byte 61 >= 0xF0). The bytes after the last whole block
are checked by the scalar validator, from the lead byte of the code point the blocks end in
(at most 3 continuation bytes back), so the last block's flag is not needed. The flag stays a
vector, as in simdjson (one vpsubusb against vtail3's bound, OR'd into the error on an ASCII
block): three byte compares per non-ASCII block cost ~5 % of stage 1 on twitter.json."""
from __future__ import annotations
from ..ir import PTR, U8, U64, USIZE, Builder, Function, Module, Not, land, lift, lor, prim
from ..isa import ISA, ISAS, movemask
from .memchr import _or_tree

# check_special_cases' error bits
TOO_SHORT, TOO_LONG, OVERLONG_3, TOO_LARGE = 1 << 0, 1 << 1, 1 << 2, 1 << 3
SURROGATE, OVERLONG_2, TOO_LARGE_1000, OVERLONG_4, TWO_CONTS = 1 << 4, 1 << 5, 1 << 6, 1 << 6, 1 << 7
CARRY = TOO_SHORT | TOO_LONG | TWO_CONTS
_L = TOO_LARGE | TOO_LARGE_1000
# byte 1's high nibble: ASCII x8, continuation x4, 110 0, 110 1, 1110, 1111
T1H = ((TOO_LONG,) * 8 + (TWO_CONTS,) * 4
       + (TOO_SHORT | OVERLONG_2, TOO_SHORT, TOO_SHORT | OVERLONG_3 | SURROGATE,
          TOO_SHORT | TOO_LARGE | TOO_LARGE_1000 | OVERLONG_4))
# byte 1's low nibble
T1L = (CARRY | OVERLONG_3 | OVERLONG_2 | OVERLONG_4, CARRY | OVERLONG_2, CARRY, CARRY,
       CARRY | TOO_LARGE, CARRY | _L, CARRY | _L, CARRY | _L,
       CARRY | _L, CARRY | _L, CARRY | _L, CARRY | _L, CARRY | _L,
       CARRY | _L | SURROGATE, CARRY | _L, CARRY | _L)
# byte 2's high nibble: ASCII x8, 1000, 1001, 101_ x2, 11__ x4
T2H = ((TOO_SHORT,) * 8
       + (TOO_LONG | OVERLONG_2 | TWO_CONTS | OVERLONG_3 | TOO_LARGE_1000 | OVERLONG_4,
          TOO_LONG | OVERLONG_2 | TWO_CONTS | OVERLONG_3 | TOO_LARGE,
          TOO_LONG | OVERLONG_2 | TWO_CONTS | SURROGATE | TOO_LARGE,
          TOO_LONG | OVERLONG_2 | TWO_CONTS | SURROGATE | TOO_LARGE)
       + (TOO_SHORT,) * 4)

HI8 = 0x8080808080808080

DOC = ("1 when p[0..len) is valid UTF-8 (no overlong forms, surrogates or code points above "
       "U+10FFFF), else 0")


def _params(b):
    return b.param("p", PTR), b.param("len", USIZE)


def scalar_validate(b, s, end):
    """Return 0 from the function unless s[0..end) is valid UTF-8 (Unicode table 3-7): eight
    ASCII bytes at a time while they last, then one code point."""
    with b.while_(s < end):
        with b.while_(land(end - s >= 8, (prim("load64", s) & HI8).eq(0))):
            b.set(s, s + 8)
        with b.if_(s < end):
            c = b.let("c", s.load8())
            with b.if_(c < 0x80):
                b.set(s, s + 1)
            with b.else_():
                with b.if_(lor(c < 0xC2, c > 0xF4)):
                    b.ret(0)
                n = b.var("n", 2, USIZE)
                lo = b.var("lo", 0x80, U8)
                hi = b.var("hi", 0xBF, U8)
                with b.if_(c > 0xDF):
                    b.set(n, 3)
                    with b.if_(c.eq(0xE0)):
                        b.set(lo, 0xA0)
                    with b.if_(c.eq(0xED)):
                        b.set(hi, 0x9F)
                    with b.if_(c > 0xEF):
                        b.set(n, 4)
                        with b.if_(c.eq(0xF0)):
                            b.set(lo, 0x90)
                        with b.if_(c.eq(0xF4)):
                            b.set(hi, 0x8F)
                with b.if_(end - s < n):
                    b.ret(0)
                c1 = b.let("c1", (s + 1).load8())
                with b.if_(lor(c1 < lo, c1 > hi)):
                    b.ret(0)
                with b.if_(n > 2):
                    with b.if_(((s + 2).load8() & 0xC0).ne(0x80)):
                        b.ret(0)
                    with b.if_(n > 3):
                        with b.if_(((s + 3).load8() & 0xC0).ne(0x80)):
                            b.ret(0)
                b.set(s, s + n)


def _consts(b, isa):
    def table(name, t):
        return b.let(name, prim("vtable16", *[lift(x, U8) for x in t]))

    def splat(name, x):
        return b.let(name, prim("splat", lift(x, U8)))
    return {"t1h": table("t1h", T1H), "t1l": table("t1l", T1L), "t2h": table("t2h", T2H),
            "k0f": splat("k0f", 0x0F), "k60": splat("k60", 0xE0 - 0x80),
            "k70": splat("k70", 0xF0 - 0x80), "k80": splat("k80", 0x80),
            "kmax": b.let("kmax", prim("vtail3", *[lift(x, U8) for x in (0xEF, 0xDF, 0xBF)]))}


def check_vector(b, isa, k, v, prev, err):
    """utf8_checker::check_utf8_bytes: err |= the errors of v, after prev."""
    p1 = b.let("prev1", prim("shin1", v, prev))
    sc = b.let("sc", prim("vand", prim("vand",
                                       prim("vshuffle", k["t1h"], prim("vnib_hi", p1)),
                                       prim("vshuffle", k["t1l"], prim("vand", p1, k["k0f"]))),
                          prim("vshuffle", k["t2h"], prim("vnib_hi", v))))
    must23 = prim("vor", prim("vsubs8", prim("shin2", v, prev), k["k60"]),
                  prim("vsubs8", prim("shin3", v, prev), k["k70"]))
    b.set(err, prim("vor", err, prim("vxor", prim("vand", must23, k["k80"]), sc)))


def ascii_block(isa, vs):
    """No byte of the block has its msb set."""
    anyv = _or_tree(vs, "vor")
    return movemask(isa, prim("msb_lanes", anyv) if isa.lane_bits == 4 else anyv).eq(0)


def incomplete_vector(k, v):
    """utf8_checker::is_incomplete: non-zero lanes where v's last bytes start a code point that
    v does not finish (byte -1 >= 0xC0, -2 >= 0xE0, -3 >= 0xF0), as one saturating subtraction."""
    return prim("vsubs8", v, k["kmax"])


def incomplete(blk):
    """The block's last bytes start a code point it does not finish (utf8_checker::is_incomplete
    on its last bytes)."""
    return lor(lor((blk + 63).load8() > 0xBF, (blk + 62).load8() > 0xDF), (blk + 61).load8() > 0xEF)


def kernel(isa: ISA | None) -> Function:
    tag = isa.name if isa else "scalar"
    b = Builder(f"nvol_utf8_valid_{tag}", USIZE, isa=isa.name if isa else None,
                doc=f"UTF-8 validation, {tag}")
    p, n = _params(b)
    end = b.let("end", p + n)
    cur = b.var("cur", p)
    if isa:
        V = isa.bytes
        k = _consts(b, isa)
        end64 = b.let("end64", p + ((n >> 6) << 6))
        prev = b.var("prev", prim("vzero"))
        err = b.var("err", prim("vzero"))
        inc = b.var("inc", prim("vzero"))   # the last non-ASCII block was incomplete
        with b.while_(cur < end64):
            vs = [b.let("in", prim("load_u", cur + V * j if j else cur)) for j in range(64 // V)]
            with b.if_(ascii_block(isa, vs), likely=True):
                b.set(err, prim("vor", err, inc))
            with b.else_():
                for j, v in enumerate(vs):
                    check_vector(b, isa, k, v, prev if j == 0 else vs[j - 1], err)
                b.set(inc, incomplete_vector(k, vs[-1]))
            b.set(prev, vs[-1])
            b.set(cur, cur + 64)
        with b.if_(Not(prim("is_zero", err))):
            b.ret(0)
        # the tail starts at the lead byte of the code point the blocks end in
        with b.if_(cur > p):
            with b.if_(((cur - 1).load8() & 0xC0).eq(0x80)):
                b.set(cur, cur - 1)
                with b.if_(((cur - 1).load8() & 0xC0).eq(0x80)):
                    b.set(cur, cur - 1)
                    with b.if_(((cur - 1).load8() & 0xC0).eq(0x80)):
                        b.set(cur, cur - 1)
            with b.if_((cur - 1).load8() > 0xBF):
                b.set(cur, cur - 1)
    scalar_validate(b, cur, end)
    b.ret(1)
    return b.build()


def build(isas: list, mod: Module) -> None:
    """AVX2, then SSSE3 (pshufb), then the scalar validator on x86_64; NEON on aarch64."""
    if "neon" in [i.name for i in isas]:
        neon = kernel(ISAS["neon"])
        mod.functions.append(neon)
        entries = [(None, neon.name)]
    else:
        scalar, ssse3, avx2 = kernel(None), kernel(ISAS["ssse3"]), kernel(ISAS["avx2"])
        mod.functions += [scalar, ssse3, avx2]
        entries = [(prim("cpu_avx2"), avx2.name), (prim("cpu_ssse3"), ssse3.name),
                   (None, scalar.name)]
    rb = Builder("nvol_utf8_valid", USIZE, public=True, doc=DOC)
    _params(rb)
    f = rb.build()
    f.ifunc = entries
    mod.functions.append(f)
