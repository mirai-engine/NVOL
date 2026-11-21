"""memmem: forward substring search (memchr 2.8.3 `memmem::Finder`).

This is the path ripgrep reaches via `regex-automata::util::prefilter::memmem`
(`Finder::new(needle)` once, `finder.find(haystack)` per buffer). The searcher
selection mirrors `memmem/searcher.rs`:

    nlen == 0            -> match at 0
    nlen == 1            -> memchr
    2 <= nlen <= 32      -> packed pair (SSE2/AVX2); Rabin-Karp if the
                            haystack is shorter than the pair finder needs
    nlen > 32            -> Rabin-Karp if hlen < 16, else Two-Way with the
                            packed-pair prefilter (gated by PrefilterState)

Finder state
------------
The per-needle state lives in `STATE_WORDS` little-endian u64 words that
kernels only *read* (prim `load64`). All words are written in one pass by the IR
function `nvol_memmem_init(needle, nend, out)`, so the language glue just
owns an array -- no per-language reimplementation of the construction.
"""
from __future__ import annotations

from ..ir import (BOOL, MPTR, NULLP, PTR, U32, U64, U8, USIZE, VOID, Builder, Call, Const,
                  Function, Module, NONE, TableLoad, land, lift, lor, max_, min_, prim)
from ..isa import ISA, ISAS, all_lanes_mask, mask_ty, movemask
from .rank import RANK

# ---- state layout -----------------------------------------------------------
F_I1, F_I2, F_RK_HASH, F_RK_POW, F_BYTESET, F_CRIT, F_SMALL, F_PERIOD_OR_SHIFT = range(8)
STATE_WORDS = 8
FIELDS = ["pair_index1", "pair_index2", "rk_hash", "rk_pow2", "tw_byteset",
          "tw_critical_pos", "tw_small_period", "tw_period_or_shift"]

PACKED_MAX = 32          # do_packed_search: 2 <= nlen <= 32
RK_FAST = 16             # rabinkarp::is_fast: hlen < 16
PRE_MIN_SKIPS = 50       # PrefilterState::MIN_SKIPS
PRE_MIN_SKIP_BYTES = 8   # PrefilterState::MIN_SKIP_BYTES (shift by 3)


def _st(state, k, ty=USIZE):
    v = prim("load64", state + 8 * k if k else state)
    return v if ty is U64 else v.cast(ty)


# ---- shared scalar helpers --------------------------------------------------
def is_equal() -> Function:
    """memchr `arch::all::is_equal_raw`: 4, then 2, then 1 byte at a time.

    Written over one index into both buffers rather than advancing two
    pointers and a remainder: inlined into the packed-pair loop, the pointer
    form needed three more registers, and on Win64 that pushed the loop bound
    to the stack (one reload every 32 bytes, -4 % to -9 % once the haystack
    left L1). The index form adds one register and keeps the loop clean."""
    b = Builder("nvol_is_equal", BOOL, doc="x[0..n) == y[0..n)")
    x, y, n = b.param("x", PTR), b.param("y", PTR), b.param("n", USIZE)
    k = b.var("k", 0)
    with b.while_(k + 4 <= n):
        with b.if_(prim("load32u", x + k).ne(prim("load32u", y + k))):
            b.ret(False)
        b.set(k, k + 4)
    with b.if_(k + 2 <= n):
        with b.if_(prim("load16u", x + k).ne(prim("load16u", y + k))):
            b.ret(False)
        b.set(k, k + 2)
    with b.if_(k < n):
        with b.if_((x + k).load8().ne((y + k).load8())):
            b.ret(False)
    b.ret(True)
    return b.build()


def _eq(a, bb, n):
    return Call("nvol_is_equal", [a, bb, n], BOOL)


def _hdr(b):
    return (b.param("state", PTR), b.param("nstart", PTR), b.param("nend", PTR),
            b.param("hstart", PTR), b.param("hend", PTR))


def _rk_needle_hash(b, nstart, nend):
    """rabinkarp::Finder::new: hash of the needle and 2^(nlen-1), u32 wrapping."""
    h = b.var("nh", 0, U32)
    pw = b.var("pw", 1, U32)
    p = b.var("np", nstart)
    with b.while_(p < nend):
        b.set(h, (h << 1).wadd(p.load8().cast(U32)))
        with b.if_(p > nstart):
            b.set(pw, pw << 1)
        b.set(p, p + 1)
    return h, pw


def rabin_karp(oneshot: bool = False) -> Function:
    """memchr `arch::all::rabinkarp::Finder::find_raw` (base-2 rolling hash, u32 wrapping).

    oneshot: public, hashes the needle itself (memchr `memmem::find` uses this
    for haystacks < 64 bytes instead of building a full Finder)."""
    if oneshot:
        b = Builder("nvol_memmem_rk_oneshot", PTR, public=True,
                    doc="Rabin-Karp without a Finder: first needle [nstart, nend) in [hstart, hend), or NULL")
        nstart, nend, hstart, hend = (b.param("nstart", PTR), b.param("nend", PTR),
                                      b.param("hstart", PTR), b.param("hend", PTR))
    else:
        b = Builder("nvol_memmem_rk", PTR, doc="Rabin-Karp; any lengths")
        state, nstart, nend, hstart, hend = _hdr(b)
    nlen = b.let("nlen", nend - nstart)
    hlen = b.let("hlen", hend - hstart)
    with b.if_(nlen > hlen):
        b.ret(NULLP)
    if oneshot:
        nhash, pow2 = _rk_needle_hash(b, nstart, nend)
    else:
        nhash = b.let("nhash", _st(state, F_RK_HASH, U32))
        pow2 = b.let("pow2", _st(state, F_RK_POW, U32))
    last = b.let("last", hend - nlen)
    h = b.var("hash", 0, U32)
    p = b.var("p", hstart)
    with b.while_(p < hstart + nlen):
        b.set(h, (h << 1).wadd(p.load8().cast(U32)))
        b.set(p, p + 1)
    cur = b.var("cur", hstart)
    with b.while_(lift(True, BOOL)):
        with b.if_(land(h.eq(nhash), _eq(cur, nstart, nlen))):
            b.ret(cur)
        with b.if_(cur >= last):
            b.ret(NULLP)
        old = cur.load8().cast(U32)
        new = (cur + nlen).load8().cast(U32)
        b.set(h, (h.wsub(old.wmul(pow2)) << 1).wadd(new))
        b.set(cur, cur + 1)
    b.ret(NULLP)
    return b.build()


# ---- packed pair --------------------------------------------------------------
def _min_hay(nlen, i1, i2, V):
    return max_(nlen, max_(i1, i2) + V)


def packed_pair(isa: ISA, prefilter: bool) -> Function:
    """memchr `arch/generic/packedpair.rs` Finder::find / find_prefilter.

    find: candidates from the two rare bytes, verified with is_equal.
    prefilter: first candidate offset only (no verification), index or NONE.
    Requires hend - hstart >= max(nlen, max(i1, i2) + V).
    """
    V = isa.bytes
    name = f"nvol_memmem_pp{'pre' if prefilter else ''}_{isa.name}"
    b = Builder(name, USIZE if prefilter else PTR, isa=isa.name,
                doc=("first packed-pair candidate index" if prefilter else "packed-pair search")
                    + f"; requires hlen >= max(nlen, max(i1, i2) + {V})")
    state, nstart, nend, hstart, hend = _hdr(b)
    nlen = b.let("nlen", nend - nstart)
    i1 = b.let("i1", _st(state, F_I1))
    i2 = b.let("i2", _st(state, F_I2))
    v1 = b.let("v1", prim("splat", (nstart + i1).load8()))
    v2 = b.let("v2", prim("splat", (nstart + i2).load8()))
    minh = b.let("minh", _min_hay(nlen, i1, i2, V))
    mx = b.let("max", hend - minh)
    cur = b.var("cur", hstart)

    def chunk(at, mask=None):
        c1 = prim("load_u", at + i1)
        c2 = prim("load_u", at + i2)
        offs = movemask(isa, prim("vand", prim("cmpeq", c1, v1), prim("cmpeq", c2, v2)))
        if prefilter:
            m = b.let("offs", offs)
            with b.if_(m.ne(0)):
                b.ret((at - hstart) + prim("first_offset", m))
            return
        m = b.var("offs", offs if mask is None else offs & mask)
        with b.while_(m.ne(0)):
            c = b.let("cand", at + prim("first_offset", m))
            with b.if_((hend - c) < nlen):
                b.ret(NULLP)
            with b.if_(_eq(nstart, c, nlen)):
                b.ret(c)
            b.set(m, m & (m - 1))

    with b.while_(cur <= mx):
        chunk(cur)
        b.set(cur, cur + V)
    with b.if_(cur < hend):
        if prefilter:
            chunk(mx)
        else:
            with b.if_((hend - cur) < nlen):
                b.ret(NULLP)
            # drop the `overlap` lanes already searched (NEON: 4 bits per lane;
            # memchr's NEON mask only clears n + 2 bits here -- correct but it
            # re-verifies candidates; NVOL clears exactly 4n)
            ov = cur - mx
            if isa.lane_bits == 4:
                ov = ov << 2
            overlap = b.let("overlap", ov.cast(mask_ty(isa)))
            mask = b.let("tail_mask", all_lanes_mask(isa) << overlap)
            chunk(mx, mask)
    b.ret(NONE if prefilter else NULLP)
    return b.build()


# ---- Two-Way ----------------------------------------------------------------
def two_way(isa: ISA) -> Function:
    """memchr `arch/all/twoway.rs` Finder::find_with_prefilter (forward), with the
    packed-pair prefilter of `isa` and memchr's PrefilterState heuristics.
    Requires nlen >= 2 (it is only selected for nlen > 32)."""
    V = isa.bytes
    b = Builder(f"nvol_memmem_tw_{isa.name}", PTR, isa=isa.name, doc="Two-Way + prefilter")
    state, nstart, nend, hstart, hend = _hdr(b)
    nlen = b.let("nlen", nend - nstart)
    hlen = b.let("hlen", hend - hstart)
    crit = b.let("crit", _st(state, F_CRIT))
    small = b.let("small", _st(state, F_SMALL).ne(0))
    pos_ = b.let("period_or_shift", _st(state, F_PERIOD_OR_SHIFT))
    byteset = b.let("byteset", _st(state, F_BYTESET, U64))
    i1 = b.let("i1", _st(state, F_I1))
    i2 = b.let("i2", _st(state, F_I2))
    rare = b.let("rare", (nstart + i1).load8())
    min16 = b.let("min16", _min_hay(nlen, i1, i2, 16))
    min32 = b.let("min32", _min_hay(nlen, i1, i2, 32)) if V == 32 else None
    skips = b.var("skips", 1, USIZE)      # PrefilterState::new()
    skipped = b.var("skipped", 0, USIZE)
    pos = b.var("pos", 0, USIZE)
    shift = b.var("shift", 0, USIZE)
    last = b.let("last_byte_pos", nlen - 1)
    pre_args = [state, nstart, nend]

    def is_effective():
        eff = b.var("eff", False, BOOL)
        with b.if_(skips.ne(0)):
            with b.if_((skips - 1) < PRE_MIN_SKIPS):
                b.set(eff, True)
            with b.else_():
                with b.if_(skipped >= ((skips - 1) << 3)):
                    b.set(eff, True)
                with b.else_():
                    b.set(skips, 0)
        return eff

    def pre_find():
        """r = prefilter over haystack[pos..]; update state; returns var (NONE = none)."""
        sub = b.let("sub", hlen - pos)
        at = b.let("at", hstart + pos)
        r = b.var("r", NONE)
        with b.if_(sub < min16):
            # Prefilter::find_simple: memchr the rarest byte, back off by its offset
            q = b.var("q", at)
            with b.while_(land(q < hend, q.load8().ne(rare))):
                b.set(q, q + 1)
            with b.if_(q < hend):
                k = b.let("k", q - at)
                with b.if_(k >= i1):
                    b.set(r, k - i1)
                with b.else_():
                    b.set(r, 0)
        with b.else_():
            if V == 32:
                with b.if_(sub < min32):
                    b.set(r, Call("nvol_memmem_pppre_sse2", pre_args + [at, hend], USIZE))
                with b.else_():
                    b.set(r, Call("nvol_memmem_pppre_avx2", pre_args + [at, hend], USIZE))
            else:
                b.set(r, Call(f"nvol_memmem_pppre_{isa.name}", pre_args + [at, hend], USIZE))
        b.set(skips, skips + 1)
        with b.if_(r.eq(NONE)):
            b.set(skipped, skipped + sub)
        with b.else_():
            b.set(skipped, skipped + r)
        return r

    def hb(i):
        return (hstart + pos + i).load8()

    def nb(i):
        return (nstart + i).load8()

    def contains(byte):
        return ((byteset >> (byte & 63).cast(U64)) & 1).ne(0)

    with b.while_((pos + nlen) <= hlen):
        i = b.var("i", max_(crit, shift))
        eff = is_effective()
        with b.if_(eff):
            r = pre_find()
            with b.if_(r.eq(NONE)):
                b.ret(NULLP)
            b.set(pos, pos + r)
            b.set(shift, 0)
            b.set(i, crit)
            with b.if_((pos + nlen) > hlen):
                b.ret(NULLP)
        with b.if_(lift(False, BOOL).eq(contains(hb(last)))):
            b.set(pos, pos + nlen)
            b.set(shift, 0)
        with b.else_():
            with b.if_(small.eq(False)):
                b.set(i, crit)          # large period: always restart at crit
            with b.while_(land(i < nlen, nb(i).eq(hb(i)))):
                b.set(i, i + 1)
            with b.if_(i < nlen):
                b.set(pos, pos + (i - crit) + 1)
                b.set(shift, 0)
            with b.else_():
                with b.if_(small):
                    j = b.var("j", crit)
                    with b.while_(land(j > shift, nb(j).eq(hb(j)))):
                        b.set(j, j - 1)
                    with b.if_(land(j <= shift, nb(shift).eq(hb(shift)))):
                        b.ret(hstart + pos)
                    b.set(pos, pos + pos_)
                    b.set(shift, nlen - pos_)
                with b.else_():
                    ok = b.var("ok", True, BOOL)
                    jj = b.var("jj", crit)
                    with b.while_(land(ok, jj > 0)):
                        b.set(jj, jj - 1)
                        with b.if_(nb(jj).ne(hb(jj))):
                            b.set(ok, False)
                    with b.if_(ok):
                        b.ret(hstart + pos)
                    b.set(pos, pos + pos_)
    b.ret(NULLP)
    return b.build()


# ---- construction -----------------------------------------------------------
def _suffix(maximal: bool) -> Function:
    """memchr twoway `Suffix::forward`; returns pos | period << 32."""
    b = Builder(f"nvol_memmem_suffix_{'max' if maximal else 'min'}", U64,
                doc="Two-Way critical factorization helper (needle < 4 GiB)")
    n, nend = b.param("nstart", PTR), b.param("nend", PTR)
    nlen = b.let("nlen", nend - n)
    spos = b.var("spos", 0, USIZE)
    period = b.var("period", 1, USIZE)
    cand = b.var("cand", 1, USIZE)
    off = b.var("off", 0, USIZE)
    with b.while_((cand + off) < nlen):
        current = b.let("current", (n + spos + off).load8())
        candidate = b.let("candidate", (n + cand + off).load8())
        accept = (candidate > current) if maximal else (candidate < current)
        skip = (candidate < current) if maximal else (candidate > current)
        with b.if_(accept):
            b.set(spos, cand); b.set(period, 1); b.set(cand, cand + 1); b.set(off, 0)
        with b.else_():
            with b.if_(skip):
                b.set(cand, cand + off + 1); b.set(off, 0); b.set(period, cand - spos)
            with b.else_():
                with b.if_((off + 1).eq(period)):
                    b.set(cand, cand + period); b.set(off, 0)
                with b.else_():
                    b.set(off, off + 1)
    b.ret(spos.cast(U64) | (period.cast(U64) << 32))
    return b.build()


def init() -> Function:
    """Fill the STATE_WORDS u64 words of a Finder in one pass, O(len(needle)).

    (memchr's Searcher::new: Pair::with_ranker, rabinkarp::Finder::new and,
    only for needles longer than PACKED_MAX, twoway::Finder::new.)"""
    # noinline: inlined by LTO, its speed swung with the call site (114 ns in
    # one binary, 301 ns in another, for the same 40-byte needle); standalone
    # it is a steady ~230 ns, level with or ahead of memchr's Finder::new.
    b = Builder("nvol_memmem_init", VOID, public=True, noinline=True,
                doc="Build a Finder for needle [nstart, nend): writes NVOL_MEMMEM_STATE_WORDS u64 to out")
    n, nend, out = b.param("nstart", PTR), b.param("nend", PTR), b.param("out", MPTR)
    nlen = b.let("nlen", nend - n)

    def put(k, v):
        b.store64(out + 8 * k if k else out, v)

    def rank(x):
        return TableLoad("rank", x.cast(USIZE))

    # pair (Pair::with_ranker)
    x1 = b.var("idx1", 0, USIZE)
    x2 = b.var("idx2", 0, USIZE)
    with b.if_(nlen >= 2):
        r1 = b.var("rare1", n.load8())
        r2 = b.var("rare2", (n + 1).load8())
        b.set(x2, 1)
        with b.if_(rank(r2) < rank(r1)):
            t = b.let("tmp", r1)
            b.set(r1, r2); b.set(r2, t); b.set(x1, 1); b.set(x2, 0)
        i = b.var("i", 2, USIZE)
        lim = b.let("lim", min_(nlen, 255))
        with b.while_(i < lim):
            by = b.let("byte", (n + i).load8())
            with b.if_(rank(by) < rank(r1)):
                b.set(r2, r1); b.set(x2, x1); b.set(r1, by); b.set(x1, i)
            with b.else_():
                with b.if_(land(by.ne(r1), rank(by) < rank(r2))):
                    b.set(r2, by); b.set(x2, i)
            b.set(i, i + 1)
    put(F_I1, x1.cast(U64))
    put(F_I2, x2.cast(U64))
    # Rabin-Karp hash and 2^(n-1)
    h, pw = _rk_needle_hash(b, n, nend)
    put(F_RK_HASH, h.cast(U64))
    put(F_RK_POW, pw.cast(U64))
    # Two-Way -- only read when nlen > PACKED_MAX, so skip the O(m) work otherwise
    with b.if_(nlen <= PACKED_MAX):
        for k in (F_BYTESET, F_CRIT, F_SMALL, F_PERIOD_OR_SHIFT):
            put(k, lift(0, U64))
        b.ret()
    bits = b.var("bits", 0, U64)
    q = b.var("q", n)
    with b.while_(q < nend):
        b.set(bits, bits | (lift(1, U64) << (q.load8() & 63).cast(U64)))
        b.set(q, q + 1)
    put(F_BYTESET, bits)
    smin = b.let("smin", Call("nvol_memmem_suffix_min", [n, nend], U64))
    smax = b.let("smax", Call("nvol_memmem_suffix_max", [n, nend], U64))
    mask = 0xFFFF_FFFF
    minpos = b.let("minpos", (smin & mask).cast(USIZE))
    maxpos = b.let("maxpos", (smax & mask).cast(USIZE))
    crit = b.var("crit", maxpos)
    plb = b.var("plb", (smax >> 32).cast(USIZE))
    with b.if_(minpos > maxpos):
        b.set(crit, minpos)
        b.set(plb, (smin >> 32).cast(USIZE))
    put(F_CRIT, crit.cast(U64))
    # Shift::forward
    large = b.let("large", max_(crit, nlen - crit))
    with b.if_((crit << 1) < nlen):
        # is_suffix(u = needle[..crit], needle = v[..plb] = needle[crit..crit+plb])
        with b.if_(land(plb <= crit, _eq(n + (crit - plb), n + crit, plb))):
            put(F_SMALL, lift(1, U64))
            put(F_PERIOD_OR_SHIFT, plb.cast(U64))
            b.ret()
    put(F_SMALL, lift(0, U64))
    put(F_PERIOD_OR_SHIFT, large.cast(U64))
    return b.build()


# ---- dispatch -------------------------------------------------------------
def _entry(isa: str) -> Function:
    b = Builder(f"nvol_memmem_find_entry_{isa}", PTR, isa=isa, doc=f"memmem: {isa} entry point")
    state, nstart, nend, hstart, hend = _hdr(b)
    args = [state, nstart, nend, hstart, hend]
    nlen = b.let("nlen", nend - nstart)
    hlen = b.let("hlen", hend - hstart)
    with b.if_(hlen < nlen):
        b.ret(NULLP)
    with b.if_(nlen.eq(0)):
        b.ret(hstart)
    with b.if_(nlen.eq(1)):
        one = "nvol_memchr_raw" if isa == "neon" else f"nvol_memchr_entry_{isa}"
        b.ret(Call(one, [nstart.load8(), hstart, hend], PTR))
    with b.if_(nlen <= PACKED_MAX):
        i1 = b.let("i1", _st(state, F_I1))
        i2 = b.let("i2", _st(state, F_I2))
        with b.if_(hlen < _min_hay(nlen, i1, i2, 16)):
            b.ret(Call("nvol_memmem_rk", args, PTR))
        if isa == "avx2":
            with b.if_(hlen < _min_hay(nlen, i1, i2, 32)):
                b.ret(Call("nvol_memmem_pp_sse2", args, PTR))
        b.ret(Call(f"nvol_memmem_pp_{isa}", args, PTR))
    with b.if_(hlen < RK_FAST):
        b.ret(Call("nvol_memmem_rk", args, PTR))
    b.ret(Call(f"nvol_memmem_tw_{isa}", args, PTR))
    return b.build()


def build(isas: list, mod: Module) -> None:
    mod.tables["rank"] = RANK
    mod.functions += [is_equal(), rabin_karp(), rabin_karp(oneshot=True),
                      _suffix(False), _suffix(True), init()]
    for isa in isas:
        mod.functions.append(packed_pair(isa, prefilter=True))
    for isa in isas:
        mod.functions += [packed_pair(isa, prefilter=False), two_way(isa)]
    targets = []
    names = [i.name for i in isas]
    if "neon" in names:
        f = _entry("neon"); mod.functions.append(f)
        targets.append((None, f.name))       # single target: printed as a direct call
    else:
        if "avx2" in names:
            f = _entry("avx2"); mod.functions.append(f)
            targets.append((prim("cpu_avx2"), f.name))
        f = _entry("sse2"); mod.functions.append(f)
        targets.append((None, f.name))
    rb = Builder("nvol_memmem_find_raw", PTR, public=True,
                 doc="First occurrence of needle [nstart, nend) in [hstart, hend), or NULL. "
                     "`state` = NVOL_MEMMEM_STATE_WORDS words written by nvol_memmem_init.")
    _hdr(rb)
    raw = rb.build()
    raw.ifunc = targets
    mod.functions.append(raw)
    mod.api.append(("memmem", "nvol_memmem_find_raw", 0))
