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
  document is invalid, simdjson's UNESCAPED_CHARS).
- `idx`: the document position of blocks[0], with idx + 64 * nblocks <= 2^32.
- `out`: room for 64 * nblocks u32; gets the structural positions in order, exactly `count`
  of them (simdjson writes in steps of 4 past the count; this kernel writes nothing past it).
"""
from __future__ import annotations

from ..ir import MPTR, PTR, U8, U32, U64, USIZE, Builder, Function, Module, lift, lor, prim
from ..isa import ISA, ISAS, prefix_xor

STATE_WORDS = 4
STATE_BYTES = 8 * STATE_WORDS
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
    return [mask("bs", lambda v, i: prim("cmpeq", v, k["bs"])),
            mask("qt", lambda v, i: prim("cmpeq", v, k["qt"])),
            mask("ws", lambda v, i: prim("cmpeq", prim("vshuffle", k["ws_table"], i), v)),
            mask("op", lambda v, i: prim("cmpeq", prim("vshuffle", k["op_table"], i),
                                         prim("vor", v, k["k20"]))),
            mask("ctl", lambda v, i: prim("cmpeq", prim("vmin8", v, k["k1f"]), v))]


def _block(b, isa, st, masks, base, out, n):
    """One block of json_scanner::next plus the indexer, on the five masks."""
    nie, pis, psc, err = st
    bs, qt, ws, op, ctl = masks
    # json_escape_scanner::next (without its no-backslash shortcut, which gives the same masks)
    pe = b.let("pe", bs & _not(nie))
    code = b.let("code", ((pe << 1) | ODD_BITS).wsub(pe) ^ ODD_BITS)
    escaped = b.let("escaped", code ^ (bs | nie))
    b.set(nie, (code & bs) >> 63)
    # json_string_scanner::next
    quote = b.let("quote", qt & _not(escaped))
    ins = b.let("in_string", prefix_xor(b, isa, quote) ^ pis)
    b.set(pis, lift(0, U64).wsub(ins >> 63))
    # json_scanner::next and json_block::structural_start
    scalar = b.let("scalar", _not(op | ws))
    nqs = b.let("nqs", scalar & _not(quote))
    follows = b.let("follows", (nqs << 1) | psc)
    b.set(psc, nqs >> 63)
    bits = b.var("bits", (op | (scalar & _not(follows))) & _not(ins ^ quote), U64)
    b.set(err, err | (ctl & ins))
    _write_indexes(b, bits, base, out, n)


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
    b = Builder(f"nvol_json_index_{tag}", USIZE, isa=isa.name if isa else None,
                doc=f"stage 1, {tag} masks")
    state, blocks, nblocks, idx, out = _params(b)
    st = [b.var(n, prim("load64", state + 8 * k if k else state), U64)
          for k, n in enumerate(("nie", "pis", "psc", "err"))]
    blk = b.var("blk", blocks)
    base = b.var("base", idx)
    end = b.let("end", blocks + (nblocks << 6))
    n = b.var("n", 0)
    k = _consts(b, isa) if isa else None
    with b.while_(blk < end):
        masks = _masks_vector(b, isa, blk, k) if isa else _masks_scalar(b, blk)
        _block(b, isa, st, masks, base, out, n)
        b.set(blk, blk + 64)
        b.set(base, base + 64)
    for k, v in enumerate(st):
        b.store64(state + 8 * k if k else state, v)
    b.ret(n)
    return b.build()


def build(isas: list, mod: Module) -> None:
    """Its own ISA set, as Teddy and byteset: AVX2 (with PCLMULQDQ), then SSSE3 (pshufb), then
    the scalar masks on x86_64; NEON alone on aarch64."""
    if "neon" in [i.name for i in isas]:     # NEON is the baseline: no scalar masks
        neon = kernel(ISAS["neon"])
        mod.functions.append(neon)
        entries = [(None, neon.name)]
    else:
        scalar, avx2, ssse3 = kernel(None), kernel(ISAS["avx2"]), kernel(ISAS["ssse3"])
        mod.functions += [scalar, ssse3, avx2]
        entries = [(prim("cpu_avx2_pclmul"), avx2.name), (prim("cpu_ssse3"), ssse3.name),
                   (None, scalar.name)]
    rb = Builder("nvol_json_index_raw", USIZE, public=True, doc=DOC)
    _params(rb)
    raw = rb.build()
    raw.ifunc = entries
    mod.functions.append(raw)
