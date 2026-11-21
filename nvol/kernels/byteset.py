"""Byte-set search: the first byte that belongs to a set, and the first position
where two sets match at two fixed offsets ("packed pair" over sets).

Parity target: ugrep 7.8.5 / RE/flex `advance_pattern_pinN_one` (first byte of
an N-byte set, N = 2..16) and `pinN_pma` (byte sets at offsets lcp and lcs).
ugrep tests N bytes with N `cmpeq` + `or`; NVOL uses exact set membership
with two nibble lookups (Wojciech Mula's "SIMD byte lookup"), so the cost per
vector does not grow with N:

    idx = x & 0x8F                      low nibble, msb kept
    row = pshufb(tA, idx) | pshufb(tB, idx - 0x80)
                                        tA answers for x < 0x80, tB for x >= 0x80;
                                        pshufb (and NEON tbl) give 0 for an index
                                        with the msb set, which picks the table
    bit = pshufb(bitpos, x >> 4)        1 << (high nibble & 7)
    x in S  <=>  (row & bit) == bit

When the set is a low range [0, T] plus at most RANGE_EXTRA other bytes (the JSON string
stop set: the control bytes 0x00-0x1F, '"' and '\'), an unsigned min and a few compares are
cheaper than the lookup, and are what rapidjson's and simdjson's own loops do:

    x in S  <=>  min(x, T) == x  or  x == e1  or ...  ("range" sets, kernels _r0.._r3)

When every member has its own low nibble (the JSON whitespace " \\t\\n\\r", for
one), a single lookup is exact and much cheaper ("unique-nibble" sets):

    x in S  <=>  pshufb(U, x & 0x0F) == x
                                        U[lo] = the member with low nibble lo, or a
                                        filler whose low nibble is not lo

`nvol_find_not_byteset` searches for the first byte *outside* the set (skip
whitespace, skip digits) with the same kernels and the comparison inverted. With SSE4.2 and
a set of 1-16 bytes without 0, it can also run on `pcmpistri` (`nvol_find_not_byteset_sse42`):
one instruction per 16 bytes against the member list kept in the class, and a single load to
set up, which is what rapidjson's SSE4.2 whitespace skip does. A caller compiled for SSE4.2
inlines it for short runs; the x86 entry without AVX2 uses it when the CPU has SSE4.2.
(Only negated: with implicit lengths a 0 byte in the text ends the comparison, which is
right for "outside a set without 0" but would hide members after it.)

`nvol_copy_until_byteset` copies to `dst` while it searches (each loaded vector is stored
before it is tested), and returns where the first member is, or end: the string-copy loop of
a JSON parser or writer ("copy until a quote, a backslash or a control byte") in one pass,
like rapidjson's own SSE2 loop and simdjson's copy_and_find. `dst` must have room for
end - start bytes: bytes after the copied prefix may be overwritten (whole vectors are
stored); source and destination must not overlap.

Data layout
-----------
class (CLASS_BYTES = 112, written by `nvol_byteset_init`):
    @ 0  tA[16]      tA[lo] bit h set  <=>  (h << 4 | lo) in S, h < 8
    @ 16 tB[16]      tB[lo] bit h set  <=>  ((h + 8) << 4 | lo) in S
    @ 32 bitpos[16]  1 << (i & 7)
    @ 48 u64 number of distinct bytes in S
    @ 56 members[6]  the first six distinct bytes, padded with the first one
                     (small sets are searched with cmpeq chains instead)
    @ 62 u8 1 when S is a non-empty unique-nibble set
    @ 64 U[16]       unique-nibble table (U[lo] = (lo + 1) & 15 where no member has low nibble lo)
    @ 80 u8 range kernel: 0 = none, else 1 + number of extra bytes (set only when the set is
                     larger than CHAIN_MAX, the low range has 2+ bytes and is not unique-nibble)
    @ 81 u8 T        top of the low range [0, T]
    @ 82 extras[3]   the bytes above the range, padded with the first one (or with T)
    @ 85 u8 1 when the member list @ 96 is usable by pcmpistri: 1-16 members, 0 not among them
    @ 96 set16[16]   the members in increasing order, then zeros (when @ 85 is 1)
pair state (PAIR_BYTES = 256, written by `nvol_byteset_pair_init`):
    @ 0   class 1 (CLASS_BYTES)
    @ 112 class 2 (CLASS_BYTES)
    @ 224 u64 i1, @ 232 u64 i2   the offsets of the two sets
    @ 240 u64 sel    0: either set empty, 1..CHAIN_MAX: k-compare chains, CHAIN_MAX + 1: nibble
                     lookup
    @ 248 u64 reach  max(i1, i2)
    (sel and reach are precomputed so a search starts with two loads)
Every size and offset a caller may need is in CONSTANTS below, exported by every backend.
masked search output (16 bytes): u64 candidate mask, u64 span (bytes the chunk covered:
    the caller resumes at base + span once it has used up the mask)
visitor search (`_visit`): every chunk with candidates goes to the caller's visitor as
    (chunk base, candidate mask), in order; when it returns true the kernel returns that base,
    else it keeps scanning (NULL at the end). Inlined into a caller that checks candidates
    itself (ugrep's predict_match), the two loops become one, like ugrep's own; a kernel that
    returned per chunk paid its setup (selector, broadcasts) again for every such chunk.
"""
from __future__ import annotations

from ..ir import MPTR, NULLP, PTR, U64, U8, USIZE, VISIT, VOID, Builder, Call, Function, Module, Visit, land, lift, lor, max_, prim
from ..isa import ISA, ISAS, all_lanes_mask, mask_ty, movemask

CLASS_BYTES = 112
OFF_TA, OFF_TB, OFF_BITPOS, OFF_COUNT, OFF_MEMBERS = 0, 16, 32, 48, 56
OFF_UNIQ_OK, OFF_UNIQ = 62, 64
OFF_RANGE, OFF_RANGE_TOP, OFF_RANGE_EXTRA = 80, 81, 82
OFF_SET16_OK, OFF_SET16 = 85, 96
RANGE_EXTRA = 3
SMALL = 6   # members stored; sets up to CHAIN_MAX distinct bytes use cmpeq chains
CHAIN_MAX = 6
OFF_I1, OFF_I2, OFF_SEL, OFF_REACH = 2 * CLASS_BYTES, 2 * CLASS_BYTES + 8, 2 * CLASS_BYTES + 16, 2 * CLASS_BYTES + 24
PAIR_BYTES = 2 * CLASS_BYTES + 32
MASK_OUT_BYTES = 16

# What a caller needs to allocate the states and to pick a kernel itself (the e2e
# patches call the k-compare and range kernels by name). Every backend exports the
# same list: C/C++ `NVOL_BYTESET_<name>`, Rust `raw::NVOL_BYTESET_<name>`, Odin and
# Python `BYTESET_<name>`.
CONSTANTS = [
    ("CLASS_BYTES", CLASS_BYTES, "nvol_byteset_init out"),
    ("PAIR_BYTES", PAIR_BYTES, "nvol_byteset_pair_init out"),
    ("MASK_OUT_BYTES", MASK_OUT_BYTES, "nvol_find_byteset_pair_mask_raw out: u64 mask, u64 span"),
    ("CHAIN_MAX", CHAIN_MAX, "k-compare kernels _k1.._k<CHAIN_MAX>"),
    ("RANGE_EXTRA_MAX", RANGE_EXTRA, "range kernels _r0.._r<RANGE_EXTRA_MAX>"),
    ("RANGE", OFF_RANGE, "u8 in the class: 0, or 1 + e when the range kernel _r<e> applies"),
    ("SET16_OK", OFF_SET16_OK, "u8 in the class: 1 when the member list is usable by pcmpistri (1-16 members, no 0)"),
    ("SET16", OFF_SET16, "16 bytes in the class: the members in increasing order, then zeros; the only "
                         "bytes nvol_find_not_byteset_sse42 reads"),
    ("PAIR_SEL", OFF_SEL, "u64 in the pair state: 0 empty, 1..CHAIN_MAX k-compare kernel _k<sel>, "
                          "CHAIN_MAX + 1 nibble lookup"),
    ("PAIR_REACH", OFF_REACH, "u64 in the pair state: max(i1, i2)"),
]


# ---- build -------------------------------------------------------------------
def _build_class(b, s, n, out):
    z = b.var("z", 0, USIZE)
    with b.while_(z < CLASS_BYTES):
        b.store64(out + z, lift(0, U64))
        b.set(z, z + 8)
    k = b.var("k", 0, USIZE)
    with b.while_(k < 16):
        b.store8(out + OFF_BITPOS + k, lift(1, U8) << (k & 7).cast(U8))
        b.set(k, k + 1)
    with b.while_(k > 0):                       # U[lo] = (lo + 1) & 15: never equal to a byte with low nibble lo
        b.set(k, k - 1)
        b.store8(out + OFF_UNIQ + k, ((k + 1) & 15).cast(U8))
    used = b.var("used", 0, USIZE)              # low nibbles taken by members
    uniq = b.var("uniq", 1, USIZE)
    distinct = b.var("distinct", 0, USIZE)
    i = b.var("i", 0, USIZE)
    with b.while_(i < n):
        byte = b.let("byte", (s + i).load8())
        lo = b.let("lo", (byte & 0x0F).cast(USIZE))
        hi = b.let("hi", byte >> 4)
        row = b.var("row", out + OFF_TA + lo)
        with b.if_(hi >= 8):
            b.set(row, out + OFF_TB + lo)
        bit = b.let("bit", lift(1, U8) << (hi & 7))
        old = b.let("old", row.load8())
        with b.if_((old & bit).eq(0)):
            with b.if_(distinct < SMALL):
                b.store8(out + OFF_MEMBERS + distinct, byte)
            b.set(distinct, distinct + 1)
            lobit = b.let("lobit", lift(1, USIZE) << lo)
            with b.if_((used & lobit).ne(0)):
                b.set(uniq, 0)
            b.set(used, used | lobit)
            b.store8(out + OFF_UNIQ + lo, byte)
        b.store8(row, old | bit)
        b.set(i, i + 1)
    b.store64(out + OFF_COUNT, distinct.cast(U64))
    with b.if_(land(uniq.ne(0), distinct.ne(0))):
        b.store8(out + OFF_UNIQ_OK, lift(1, U8))
    first = b.let("first", (out + OFF_MEMBERS).load8())
    pad = b.var("pad", distinct)
    with b.while_(land(pad.ne(0), pad < SMALL)):
        b.store8(out + OFF_MEMBERS + pad, first)
        b.set(pad, pad + 1)
    # range form: t = length of the run 0, 1, 2, ... in S; the members above it are the extras
    t = b.var("t", 0, USIZE)
    with b.while_(land(t < 256, _scalar_member(out, t.cast(U8)))):
        b.set(t, t + 1)
    extra = b.var("extra", 0, USIZE)
    c = b.var("c", t)
    with b.while_(c < 256):
        with b.if_(_scalar_member(out, c.cast(U8))):
            with b.if_(extra < RANGE_EXTRA):
                b.store8(out + OFF_RANGE_EXTRA + extra, c.cast(U8))
            b.set(extra, extra + 1)
        b.set(c, c + 1)
    # pcmpistri member list: 1-16 members and 0 not one of them (t == 0)
    with b.if_(land(land(distinct.ne(0), distinct <= 16), t.eq(0))):
        j = b.var("j", 0, USIZE)
        m = b.var("m", 1, USIZE)
        with b.while_(m < 256):
            with b.if_(_scalar_member(out, m.cast(U8))):
                b.store8(out + OFF_SET16 + j, m.cast(U8))
                b.set(j, j + 1)
            b.set(m, m + 1)
        b.store8(out + OFF_SET16_OK, lift(1, U8))
    with b.if_(land(land(t >= 2, extra <= RANGE_EXTRA), land(distinct > CHAIN_MAX, uniq.eq(0)))):
        top = b.let("top", (t - 1).cast(U8))
        b.store8(out + OFF_RANGE, (extra + 1).cast(U8))
        b.store8(out + OFF_RANGE_TOP, top)
        fill = b.var("fill", top)
        with b.if_(extra.ne(0)):
            b.set(fill, (out + OFF_RANGE_EXTRA).load8())
        with b.while_(extra < RANGE_EXTRA):
            b.store8(out + OFF_RANGE_EXTRA + extra, fill)
            b.set(extra, extra + 1)


def init() -> Function:
    b = Builder("nvol_byteset_init", VOID, public=True,
                doc=f"Build a byte-set class from set[0..n) (duplicates allowed) into out[{CLASS_BYTES}]")
    s, n, out = b.param("set", PTR), b.param("n", USIZE), b.param("out", MPTR)
    _build_class(b, s, n, out)
    return b.build()


def init_pair() -> Function:
    b = Builder("nvol_byteset_pair_init", VOID, public=True,
                doc=f"Pair state: set1 at offset i1, set2 at offset i2, into out[{PAIR_BYTES}]")
    s1, n1, s2, n2 = b.param("set1", PTR), b.param("n1", USIZE), b.param("set2", PTR), b.param("n2", USIZE)
    i1, i2, out = b.param("i1", USIZE), b.param("i2", USIZE), b.param("out", MPTR)
    _build_class(b, s1, n1, out)
    _build_class(b, s2, n2, out + CLASS_BYTES)
    b.store64(out + OFF_I1, i1.cast(U64))
    b.store64(out + OFF_I2, i2.cast(U64))
    c1 = b.let("c1", prim("load64", out + OFF_COUNT).cast(USIZE))
    c2 = b.let("c2", prim("load64", out + CLASS_BYTES + OFF_COUNT).cast(USIZE))
    sel = b.var("sel", max_(c1, c2))
    with b.if_(sel > CHAIN_MAX):
        b.set(sel, CHAIN_MAX + 1)
    with b.if_(c1.eq(0)):
        b.set(sel, 0)
    with b.if_(c2.eq(0)):
        b.set(sel, 0)
    b.store64(out + OFF_SEL, sel.cast(U64))
    b.store64(out + OFF_REACH, max_(i1, i2).cast(U64))
    return b.build()


# ---- membership ----------------------------------------------------------------
def _tables(b, cls, isa: ISA):
    ld = "load_half_bcast" if isa.bytes == 32 else "load_u"
    return (b.let("ta", prim(ld, cls + OFF_TA if OFF_TA else cls)),
            b.let("tb", prim(ld, cls + OFF_TB)),
            b.let("bitpos", prim(ld, cls + OFF_BITPOS)))


def _member(b, x, tabs, m8f, m80):
    ta, tb, bitpos = tabs
    idx = b.let("idx", prim("vand", x, m8f))
    row = prim("vor", prim("vshuffle", ta, idx), prim("vshuffle", tb, prim("vsub8", idx, m80)))
    bit = b.let("bit", prim("vshuffle", bitpos, prim("vnib_hi", x)))
    return prim("cmpeq", prim("vand", row, bit), bit)


def _chain(b, x, vs):
    e = prim("cmpeq", x, vs[0])
    for v in vs[1:]:
        e = prim("mask_or", e, prim("cmpeq", x, v))
    return e


def _scalar_member(cls, byte, negate=False):
    """(table[lo] & bitpos[hi]) != 0, with table = tA or tB by the byte's msb
    (== 0 with negate)."""
    lo = (byte & 0x0F).cast(USIZE)
    tab = (byte >> 7).cast(USIZE) << 4          # 0 for tA, 16 for tB
    row = (cls + tab + lo).load8()
    bits = row & (lift(1, U8) << ((byte >> 4) & 7))
    return bits.eq(0) if negate else bits.ne(0)


# ---- one set -------------------------------------------------------------------
def _one_base(negate):
    return "nvol_find_not_byteset" if negate else "nvol_find_byteset"


def find_scalar(negate=False, copy=False) -> Function:
    base = "nvol_copy_until_byteset" if copy else _one_base(negate)
    b = Builder(f"{base}_scalar", PTR, doc="byte-at-a-time fallback")
    cls, start, end = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR)
    dst = b.param("dst", MPTR) if copy else None
    p = b.var("p", start)
    with b.while_(p < end):
        c = b.let("c", p.load8(), U8)
        with b.if_(_scalar_member(cls, c, negate)):
            b.ret(p)
        if copy:
            b.store8(dst + (p - start), c)
        b.set(p, p + 1)
    b.ret(end if copy else NULLP)
    return b.build()


def find_vector(isa: ISA, k=None, uniq=False, negate=False, copy=False, rng=None) -> Function:
    """k = None: nibble lookup; k: k-compare chain over the stored members;
    uniq: one lookup in the unique-nibble table; rng = e (0..RANGE_EXTRA): low range plus e
    extra bytes. negate: first byte outside the set.
    copy: store every loaded vector to dst first (copy-until; returns end, not NULL)."""
    V = isa.bytes
    suffix = "_u" if uniq else ("_r%d" % rng if rng is not None else ("_k%d" % k if k else ""))
    what = "outside" if negate else "in"
    base = "nvol_copy_until_byteset" if copy else _one_base(negate)
    b = Builder(f"{base}_{isa.name}{suffix}", PTR, isa=isa.name,
                doc=(f"copy [start, end) to dst up to the first byte in the class; requires end - start >= {V}"
                     if copy else f"first byte of [start, end) {what} the class; requires end - start >= {V}"))
    cls, start, end = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR)
    dst = b.param("dst", MPTR) if copy else None
    if uniq:
        ld = "load_half_bcast" if V == 32 else "load_u"
        ut = b.let("ut", prim(ld, cls + OFF_UNIQ))
        m0f = b.let("m0f", prim("splat", lift(0x0F, U8)))
    elif rng is not None:
        top = b.let("top", prim("splat", (cls + OFF_RANGE_TOP).load8()))
        xs = [b.let("e", prim("splat", (cls + OFF_RANGE_EXTRA + j).load8())) for j in range(rng)]
    elif k:
        cs = [b.let("c", prim("splat", (cls + OFF_MEMBERS + j).load8())) for j in range(k)]
    else:
        tabs = _tables(b, cls, isa)
        m8f = b.let("m8f", prim("splat", lift(0x8F, U8)))
        m80 = b.let("m80", prim("splat", lift(0x80, U8)))
    if negate:
        zero = b.let("zero", prim("vzero"))
    cur = b.var("cur", start)

    def chunk(at, mask=None):
        x = b.let("x", prim("load_u", at))
        if copy:
            b.store_vec(dst + (at - start), x)
        if uniq:
            e = prim("cmpeq", prim("vshuffle", ut, prim("vand", x, m0f)), x)
        elif rng is not None:
            e = prim("cmpeq", prim("vmin8", x, top), x)
            for xv in xs:
                e = prim("mask_or", e, prim("cmpeq", x, xv))
        elif k:
            e = _chain(b, x, cs)
        else:
            e = _member(b, x, tabs, m8f, m80)
        if negate:
            e = prim("cmpeq", e, zero)      # lanes are 0x00 / 0xFF: inverts them
        m = movemask(isa, e)
        hit = b.let("hit", m if mask is None else m & mask)
        with b.if_(hit.ne(0)):
            b.ret(at + prim("first_offset", hit))

    with b.while_(cur <= end - V):
        chunk(cur)
        b.set(cur, cur + V)
    with b.if_(cur < end):
        last = b.let("last", end - V)
        ov = cur - last
        if isa.lane_bits == 4:
            ov = ov << 2
        mask = b.let("tail_mask", all_lanes_mask(isa) << b.let("overlap", ov.cast(mask_ty(isa))))
        chunk(last, mask)
    b.ret(end if copy else NULLP)
    return b.build()


# ---- pair of sets ------------------------------------------------------------------
def _pair_base(masked):
    if masked == "visit":
        return "nvol_find_byteset_pair_visit"
    return "nvol_find_byteset_pair_mask" if masked else "nvol_find_byteset_pair"


def pair_scalar(masked=False, pos_bit=1) -> Function:
    """masked: also store the candidate mask (just this position: pos_bit) to out.
    "visit": every candidate position goes to the visitor (mask pos_bit)."""
    visit = masked == "visit"
    b = Builder(f"{_pair_base(masked)}_scalar", PTR, doc="position-at-a-time fallback")
    st, start, end = b.param("st", PTR), b.param("start", PTR), b.param("end", PTR)
    out = b.param("visit" if visit else "out", VISIT if visit else MPTR) if masked else None
    i1 = b.let("i1", prim("load64", st + OFF_I1).cast(USIZE))
    i2 = b.let("i2", prim("load64", st + OFF_I2).cast(USIZE))
    span = b.let("span", max_(i1, i2) + 1)
    with b.if_((end - start) < span):
        b.ret(NULLP)
    last = b.let("last", end - span)
    p = b.var("p", start)
    with b.while_(p <= last):
        c1 = _scalar_member(st, (p + i1).load8())
        with b.if_(c1):
            with b.if_(_scalar_member(st + CLASS_BYTES, (p + i2).load8())):
                if visit:
                    with b.if_(Visit(out, p, lift(pos_bit, U64))):
                        b.ret(p)
                else:
                    if masked:
                        b.store64(out, lift(pos_bit, U64))
                        b.store64(out + 8, lift(1, U64))
                    b.ret(p)
        b.set(p, p + 1)
    b.ret(NULLP)
    return b.build()


def pair_vector(isa: ISA, k=None, masked=False) -> Function:
    """k = None: nibble lookup; k: k-compare chains over the stored members.
    masked: return the chunk base and store its whole candidate mask to out.
    masked == "visit": hand every chunk with candidates to the visitor (see the module doc)."""
    visit = masked == "visit"
    V = isa.bytes
    suffix = "_k%d" % k if k else ""
    how = ("%d-compare chains" % k) if k else "nibble lookup"
    b = Builder(f"{_pair_base(masked)}_{isa.name}{suffix}", PTR, isa=isa.name,
                doc=f"first p with hay[p+i1] in set 1 and hay[p+i2] in set 2 ({how}); "
                    f"requires end - start >= max(i1, i2) + {V}")
    st, start, end = b.param("st", PTR), b.param("start", PTR), b.param("end", PTR)
    out = b.param("visit" if visit else "out", VISIT if visit else MPTR) if masked else None
    i1 = b.let("i1", prim("load64", st + OFF_I1).cast(USIZE))
    i2 = b.let("i2", prim("load64", st + OFF_I2).cast(USIZE))
    if k:
        c1 = [b.let("a", prim("splat", (st + OFF_MEMBERS + j).load8())) for j in range(k)]
        c2 = [b.let("b", prim("splat", (st + CLASS_BYTES + OFF_MEMBERS + j).load8())) for j in range(k)]
    else:
        t1 = _tables(b, st, isa)
        t2 = _tables(b, st + CLASS_BYTES, isa)
        m8f = b.let("m8f", prim("splat", lift(0x8F, U8)))
        m80 = b.let("m80", prim("splat", lift(0x80, U8)))
    mx = b.let("max", end - (max_(i1, i2) + V))      # last position a full chunk can start at
    cur = b.var("cur", start)

    def chunk(at, mask=None):
        x1, x2 = prim("load_u", at + i1), prim("load_u", at + i2)
        if k:
            e1 = b.let("e1", _chain(b, x1, c1))
            e2 = b.let("e2", _chain(b, x2, c2))
        else:
            e1 = b.let("e1", _member(b, x1, t1, m8f, m80))
            e2 = b.let("e2", _member(b, x2, t2, m8f, m80))
        m = movemask(isa, prim("vand", e1, e2))
        hit = b.let("hit", m if mask is None else m & mask)
        with b.if_(hit.ne(0)):
            if visit:
                with b.if_(Visit(out, at, hit.cast(U64))):
                    b.ret(at)
            elif masked:
                b.store64(out, hit.cast(U64))
                b.store64(out + 8, lift(V, U64))
                b.ret(at)
            else:
                b.ret(at + prim("first_offset", hit))

    with b.while_(cur <= mx):
        chunk(cur)
        b.set(cur, cur + V)
    with b.if_(cur < mx + V):                        # positions cur..mx+V-1 are left
        ov = cur - mx
        if isa.lane_bits == 4:
            ov = ov << 2
        mask = b.let("tail_mask", all_lanes_mask(isa) << b.let("overlap", ov.cast(mask_ty(isa))))
        chunk(mx, mask)
    b.ret(NULLP)
    return b.build()


def find_not_set16() -> Function:
    """First byte outside the set with pcmpistri over the member list (class @ 96): needs
    the flag @ 85 (1-16 members, no 0). A 0 byte of the text ends the comparison and is
    reported, which is right: 0 is not a member. The tail rereads the last 16 bytes; the
    bytes already checked are all members, so the first one outside is still the answer."""
    b = Builder("nvol_find_not_byteset_sse42", PTR, isa="sse42",
                doc="first byte of [start, end) outside the class, by pcmpistri; needs the class's "
                    "member list (1-16 members, no 0) and end - start >= 16")
    cls, start, end = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR)
    setv = b.let("set", prim("load_u", cls + OFF_SET16))
    cur = b.var("cur", start)
    last = b.let("last", end - 16)
    # the index is compared as the u32 pcmpistri gives and widened only on the way out:
    # widened first, GCC kept the zero-extension inside the loop (one more instruction per
    # 16 bytes than rapidjson's own SSE4.2 loop)
    with b.while_(cur <= last):
        i = b.let("i", prim("skip_set16", setv, prim("load_u", cur)))
        with b.if_(i < 16):
            b.ret(cur + i.cast(USIZE))
        b.set(cur, cur + 16)
    with b.if_(cur < end):
        t = b.let("i_tail", prim("skip_set16", setv, prim("load_u", last)))
        with b.if_(t < 16):
            b.ret(last + t.cast(USIZE))
    b.ret(NULLP)
    return b.build()


# ---- dispatch ------------------------------------------------------------------
MEMCHR = {1: "nvol_memchr", 2: "nvol_memchr2", 3: "nvol_memchr3"}


def _count(b, cls, name):
    return b.let(name, prim("load64", cls + OFF_COUNT).cast(USIZE))


def _entry_one(isa_name, small_entry):
    """Sets of 1-3 bytes -> the memchr family (unrolled cmpeq chains); else the
    nibble-lookup kernel: AVX2 entry, < 32 bytes SSSE3, < 16 scalar. small_entry
    names the memchr entry to call for this ISA ("{fn}_entry_avx2", "{fn}_raw", ...)."""
    tag = isa_name or "base"
    b = Builder(f"nvol_find_byteset_entry_{tag}", PTR, isa=isa_name, doc=f"nvol_find_byteset: {tag} entry point")
    cls, start, end = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR)
    args = [cls, start, end]
    cnt = _count(b, cls, "count")
    with b.if_(cnt.eq(0)):
        b.ret(NULLP)
    ms = [b.let("m", (cls + OFF_MEMBERS + j).load8()) for j in range(3)]
    for k in (1, 2, 3):
        with b.if_(cnt.eq(k)):
            b.ret(Call(small_entry.format(fn=MEMCHR[k]), ms[:k] + [start, end], PTR))
    _pick_one(b, isa_name, cls, cnt, args, 4, negate=False)
    return b.build()


def _pick_one(b, isa_name, cls, cnt, args, kmin, negate, base=None):
    """Kernel choice once the length is known to be worth a vector: unique-nibble
    set -> one lookup; kmin..CHAIN_MAX distinct bytes -> k-compare chain; low range
    -> range kernel; else nibble lookup. AVX2 entries take 16-byte kernels below 32
    bytes, scalar below 16. On x86 the 16-byte chains and ranges are SSE2 (compares
    and an unsigned min, no pshufb), so the base entry (isa_name None: no SSSE3
    assumed) has them too and falls back to scalar only for the lookups."""
    base = base or _one_base(negate)
    start, end = args[1], args[2]
    n = b.let("len", end - start)
    with b.if_(n < 16):
        b.ret(Call(f"{base}_scalar", args, PTR))
    uniq = b.let("uniq", (cls + OFF_UNIQ_OK).load8())
    rk = b.let("range", (cls + OFF_RANGE).load8())

    def pick(isa):
        lookup = isa is not None          # the lookups need pshufb (SSSE3 and up)
        look = "ssse3" if isa == "sse42" else isa       # SSE4.2 implies SSSE3
        chain = "sse2" if isa in (None, "ssse3", "sse42") else isa
        if lookup:
            with b.if_(uniq.ne(0)):
                b.ret(Call(f"{base}_{look}_u", args, PTR))
        for k in range(kmin, CHAIN_MAX + 1):
            with b.if_(cnt.eq(k)):
                b.ret(Call(f"{base}_{chain}_k{k}", args, PTR))
        for e in range(RANGE_EXTRA + 1):
            with b.if_(rk.eq(e + 1)):
                b.ret(Call(f"{base}_{chain}_r{e}", args, PTR))
        if isa == "sse42" and negate:     # 7-16 members without 0: pcmpistri, not the nibble lookup
            with b.if_((cls + OFF_SET16_OK).load8().ne(0)):
                b.ret(Call(f"{base}_sse42", args, PTR))
        b.ret(Call(f"{base}_{look}" if lookup else f"{base}_scalar", args, PTR))

    if isa_name == "avx2":
        with b.if_(n < 32):
            pick("ssse3")
    pick(isa_name)


def _entry_not(isa_name):
    """First byte outside the set: empty set -> start; otherwise like _entry_one,
    with k-compare chains from one byte up (there is no negated memchr)."""
    tag = isa_name or "base"
    b = Builder(f"nvol_find_not_byteset_entry_{tag}", PTR, isa=isa_name,
                doc=f"nvol_find_not_byteset: {tag} entry point")
    cls, start, end = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR)
    args = [cls, start, end]
    cnt = _count(b, cls, "count")
    with b.if_(land(cnt.eq(0), start < end)):
        b.ret(start)
    _pick_one(b, isa_name, cls, cnt, args, 1, negate=True)
    return b.build()


def _entry_copy(isa_name):
    """copy-until: like _entry_one, with k-compare chains from one byte up (no memchr
    copies) and the copying kernels; without AVX2 on x86, SSE2 chains and ranges only."""
    tag = isa_name or "base"
    b = Builder(f"nvol_copy_until_byteset_entry_{tag}", PTR, isa=isa_name,
                doc=f"nvol_copy_until_byteset: {tag} entry point")
    cls, start, end, dst = b.param("cls", PTR), b.param("start", PTR), b.param("end", PTR), b.param("dst", MPTR)
    args = [cls, start, end, dst]
    cnt = _count(b, cls, "count")
    _pick_one(b, isa_name, cls, cnt, args, 1, negate=False, base="nvol_copy_until_byteset")
    return b.build()


def _entry_pair(isa_name, masked=False):
    """k = max(distinct bytes of the two sets): k <= CHAIN_MAX -> k-compare chains, else
    nibble lookup. Short haystacks (less than max(i1, i2) + 16) -> scalar."""
    base = _pair_base(masked)
    b = Builder(f"{base}_entry_{isa_name}", PTR, isa=isa_name, doc=f"{base}: {isa_name} entry point")
    st, start, end = b.param("st", PTR), b.param("start", PTR), b.param("end", PTR)
    args = [st, start, end]
    if masked:
        args.append(b.param("visit", VISIT) if masked == "visit" else b.param("out", MPTR))
    kk = b.let("sel", prim("load64", st + OFF_SEL).cast(USIZE))
    reach = b.let("reach", prim("load64", st + OFF_REACH).cast(USIZE))
    n = b.var("len", end - start)
    with b.if_(lor(kk.eq(0), n <= reach)):
        b.ret(NULLP)
    b.set(n, n - reach)
    with b.if_(n < 16):
        b.ret(Call(f"{base}_scalar", args, PTR))
    if isa_name == "avx2":
        with b.if_(n < 32):
            for k in range(1, CHAIN_MAX + 1):
                with b.if_(kk.eq(k)):
                    b.ret(Call(f"{base}_ssse3_k{k}", args, PTR))
            b.ret(Call(f"{base}_ssse3", args, PTR))
    for k in range(1, CHAIN_MAX + 1):
        with b.if_(kk.eq(k)):
            b.ret(Call(f"{base}_{isa_name}_k{k}", args, PTR))
    b.ret(Call(f"{base}_{isa_name}", args, PTR))
    return b.build()


def _public(mod, name, doc, entries, masked=False):
    rb = Builder(f"{name}_raw", PTR, public=True, doc=doc)
    rb.param("cls" if masked == "copy" else "st", PTR)
    rb.param("start", PTR)
    rb.param("end", PTR)
    if masked == "copy":
        rb.param("dst", MPTR)
    elif masked == "visit":
        rb.param("visit", VISIT)
    elif masked:
        rb.param("out", MPTR)
    raw = rb.build()
    raw.ifunc = entries
    mod.functions.append(raw)


def build(isas: list, mod: Module) -> None:
    names = [i.name for i in isas]
    neon = "neon" in names
    pos_bit = 8 if neon else 1      # where position 0 sits in a movemask (NEON: bit 4i+3)
    mod.functions += [init(), init_pair(), find_scalar(), find_scalar(negate=True), find_scalar(copy=True),
                      pair_scalar(), pair_scalar(True, pos_bit), pair_scalar("visit", pos_bit)]
    vec = ["neon"] if neon else ["ssse3", "avx2"]
    for v in vec:
        # 16-byte chains and ranges use only SSE2 on x86: emitted for "sse2" (no target
        # attribute), so they inline into code built for baseline x86-64 too
        c = ISAS["sse2" if v == "ssse3" else v]
        mod.functions += [find_vector(ISAS[v]), find_vector(ISAS[v], uniq=True), pair_vector(ISAS[v])]
        mod.functions += [find_vector(c, k) for k in range(4, CHAIN_MAX + 1)]
        mod.functions += [find_vector(ISAS[v], negate=True), find_vector(ISAS[v], uniq=True, negate=True)]
        mod.functions += [find_vector(c, k, negate=True) for k in range(1, CHAIN_MAX + 1)]
        mod.functions += [find_vector(ISAS[v], copy=True), find_vector(ISAS[v], uniq=True, copy=True)]
        mod.functions += [find_vector(c, k, copy=True) for k in range(1, CHAIN_MAX + 1)]
        mod.functions += [find_vector(c, rng=e, negate=neg, copy=cp) for e in range(RANGE_EXTRA + 1)
                          for neg, cp in ((False, False), (True, False), (False, True))]
        mod.functions += [pair_vector(ISAS[v], k) for k in range(1, CHAIN_MAX + 1)]
        mod.functions += [pair_vector(ISAS[v], masked=True)]
        mod.functions += [pair_vector(ISAS[v], k, masked=True) for k in range(1, CHAIN_MAX + 1)]
        mod.functions += [pair_vector(ISAS[v], masked="visit")]
        mod.functions += [pair_vector(ISAS[v], k, masked="visit") for k in range(1, CHAIN_MAX + 1)]
    one_doc = "First byte of [start, end) in the class built by nvol_byteset_init, or NULL."
    not_doc = "First byte of [start, end) outside the class built by nvol_byteset_init, or NULL."
    two_doc = ("First p in [start, end) with p[i1] in set 1 and p[i2] in set 2 "
               "(state from nvol_byteset_pair_init), or NULL.")
    mask_doc = ("Like nvol_find_byteset_pair_raw, but returns the base of the first chunk with candidates "
                "and stores all its candidates to *out: position base + i is a candidate when bit i "
                "(x86_64) or bit 4i+3 (aarch64) is set. NULL when there is none.")
    copy_doc = ("Copy [start, end) to dst up to the first byte in the class built by nvol_byteset_init; "
                "returns that byte's address, or end. dst needs room for end - start bytes (whole "
                "vectors are stored, so bytes after the copied prefix may change); no overlap.")
    visit_doc = ("Every chunk with pair candidates, in order, to visit->fn(ctx, chunk, mask) (mask as "
                 "in nvol_find_byteset_pair_mask_raw); returns the chunk where the visitor returned true, "
                 "or NULL at the end. Inline the _visit kernels to fuse the visitor into their loop.")
    if neon:
        one, two, msk = _entry_one("neon", "{fn}_raw"), _entry_pair("neon"), _entry_pair("neon", True)
        neg = _entry_not("neon")
        vis = _entry_pair("neon", "visit")
        cpy = _entry_copy("neon")
        mod.functions += [one, neg, two, msk, vis, cpy]
        _public(mod, "nvol_copy_until_byteset", copy_doc, [(None, cpy.name)], masked="copy")
        _public(mod, "nvol_find_byteset_pair_visit", visit_doc, [(None, vis.name)], masked="visit")
        _public(mod, "nvol_find_byteset", one_doc, [(None, one.name)])
        _public(mod, "nvol_find_not_byteset", not_doc, [(None, neg.name)])
        _public(mod, "nvol_find_byteset_pair", two_doc, [(None, two.name)])
        _public(mod, "nvol_find_byteset_pair_mask", mask_doc, [(None, msk.name)], masked=True)
    else:
        one, base, two = _entry_one("avx2", "{fn}_entry_avx2"), _entry_one(None, "{fn}_entry_sse2"), _entry_pair("avx2")
        msk = _entry_pair("avx2", True)
        neg, neg_base, neg42 = _entry_not("avx2"), _entry_not(None), _entry_not("sse42")
        vis = _entry_pair("avx2", "visit")
        cpy, cpy_base = _entry_copy("avx2"), _entry_copy(None)
        mod.functions += [find_not_set16(), one, base, neg, neg_base, neg42, two, msk, vis, cpy, cpy_base]
        _public(mod, "nvol_copy_until_byteset", copy_doc,
                [(prim("cpu_avx2"), cpy.name), (None, cpy_base.name)], masked="copy")
        _public(mod, "nvol_find_byteset_pair_visit", visit_doc,
                [(prim("cpu_avx2"), vis.name), (None, "nvol_find_byteset_pair_visit_scalar")], masked="visit")
        # x86 without AVX2: the memchr family (SSE2) for 1-3 bytes, SSE2 chains and ranges, scalar otherwise
        _public(mod, "nvol_find_byteset", one_doc, [(prim("cpu_avx2"), one.name), (None, base.name)])
        _public(mod, "nvol_find_not_byteset", not_doc,
                [(prim("cpu_avx2"), neg.name), (prim("cpu_sse42"), neg42.name), (None, neg_base.name)])
        _public(mod, "nvol_find_byteset_pair", two_doc,
                [(prim("cpu_avx2"), two.name), (None, "nvol_find_byteset_pair_scalar")])
        _public(mod, "nvol_find_byteset_pair_mask", mask_doc,
                [(prim("cpu_avx2"), msk.name), (None, "nvol_find_byteset_pair_mask_scalar")], masked=True)
