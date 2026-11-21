"""Teddy: packed multi-literal search (aho-corasick 1.1.5 `packed::Searcher`).

ripgrep reaches it via `regex-automata::util::prefilter::teddy`:
`packed::Config::new().match_kind(LeftmostFirst).builder().extend(needles).build()`
then `searcher.find_in(haystack, span)`. Parity points:

  * build returns "none" exactly when aho-corasick's Teddy builder does: no
    patterns, an empty pattern, > 64 patterns, mask length 1 with > 16
    patterns, or no SSSE3;
  * variant choice: AVX2 -> Slim256 (<= 32 patterns) or Fat256 (> 32),
    else SSSE3 -> Slim128; mask length m = min(4, shortest pattern);
  * bucket assignment, masks, verification order and leftmost-first result are
    the same; haystacks shorter than 16 + m - 1 use aho-corasick's packed
    Rabin-Karp (`slow()`): 64 buckets over the hash of each pattern's first
    `minlen` bytes, scanned in pattern order.

Data layout
-----------
patterns blob (built by the language glue, read-only):
    u64 n | n x (u64 offset, u64 len) | pattern bytes      (offsets from blob start)
state (STATE_BYTES, written once by `nvol_teddy_init`, then read-only):
    words W_KIND, W_MASK_LEN, W_NPAT, W_MINLEN_SEARCH, W_MINLEN_PAT,
          W_SEL, W_SEL_SMALL, W_SWITCH  (variant index into fn table `teddy`)
    @ OFF_MASKS   4 masks x (lo[32], hi[32])     nybble -> bucket bits
    @ OFF_COUNTS  16 bucket sizes (u8)
    @ OFF_LISTS   16 x 64 pattern ids (u8), bucket-major, insertion order
    @ OFF_BUCKET  64 bucket-of-pattern (u8), used while building
    @ OFF_RK      64 x u64 Rabin-Karp buckets: bit pid set when hash(pattern pid's
                  first minlen bytes) % 64 is that bucket
    @ OFF_RKH     64 x u64 that hash, per pattern (compared before the bytes, as
                  aho-corasick does)
Result of a search: (start - hstart) << 8 | pattern id, or NONE.
"""
from __future__ import annotations

from ..ir import (BOOL, MPTR, NONE, PTR, U64, U8, USIZE, VOID, Builder, Call, CallTable,
                  Function, Module, land, lift, lor, min_, prim)
from ..isa import ISAS

W_KIND, W_MASK_LEN, W_NPAT, W_MINLEN_SEARCH, W_MINLEN_PAT, W_SEL, W_SEL_SMALL, W_SWITCH = range(8)
# function table `teddy`: index = variant * 4 + (m - 1)
VARIANTS = [("slim", "ssse3"), ("slim", "avx2"), ("fat", "avx2")]
KIND_NONE, KIND_SLIM128, KIND_SLIM256, KIND_FAT256 = range(4)
OFF_MASKS, OFF_COUNTS, OFF_LISTS, OFF_BUCKET, OFF_RK, OFF_RKH = 64, 320, 336, 1360, 1424, 1936
STATE_BYTES = 2448
RK_BUCKETS = 64
MAX_PATTERNS = 64


def _w(state, k):
    return prim("load64", state + 8 * k if k else state).cast(USIZE)


def _pat(blob, pid):
    """(pointer, len) of pattern pid in the blob."""
    off = prim("load64", blob + 8 + (pid << 4)).cast(USIZE)
    ln = prim("load64", blob + 16 + (pid << 4)).cast(USIZE)
    return blob + off, ln


def _is_prefix(b, pptr, plen, at, hend):
    return land(plen <= (hend - at), Call("nvol_is_equal_ac", [pptr, at, plen], BOOL))


def is_equal_ac() -> Function:
    """aho-corasick `packed::pattern::is_equal_raw` (differs from memchr's):
    n < 4 handled by fixed-size compares; otherwise 4-byte steps and one final,
    overlapping 4-byte compare -- no 2/1-byte tails, fewer branches per verify."""
    b = Builder("nvol_is_equal_ac", BOOL, doc="x[0..n) == y[0..n), aho-corasick style")
    x0, y0, n = b.param("x", PTR), b.param("y", PTR), b.param("n", USIZE)
    with b.if_(n < 4):
        with b.if_(n.eq(0)):
            b.ret(True)
        with b.if_(n.eq(1)):
            b.ret(x0.load8().eq(y0.load8()))
        with b.if_(n.eq(2)):
            b.ret(prim("load16u", x0).eq(prim("load16u", y0)))
        b.ret(land(prim("load16u", x0).eq(prim("load16u", y0)), (x0 + 2).load8().eq((y0 + 2).load8())))
    xend = b.let("xend", x0 + (n - 4))
    yend = b.let("yend", y0 + (n - 4))
    x, y = b.var("px", x0), b.var("py", y0)
    with b.while_(x < xend):
        with b.if_(prim("load32u", x).ne(prim("load32u", y))):
            b.ret(False)
        b.set(x, x + 4)
        b.set(y, y + 4)
    b.ret(prim("load32u", xend).eq(prim("load32u", yend)))
    return b.build()


# ---- verification ---------------------------------------------------------
def verify64(fat: bool) -> Function:
    """Teddy::verify64 + verify_bucket: candidate bits -> first verified match."""
    shift, bmask = (4, 15) if fat else (3, 7)
    b = Builder(f"nvol_teddy_verify64_{'fat' if fat else 'slim'}", USIZE,
                doc="verify one 64-bit candidate chunk")
    state, blob = b.param("state", PTR), b.param("blob", PTR)
    cur, hstart, hend = b.param("cur", PTR), b.param("hstart", PTR), b.param("hend", PTR)
    chunk = b.param("chunk", U64)
    c = b.var("c", chunk)
    with b.while_(c.ne(0)):
        bit = b.let("bit", prim("first_offset64", c))
        b.set(c, c & (c - 1))
        at = b.let("at", cur + (bit >> shift))
        bucket = b.let("bucket", bit & bmask)
        cnt = b.let("cnt", (state + OFF_COUNTS + bucket).load8().cast(USIZE))
        j = b.var("j", 0, USIZE)
        with b.while_(j < cnt):
            pid = b.let("pid", (state + OFF_LISTS + (bucket << 6) + j).load8().cast(USIZE))
            pp, pl = _pat(blob, pid)
            pptr = b.let("pptr", pp)
            plen = b.let("plen", pl)
            with b.if_(_is_prefix(b, pptr, plen, at, hend)):
                b.ret(((at - hstart) << 8) | pid)
            b.set(j, j + 1)
    b.ret(NONE)
    return b.build()


# ---- candidate search -----------------------------------------------------
def _masks(b, state, m):
    lo = [b.let("lo", prim("load_u", state + OFF_MASKS + 64 * i)) for i in range(m)]
    hi = [b.let("hi", prim("load_u", state + OFF_MASKS + 64 * i + 32)) for i in range(m)]
    return lo, hi


def search(isa: str, m: int, fat: bool) -> Function:
    """generic::Slim<V, m>::find / generic::Fat<V, m>::find."""
    V = ISAS[isa].bytes
    step = 16 if fat else V
    kind = "fat" if fat else "slim"
    b = Builder(f"nvol_teddy_{kind}_{isa}_{m}", USIZE, isa=isa, noinline=True,
                doc=f"{kind} Teddy, {m}-byte masks; requires hend - hstart >= {step + m - 1}")
    state, blob, hstart, hend = (b.param("state", PTR), b.param("blob", PTR),
                                 b.param("hstart", PTR), b.param("hend", PTR))
    lo, hi = _masks(b, state, m)
    lomask = b.let("lomask", prim("splat", lift(0x0F, U8)))
    ones = b.let("ones", prim("splat", lift(0xFF, U8)))
    prev = [b.var("prev", ones) for _ in range(m - 1)]
    cur = b.var("cur", hstart + (m - 1))
    shin = "hshin" if fat else "shin"
    vfy = f"nvol_teddy_verify64_{kind}"

    def find_one(at):
        chunk = b.let("chunk", prim("load_half_bcast" if fat else "load_u", at))
        hlo = b.let("hlo", prim("vand", chunk, lomask))
        hhi = b.let("hhi", prim("vnib_hi", chunk))
        res = [b.let("res", prim("vand", prim("vshuffle", lo[i], hlo), prim("vshuffle", hi[i], hhi)))
               for i in range(m)]
        c = res[m - 1]
        for k in range(m - 1):
            c = prim("vand", prim(f"{shin}{m - 1 - k}", res[k], prev[k]), c)
        cand = b.let("cand", c)
        for k in range(m - 1):
            b.set(prev[k], res[k])
        with b.if_(prim("is_zero", cand).eq(False)):
            base = b.let("base", at - (m - 1))
            if fat:
                sw = b.let("sw", prim("swap_halves", cand))
                r1 = b.let("r1", prim("unpacklo8", cand, sw))
                r2 = b.let("r2", prim("unpackhi8", cand, sw))
                lanes = [(r1, 0), (r1, 1), (r2, 0), (r2, 1)]
                for j, (v, k) in enumerate(lanes):
                    r = b.let("r", Call(vfy, [state, blob, base + 4 * j if j else base, hstart, hend,
                                              prim(f"lane64_{k}", v)], USIZE))
                    with b.if_(r.ne(NONE)):
                        b.ret(r)
            else:
                for k in range(V // 8):
                    r = b.let("r", Call(vfy, [state, blob, base + 8 * k if k else base, hstart, hend,
                                              prim(f"lane64_{k}", cand)], USIZE))
                    with b.if_(r.ne(NONE)):
                        b.ret(r)

    with b.while_(cur <= hend - step):
        find_one(cur)
        b.set(cur, cur + step)
    with b.if_(cur < hend):
        b.set(cur, hend - step)
        for p in prev:
            b.set(p, ones)
        find_one(cur)
    b.ret(NONE)
    return b.build()


def _rk_hash(b, at, hl):
    """aho-corasick's Rabin-Karp hash of hl bytes: h = h * 2 + byte, wrapping."""
    h = b.var("h", lift(0, U64))
    j = b.var("j", 0, USIZE)
    with b.while_(j < hl):
        b.set(h, (h << 1) + (at + j).load8().cast(U64))
        b.set(j, j + 1)
    return h


def slow() -> Function:
    """Short haystacks (< 16 + m - 1 bytes): aho-corasick 1.1.5's packed Rabin-Karp
    (`packed/rabinkarp.rs`). The hash covers the first minlen bytes, so every pattern that
    matches at a position is in that position's bucket; a bucket is a mask of pattern ids,
    scanned from the lowest (insertion order), so the first prefix found is the
    leftmost-first match. A bucket that holds the hash but not the pattern only costs a
    failed comparison."""
    b = Builder("nvol_teddy_slow", USIZE, noinline=True, doc="leftmost-first by Rabin-Karp")
    state, blob, hstart, hend = (b.param("state", PTR), b.param("blob", PTR),
                                 b.param("hstart", PTR), b.param("hend", PTR))
    hl = b.let("hl", _w(state, W_MINLEN_PAT))
    with b.if_((hend - hstart) < hl):
        b.ret(NONE)
    h = _rk_hash(b, hstart, hl)
    top = b.let("top", (hl - 1).cast(U64))            # 2^(hl - 1): the oldest byte's weight
    at = b.var("at", hstart)
    with b.while_(hl <= (hend - at)):
        bm = b.var("bm", prim("load64", state + OFF_RK + ((h & (RK_BUCKETS - 1)).cast(USIZE) << 3)))
        with b.while_(bm.ne(0)):
            pid = b.let("pid", prim("first_offset64", bm))
            pp, pl = _pat(blob, pid)
            pptr = b.let("pptr", pp)
            plen = b.let("plen", pl)
            same = prim("load64", state + OFF_RKH + (pid << 3)).eq(h)
            with b.if_(land(same, _is_prefix(b, pptr, plen, at, hend))):
                b.ret(((at - hstart) << 8) | pid)
            b.set(bm, bm & (bm - 1))
        with b.if_(hl < (hend - at)):
            b.set(h, ((h - (at.load8().cast(U64) << top)) << 1) + (at + hl).load8().cast(U64))
        b.set(at, at + 1)
    b.ret(NONE)
    return b.build()


# ---- construction -----------------------------------------------------------
def init(arch: str = "x86_64") -> Function:
    """teddy::Builder::build + Teddy::new + {Slim,Fat}MaskBuilder, in one pass.

    aarch64 follows aho-corasick 1.1.5 exactly: its NEON branch only *logs* the
    per-mask-length pattern limits (no `return None`), so any 1..64 patterns
    build a SlimNeon searcher there."""
    b = Builder("nvol_teddy_init", VOID, public=True,
                doc="Build a Teddy searcher for a patterns blob into out[NVOL_TEDDY_STATE_BYTES]; "
                    "out word 0 (kind) is 0 when Teddy cannot be used (aho-corasick returns None)")
    blob, out = b.param("blob", PTR), b.param("out", MPTR)
    z = b.var("z", 0, USIZE)
    with b.while_(z < STATE_BYTES):
        b.store64(out + z, lift(0, U64))
        b.set(z, z + 8)
    n = b.let("n", prim("load64", blob).cast(USIZE))
    with b.if_(lor(n.eq(0), n > MAX_PATTERNS)):
        b.ret()
    minlen = b.var("minlen", NONE)
    i = b.var("i", 0, USIZE)
    with b.while_(i < n):
        _, pl = _pat(blob, i)
        ln = b.let("ln", pl)
        with b.if_(ln < minlen):
            b.set(minlen, ln)
        b.set(i, i + 1)
    with b.if_(minlen.eq(0)):
        b.ret()
    # Rabin-Karp buckets for the short-haystack path (`slow`)
    rk = b.var("rk", 0, USIZE)
    with b.while_(rk < n):
        rp, _ = _pat(blob, rk)
        rh = _rk_hash(b, b.let("rp", rp), minlen)
        slot = b.let("slot", out + OFF_RK + ((rh & (RK_BUCKETS - 1)).cast(USIZE) << 3))
        b.store64(slot, prim("load64", slot) | (lift(1, U64) << rk.cast(U64)))
        b.store64(out + OFF_RKH + (rk << 3), rh)
        b.set(rk, rk + 1)
    m = b.let("m", min_(minlen, 4))
    if arch == "aarch64":
        avx2 = b.let("avx2", lift(False, BOOL))
    else:
        avx2 = b.let("avx2", prim("cpu_avx2"))
        ssse3 = b.let("ssse3", lor(avx2, prim("cpu_ssse3")))
        with b.if_(ssse3.eq(False)):
            b.ret()
        with b.if_(land(m.eq(1), n > 16)):
            b.ret()
    fat = b.let("fat", land(avx2, n > 32))
    nb = b.var("nbuckets", 8, USIZE)
    with b.if_(fat):
        b.set(nb, 16)
    # buckets: patterns with identical low nybbles (first m bytes) share one
    pid = b.var("pid", 0, USIZE)
    with b.while_(pid < n):
        pp, _ = _pat(blob, pid)
        pi = b.let("pi", pp)
        bucket = b.var("bucket", 0xFF, USIZE)
        j = b.var("j", 0, USIZE)
        with b.while_(land(j < pid, bucket.eq(0xFF))):
            qp, _ = _pat(blob, j)
            pj = b.let("pj", qp)
            same = b.var("same", True, BOOL)
            k = b.var("k", 0, USIZE)
            with b.while_(land(same, k < m)):
                with b.if_(((pi + k).load8() & 0x0F).ne((pj + k).load8() & 0x0F)):
                    b.set(same, False)
                b.set(k, k + 1)
            with b.if_(same):
                b.set(bucket, (out + OFF_BUCKET + j).load8().cast(USIZE))
            b.set(j, j + 1)
        with b.if_(bucket.eq(0xFF)):
            b.set(bucket, (nb - 1) - (pid & (nb - 1)))
        b.store8(out + OFF_BUCKET + pid, bucket.cast(U8))
        cnt = b.let("cnt", (out + OFF_COUNTS + bucket).load8())
        b.store8(out + OFF_LISTS + (bucket << 6) + cnt.cast(USIZE), pid.cast(U8))
        b.store8(out + OFF_COUNTS + bucket, cnt + 1)
        # masks
        k2 = b.var("mi", 0, USIZE)
        with b.while_(k2 < m):
            byte = b.let("byte", (pi + k2).load8())
            lon = b.let("lon", (byte & 0x0F).cast(USIZE))
            hin = b.let("hin", (byte >> 4).cast(USIZE))
            base = b.let("mbase", out + OFF_MASKS + (k2 << 6))
            bit = b.let("bit", lift(1, U8) << (bucket & 7).cast(U8))

            def orin(p):
                b.store8(p, p.load8() | bit)
            with b.if_(fat):
                with b.if_(bucket < 8):
                    orin(base + lon)
                    orin(base + 32 + hin)
                with b.else_():
                    orin(base + 16 + lon)
                    orin(base + 48 + hin)
            with b.else_():
                orin(base + lon)
                orin(base + 16 + lon)
                orin(base + 32 + hin)
                orin(base + 48 + hin)
            b.set(k2, k2 + 1)
        b.set(pid, pid + 1)
    kind = b.var("kind", KIND_SLIM128, USIZE)
    with b.if_(avx2):
        b.set(kind, KIND_SLIM256)
        with b.if_(fat):
            b.set(kind, KIND_FAT256)
    b.store64(out, kind.cast(U64))
    b.store64(out + 8 * W_MASK_LEN, m.cast(U64))
    b.store64(out + 8 * W_NPAT, n.cast(U64))
    b.store64(out + 8 * W_MINLEN_SEARCH, (m + 15).cast(U64))
    b.store64(out + 8 * W_MINLEN_PAT, minlen.cast(U64))
    # variant selector (one indirect call per search); SlimAVX2 switches to its
    # 128-bit searcher below 32 + m - 1 bytes
    sel = b.var("sel", m - 1)
    small = b.var("sel_small", m - 1)
    switch = b.var("switch", 0, USIZE)
    with b.if_(kind.eq(KIND_SLIM256)):
        b.set(sel, m + 3)
        b.set(switch, m + 31)
    with b.if_(kind.eq(KIND_FAT256)):
        b.set(sel, m + 7)
        b.set(small, m + 7)
    b.store64(out + 8 * W_SEL, sel.cast(U64))
    b.store64(out + 8 * W_SEL_SMALL, small.cast(U64))
    b.store64(out + 8 * W_SWITCH, switch.cast(U64))
    return b.build()


# ---- dispatch -------------------------------------------------------------
def find_raw() -> Function:
    """One indirect call per search through the `teddy` function table, using
    the variant chosen at build time -- like aho-corasick's `dyn SearcherT`
    call into SlimAVX2<m> / FatAVX2<m> / SlimSSSE3<m>."""
    b = Builder("nvol_teddy_find_raw", USIZE, public=True,
                doc="Leftmost-first match in [hstart, hend): (start - hstart) << 8 | pattern id, "
                    "or SIZE_MAX. state from nvol_teddy_init (kind != 0), blob = patterns.")
    state, blob, hstart, hend = (b.param("state", PTR), b.param("blob", PTR),
                                 b.param("hstart", PTR), b.param("hend", PTR))
    args = [state, blob, hstart, hend]
    with b.if_(_w(state, W_KIND).eq(KIND_NONE)):
        b.ret(NONE)
    hlen = b.let("hlen", hend - hstart)
    with b.if_(hlen < _w(state, W_MINLEN_SEARCH)):
        b.ret(Call("nvol_teddy_slow", args, USIZE))
    sel = b.var("sel", _w(state, W_SEL))
    with b.if_(hlen < _w(state, W_SWITCH)):
        b.set(sel, _w(state, W_SEL_SMALL))
    b.ret(CallTable("teddy", sel, args, USIZE))
    return b.build()


def build(isas: list, mod: Module) -> None:
    """x86_64: SlimSSSE3 / SlimAVX2 / FatAVX2. aarch64: SlimNeon."""
    if any(i.name == "neon" for i in isas):
        mod.functions += [is_equal_ac(), verify64(False), slow(), init("aarch64")]
        for m in (1, 2, 3, 4):
            mod.functions.append(search("neon", m, fat=False))
        mod.fn_tables["teddy"] = [f"nvol_teddy_slim_neon_{m}" for m in (1, 2, 3, 4)]
        mod.functions.append(find_raw())
        mod.api.append(("teddy", "nvol_teddy_find_raw", 0))
        return
    mod.functions += [is_equal_ac(), verify64(False), verify64(True), slow(), init()]
    for m in (1, 2, 3, 4):
        mod.functions.append(search("ssse3", m, fat=False))
    for m in (1, 2, 3, 4):
        mod.functions += [search("avx2", m, fat=False), search("avx2", m, fat=True)]
    mod.fn_tables["teddy"] = [f"nvol_teddy_{k}_{i}_{m}" for k, i in VARIANTS for m in (1, 2, 3, 4)]
    mod.functions.append(find_raw())
    mod.api.append(("teddy", "nvol_teddy_find_raw", 0))
