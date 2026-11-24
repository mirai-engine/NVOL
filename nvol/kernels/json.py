"""simdjson stage 1: the structural positions of 64-byte blocks (PENDING 35).

A port of the loop of simdjson 5.2.0's `json_structural_indexer::index` (src/generic/stage1):
`json_escape_scanner`, `json_string_scanner`, `json_character_block::classify` (haswell's two
nibble tables), `json_scanner` and the bit indexer. One call runs the whole loop, with the
scanners inlined (docs/SIMDJSON_SIMD_INVENTORY.md, "The stage 1 API").

    nvol_json_index_raw(state, blocks, nblocks, idx, out) -> count

- `blocks`: nblocks * 64 readable bytes; simdjson's buf_block_reader pads the last block.
- `state`: STATE_BYTES, zero before the first block of a document, carried between calls:
  word 0 next_is_escaped (0/1), word 1 prev_in_string (0 or all ones), word 2 prev_scalar
  (0/1), word 3 the OR of the control bytes (<= 0x1F) found inside strings (non-zero: the
  document is invalid, simdjson's UNESCAPED_CHARS), word 4 non-zero once the bytes are not
  valid UTF-8 (simdjson's UTF8_ERROR), word 5 non-zero when they end inside a code point (at
  the end of the document that is an error too: simdjson's check_eof), bytes 48..79 the last
  bytes of the previous block (UTF-8's prev<1..3>; only the last 3 are read).
- `idx`: the document position of blocks[0], with idx + 64 * nblocks <= 2^32.
- `out`: room for 64 * nblocks u32; gets the structural positions in order, `count` of them,
  and the entries past the count may be overwritten (the indexer writes in steps of 4).

UTF-8 is validated in the same pass, as simdjson's utf8_checker (nvol/kernels/utf8.py).
"""
from __future__ import annotations

from ..ir import MPTR, PTR, U8, U32, U64, USIZE, Builder, Function, Module, Not, lift, lor, prim
from ..isa import ISA, ISAS, prefix_xor
from . import utf8

STATE_WORDS = 6
PREV_AT, PREV_BYTES = 8 * STATE_WORDS, 32
STATE_BYTES = PREV_AT + PREV_BYTES
# exported by every backend as NVOL_JSON_<name> (C, C++, Rust) or JSON_<name> (Odin, Python)
CONSTANTS = [("STATE_BYTES", STATE_BYTES, "bytes of the nvol_json_index_raw state")]
M64 = (1 << 64) - 1
ODD_BITS = 0xAAAA_AAAA_AAAA_AAAA
# classify's tables (haswell): entry lo has low nibble lo for the members, so a byte matches its
# own entry only if it is that member; the other entries can never match
WS_TABLE = (0x20, 100, 100, 100, 17, 100, 113, 2, 100, 0x09, 0x0A, 112, 100, 0x0D, 100, 100)
# compared with byte | 0x20: ':' 3A, '{' 7B (and '[' 5B), ',' 2C, '}' 7D (and ']' 5D)
OP_TABLE = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x3A, 0x7B, 0x2C, 0x7D, 0, 0)

DOC = ("simdjson stage 1 over nblocks 64-byte blocks: writes the structural positions (idx + "
       "offset, as u32) to out and returns how many (out holds 64 * nblocks u32; the entries past "
       "the count may be overwritten); state (NVOL_JSON_STATE_BYTES, zero at the "
       "start of a document) carries the escape, string and scalar state between calls, and "
       "word 3 is non-zero once a control byte was found inside a string.")


def _not(x):
    return x ^ M64


def _params(b):
    return (b.param("state", MPTR), b.param("blocks", PTR), b.param("nblocks", USIZE),
            b.param("idx", USIZE), b.param("out", MPTR))


def _masks_scalar(b, blk):
    """The block's masks byte by byte: backslash, quote, whitespace, operator, control."""
    ms = [b.var(n, 0, U64) for n in ("bs", "qt", "ws", "op", "ctl")]
    bs, qt, ws, op, ctl = ms
    i = b.var("i", 0)
    with b.while_(i < 64):
        c = b.let("c", (blk + i).load8())
        bit = b.let("bit", lift(1, U64) << i.cast(U64))
        with b.if_(c.eq(0x5C)):
            b.set(bs, bs | bit)
        with b.if_(c.eq(0x22)):
            b.set(qt, qt | bit)
        with b.if_(lor(lor(c.eq(0x20), c.eq(0x09)), lor(c.eq(0x0A), c.eq(0x0D)))):
            b.set(ws, ws | bit)
        c20 = b.let("c20", c | 0x20)
        with b.if_(lor(lor(c20.eq(0x3A), c20.eq(0x7B)), lor(c20.eq(0x2C), c20.eq(0x7D)))):
            b.set(op, op | bit)
        with b.if_(c <= 0x1F):
            b.set(ctl, ctl | bit)
        b.set(i, i + 1)
    return ms


def _nibble(b, table, x):
    """table[x] for a 16-entry byte table and x in 0..15, from two u64 constants."""
    lo = sum(v << (8 * i) for i, v in enumerate(table[:8]))
    hi = sum(v << (8 * i) for i, v in enumerate(table[8:]))
    w = b.var("tw", lo, U64)
    with b.if_(x > 7):
        b.set(w, hi)
    return (w >> ((x & 7) << 3)) & 0xFF


def _sat_sub(b, x, k):
    r = b.var("ss", 0, U64)
    with b.if_(x > k):
        b.set(r, x - k)
    return r


def _utf8_scalar(b, blk, u):
    """utf8_checker on one block, one byte at a time: the same lookups and terms as the vector
    check (so the same error bits), with prev1..3 the three bytes before."""
    p1, p2, p3 = u["p1"], u["p2"], u["p3"]
    i = b.var("ui", 0)
    with b.while_(i < 64):
        c = b.let("uc", (blk + i).load8().cast(U64))
        sc = b.let("usc", _nibble(b, utf8.T1H, p1 >> 4) & _nibble(b, utf8.T1L, p1 & 15)
                   & _nibble(b, utf8.T2H, c >> 4))
        m23 = b.let("m23", _sat_sub(b, p2, 0xE0 - 0x80) | _sat_sub(b, p3, 0xF0 - 0x80))
        b.set(u["err"], u["err"] | ((m23 & 0x80) ^ sc))
        b.set(p3, p2)
        b.set(p2, p1)
        b.set(p1, c)
        b.set(i, i + 1)
    b.set(u["inc"], utf8.incomplete(blk).cast(U64))


def _utf8_vector(b, isa, k, blk, vs, u):
    """utf8_checker::check_next_input on one block's vectors."""
    with b.if_(utf8.ascii_block(isa, vs), likely=True):
        b.set(u["verr"], prim("vor", u["verr"], u["vinc"]))
    with b.else_():
        for j, v in enumerate(vs):
            utf8.check_vector(b, isa, k, v, u["prev"] if j == 0 else vs[j - 1], u["verr"])
        b.set(u["vinc"], utf8.incomplete_vector(k, vs[-1]))
    b.set(u["prev"], vs[-1])


def _consts(b, isa):
    """The splats and tables of the vector masks, hoisted out of the block loop."""
    def splat(name, x):
        return b.let(name, prim("splat", lift(x, U8)))
    return {"ws_table": b.let("ws_table", prim("vtable16", *[lift(x, U8) for x in WS_TABLE])),
            "op_table": b.let("op_table", prim("vtable16", *[lift(x, U8) for x in OP_TABLE])),
            "bs": splat("k_bs", 0x5C), "qt": splat("k_qt", 0x22), "k20": splat("k20", 0x20),
            "k1f": splat("k1f", 0x1F), "k0f": splat("k0f", 0x0F) if isa.name == "neon" else None}


def _masks_vector(b, isa, blk, k):
    """The block's masks from 64 / V vectors: two cmpeq, classify's two lookups (haswell:
    whitespace = table[lo] == byte, operator = table[lo] == byte | 0x20), and byte <= 0x1F as
    min(byte, 0x1F) == byte."""
    V = isa.bytes
    vs = [b.let("in", prim("load_u", blk + V * j if j else blk)) for j in range(64 // V)]
    # pshufb takes the low nibble of an index below 0x80 (and gives 0 from 0x80 up); NEON's tbl
    # gives 0 from 16 up, so it is indexed by the low nibble. Either way a byte only matches
    # its own entry: the tables hold bytes below 0x80 whose low nibble is their index.
    idx = [b.let("lo", prim("vand", v, k["k0f"])) for v in vs] if k["k0f"] is not None else vs
    bm = "bmask64x2" if V == 32 else "bmask64x4"

    def mask(name, f):
        return b.let(name, prim(bm, *[f(v, i) for v, i in zip(vs, idx)]))
    return vs, [mask("bs", lambda v, i: prim("cmpeq", v, k["bs"])),
            mask("qt", lambda v, i: prim("cmpeq", v, k["qt"])),
            mask("ws", lambda v, i: prim("cmpeq", prim("vshuffle", k["ws_table"], i), v)),
            mask("op", lambda v, i: prim("cmpeq", prim("vshuffle", k["op_table"], i),
                                         prim("vor", v, k["k20"]))),
            mask("ctl", lambda v, i: prim("cmpeq", prim("vmin8", v, k["k1f"]), v))]


def _block(b, isa, st, masks):
    """One block of json_scanner::next on the five masks: its structural starts."""
    nie, pis, psc, err = st
    bs, qt, ws, op, ctl = masks
    # json_escape_scanner::next, with its shortcut: a block without a backslash escapes only its
    # first byte (when the last one ended in an odd run of backslashes) and carries nothing
    escaped = b.var("escaped", nie, U64)
    with b.if_(bs.ne(0), likely=False):
        pe = b.let("pe", bs & _not(nie))
        code = b.let("code", ((pe << 1) | ODD_BITS).wsub(pe) ^ ODD_BITS)
        b.set(escaped, code ^ (bs | nie))
        b.set(nie, (code & bs) >> 63)
    with b.else_():
        b.set(nie, 0)
    # json_string_scanner::next
    quote = b.let("quote", qt & _not(escaped))
    ins = b.let("in_string", prefix_xor(b, isa, quote) ^ pis)
    b.set(pis, lift(0, U64).wsub(ins >> 63))
    # json_scanner::next and json_block::structural_start
    scalar = b.let("scalar", _not(op | ws))
    nqs = b.let("nqs", scalar & _not(quote))
    follows = b.let("follows", (nqs << 1) | psc)
    b.set(psc, nqs >> 63)
    bits = b.let("bits", (op | (scalar & _not(follows))) & _not(ins ^ quote))
    b.set(err, err | (ctl & ins))
    return bits


# bit_indexer::write's unrolling (haswell): 4 positions unconditionally, then 4 more while the
# count goes past them, up to 24, then one at a time
STEP, STEP_UNTIL = 4, 24


def _write_indexes(b, bits, base, out, n):
    """One u32 per set bit, lowest first, as simdjson's bit_indexer::write: the slots past the
    count get base + 64 (tzcnt of 0; out holds 64 per block, so they stay inside it) and n moves
    by the count."""
    with b.if_(bits.ne(0)):
        cnt = b.let("cnt", prim("popcnt64", bits))
        o = b.let("o", out + (n << 2))

        def write(i):
            b.store32(o + 4 * i if i else o, (base + prim("tzcnt64", bits)).cast(U32))
            b.set(bits, bits & bits.wsub(1))

        def stepped(start):
            for i in range(start, start + STEP):
                write(i)
            if start + STEP < STEP_UNTIL:
                with b.if_(cnt > start + STEP):
                    stepped(start + STEP)
            else:
                with b.if_(cnt > STEP_UNTIL):
                    i = b.var("i", STEP_UNTIL, USIZE)
                    with b.while_(i < cnt):
                        b.store32(o + (i << 2), (base + prim("first_offset64", bits)).cast(U32))
                        b.set(bits, bits & bits.wsub(1))
                        b.set(i, i + 1)
        stepped(0)
        b.set(n, n + cnt)


def kernel(isa: ISA | None) -> Function:
    tag = isa.name if isa else "scalar"
    # AVX2 with BMI1, as simdjson's haswell kernel: tzcnt and blsr in the indexer
    b = Builder(f"nvol_json_index_{tag}", USIZE, isa=isa.name if isa else None,
                doc=f"stage 1, {tag} masks", features=("bmi1",) if tag == "avx2" else ())
    state, blocks, nblocks, idx, out = _params(b)
    st = [b.var(n, prim("load64", state + 8 * k if k else state), U64)
          for k, n in enumerate(("nie", "pis", "psc", "err"))]
    blk = b.var("blk", blocks)
    base = b.var("base", idx)
    end = b.let("end", blocks + (nblocks << 6))
    n = b.var("n", 0)
    k = _consts(b, isa) if isa else None
    u = {"err": b.var("uerr", prim("load64", state + 32), U64),
         "inc": b.var("uinc", prim("load64", state + 40), U64)}
    if isa:
        ku = utf8._consts(b, isa)
        prev = b.var("uprev", prim("load_u", state + (STATE_BYTES - isa.bytes)))
        # "incomplete" from the bytes the last call left (the scalar kernel stores 3 of them,
        # the only ones the bound looks at), so both kernels carry the same state
        u.update(prev=prev, verr=b.var("verr", prim("vzero")),
                 vinc=b.var("vinc", utf8.incomplete_vector(ku, prev)))
    else:
        u.update({f"p{j}": b.var(f"p{j}", (state + (STATE_BYTES - j)).load8().cast(U64), U64)
                  for j in (1, 2, 3)})
    # json_structural_indexer::next: a block's positions are written while the next block is
    # scanned, so the indexer's branches wait on the previous block, not on this one's chain
    prev = b.var("prev", 0, U64)
    pbase = b.var("pbase", idx)

    def scan(at):
        """The block's structural starts, then its UTF-8 check (as simdjson's
        json_structural_indexer::next)."""
        if isa:
            vs, ms = _masks_vector(b, isa, at, k)
            bits = b.var("sbits", _block(b, isa, st, ms), U64)
            _utf8_vector(b, isa, ku, at, vs, u)
        else:
            bits = b.var("sbits", _block(b, isa, st, _masks_scalar(b, at)), U64)
            _utf8_scalar(b, at, u)
        return bits
    # stage1_mode's step<128>: two blocks per iteration, both scanned before either is written
    end2 = b.let("end2", blocks + ((nblocks >> 1) << 7))
    with b.while_(blk < end2):
        # each block's masks right before its scan, as simdjson's json_scanner::next: the
        # masks of both blocks at once outnumber the registers (GCC spilled 6 compares)
        bits1 = scan(blk)
        bits2 = scan(blk + 64)
        _write_indexes(b, prev, pbase, out, n)
        _write_indexes(b, bits1, base, out, n)
        b.set(prev, bits2)
        b.set(pbase, base + 64)
        b.set(blk, blk + 128)
        b.set(base, base + 128)
    with b.if_(blk < end):              # an odd last block
        bits = scan(blk)
        _write_indexes(b, prev, pbase, out, n)
        b.set(prev, bits)
        b.set(pbase, base)
    _write_indexes(b, prev, pbase, out, n)
    for k, v in enumerate(st):
        b.store64(state + 8 * k if k else state, v)
    if isa:
        b.store64(state + 32, u["err"] | Not(prim("is_zero", u["verr"])).cast(U64))
        b.store64(state + 40, Not(prim("is_zero", u["vinc"])).cast(U64))
        b.store_vec(state + (STATE_BYTES - isa.bytes), u["prev"])
    else:
        b.store64(state + 32, u["err"])
        b.store64(state + 40, u["inc"])
        for j in (1, 2, 3):
            b.store8(state + (STATE_BYTES - j), u[f"p{j}"].cast(U8))
    b.ret(n)
    return b.build()


def build(isas: list, mod: Module) -> None:
    """Its own ISA set, as Teddy and byteset: AVX2 (with PCLMULQDQ), then SSE4.2 (simdjson's
    westmere class: POPCNT), then SSSE3 (pshufb), then the scalar masks on x86_64; NEON alone on
    aarch64."""
    if "neon" in [i.name for i in isas]:     # NEON is the baseline: no scalar masks
        neon = kernel(ISAS["neon"])
        mod.functions.append(neon)
        entries = [(None, neon.name)]
    else:
        scalar, avx2, ssse3 = kernel(None), kernel(ISAS["avx2"]), kernel(ISAS["ssse3"])
        sse42 = kernel(ISAS["sse42"])
        mod.functions += [scalar, ssse3, sse42, avx2]
        entries = [(prim("cpu_avx2_pclmul_bmi1"), avx2.name), (prim("cpu_sse42"), sse42.name),
                   (prim("cpu_ssse3"), ssse3.name), (None, scalar.name)]
    rb = Builder("nvol_json_index_raw", USIZE, public=True, doc=DOC)
    _params(rb)
    raw = rb.build()
    raw.ifunc = entries
    mod.functions.append(raw)
