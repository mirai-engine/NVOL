"""simdjson's string copy (PENDING 37): backslash_and_quote::copy_and_find, the inner step of
its stage-2 parse_string. Each step copies 32 bytes of the string to the output and finds
the backslashes and quotes in them; the caller (simdjson's generic code) then compares the
two masks to know whether the string ends, an escape comes first, or neither.

nvol_json_copy_and_find(src, dst) copies src[0..32) to dst[0..32) and returns the mask of '\\'
(bit i = byte i, bits 0-31) and of '"' (bits 32-63). Both 32-byte ranges must be valid: like
simdjson, the copy is unconditional, and the caller writes past it later.

- AVX2 (haswell): one 32-byte load and store, two vpcmpeqb, the two masks with bmask64x2;
- SSE2 (westmere) and NEON (arm64): two 16-byte loads and stores, four compares and bmask64x4,
  as simdjson's westmere and arm64 kernels pack them (simd8x64<bool>(...).to_bitmask()).

simdjson's fallback copies one byte per step; a 32-byte scalar step has no reference to match,
so x86_64 has no scalar kernel (SSE2 is its baseline)."""
from __future__ import annotations
from ..ir import MPTR, PTR, U8, U64, Builder, Function, Module, lift, prim
from ..isa import ISA, ISAS

DOC = ("copies src[0..32) to dst[0..32) and returns the mask of '\\\\' (bits 0-31) and of '\"' "
       "(bits 32-63), bit i for byte i; reads and writes exactly 32 bytes")


def kernel(isa: ISA) -> Function:
    b = Builder(f"nvol_json_copy_and_find_{isa.name}", U64, isa=isa.name, doc=DOC)
    src, dst = b.param("src", PTR), b.param("dst", MPTR)
    bs = b.let("bs", prim("splat", lift(ord("\\"), U8)))
    q = b.let("q", prim("splat", lift(ord('"'), U8)))
    V = isa.bytes
    vs = [b.let(f"v{k}", prim("load_u", src + V * k if k else src)) for k in range(32 // V)]
    for k, v in enumerate(vs):
        b.store_vec(dst + V * k if k else dst, v)
    if V == 32:
        b.ret(prim("bmask64x2", prim("cmpeq", vs[0], bs), prim("cmpeq", vs[0], q)))
    else:
        b.ret(prim("bmask64x4", prim("cmpeq", vs[0], bs), prim("cmpeq", vs[1], bs),
                   prim("cmpeq", vs[0], q), prim("cmpeq", vs[1], q)))
    return b.build()


def build(isas: list, mod: Module) -> None:
    """AVX2, then SSE2 on x86_64; NEON on aarch64."""
    if "neon" in [i.name for i in isas]:
        neon = kernel(ISAS["neon"])
        mod.functions.append(neon)
        entries = [(None, neon.name)]
    else:
        sse2, avx2 = kernel(ISAS["sse2"]), kernel(ISAS["avx2"])
        mod.functions += [sse2, avx2]
        entries = [(prim("cpu_avx2"), avx2.name), (None, sse2.name)]
    rb = Builder("nvol_json_copy_and_find", U64, public=True, doc=DOC)
    rb.param("src", PTR)
    rb.param("dst", MPTR)
    f = rb.build()
    f.ifunc = entries
    mod.functions.append(f)
