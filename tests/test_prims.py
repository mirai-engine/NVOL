"""Per-primitive lowering tests (PENDING 2.3): each vector primitive's C lowering
against its reference semantics in nvol.primitives, on random vectors.

For every (isa, prim) a tiny IR function loads its operands from `in`, applies
the primitive and stores the result to `out`; it runs both in the interpreter
and as compiled C (via ctypes)."""
import ctypes
import os
import random
import subprocess
import sys

import pytest

from conftest import _cc
from nvol import ir
from nvol.backends import c as c_backend
from nvol.interp import Interpreter, Memory
from nvol.isa import ISAS, prefix_xor
from nvol.primitives import PRIMS

VEC_TO_VEC = {
    "ssse3": ["cmpeq", "vor", "mask_or", "vand", "vxor", "vandnot", "vsub8", "vsubs8", "vmin8", "vshr4", "vshuffle", "shin1", "shin2", "shin3"],
    "avx2": ["cmpeq", "vor", "mask_or", "vand", "vxor", "vandnot", "vsub8", "vsubs8", "vmin8", "vshr4", "vshuffle", "shin1", "shin2", "shin3",
             "hshin1", "hshin2", "hshin3", "swap_halves", "unpacklo8", "unpackhi8", "sad0", "vadd64"],
}
VEC_TO_SCALAR = {
    "ssse3": ["is_zero", "any_nonzero", "movemask", "lane64_0", "lane64_1"],
    "avx2": ["is_zero", "any_nonzero", "movemask", "lane64_0", "lane64_1", "lane64_2", "lane64_3", "hsum64"],
}
VEC2_TO_SCALAR = {"sse42": ["skip_set16"]}
BLOCK = {"ssse3": ["bmask64x4"], "avx2": ["bmask64x2"]}
# u64 -> usize, inside an ISA function: the assembly picks popcnt by the ISA (SSE4.2 and AVX2
# have it; below, a byte table), which the ISA-less fuzzer never reaches
U64_TO_SCALAR = {"ssse3": ["popcnt64", "tzcnt64"], "avx2": ["popcnt64", "prefix_xor64", "tzcnt64"]}
# primitives whose all-zero input is a case of its own
ZERO_CASE = {"is_zero", "tzcnt64"}
# statements, not primitives: t_<isa>_store32 (a register and an immediate, at odd offsets)
STORES = {"ssse3": ["store32"], "avx2": []}
# IR helpers of nvol.isa, not primitives: t_<isa>_prefix_xor (the shift/xor steps without PCLMUL)
HELPERS = {"ssse3": ["prefix_xor"], "avx2": []}
# constant vectors: t_<isa>_vtable16 (the bytes of TABLE16, which ignore src)
CONSTS = {"ssse3": ["vtable16"], "avx2": ["vtable16"]}
TABLE16 = (0x20, 0x00, 0x7F, 0x80, 0xFF, 0x11, 0x71, 0x02, 0x64, 0x09, 0x0A, 0x70, 0xC3, 0x0D, 0x01, 0xFE)


def _fn_vec(isa, name):
    V = ISAS[isa].bytes
    b = ir.Builder(f"t_{isa}_{name}", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    p = PRIMS[name]
    a = b.let("a", ir.prim("load_u", src))
    bb = b.let("b", ir.prim("load_u", src + V))
    r = b.let("r", ir.prim(name, *([a, bb][:len(p.params)])))
    for k in range(V // 8):
        b.store64(out + 8 * k if k else out, ir.prim(f"lane64_{k}", r))
    return b.build()


def _fn_scalar(isa, name):
    V = ISAS[isa].bytes
    b = ir.Builder(f"t_{isa}_{name}", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    a = b.let("a", ir.prim("load_u", src))
    v = ir.prim(name, a)
    if v.ty is ir.BOOL:
        with b.if_(v):
            b.store64(out, 1)
        with b.else_():
            b.store64(out, 0)
    else:
        b.store64(out, v.cast(ir.U64))
    return b.build()


def _fn_scalar2(isa, name):
    V = ISAS[isa].bytes
    b = ir.Builder(f"t_{isa}_{name}", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    a = b.let("a", ir.prim("load_u", src))
    bb = b.let("b", ir.prim("load_u", src + V))
    b.store64(out, ir.prim(name, a, bb).cast(ir.U64))
    return b.build()


def _fn_block(isa, name):
    """A 64-byte block mask: 64 / V vectors loaded from src, the u64 stored to out."""
    V = ISAS[isa].bytes
    b = ir.Builder(f"t_{isa}_{name}", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    vs = [b.let(f"v{k}", ir.prim("load_u", src + V * k if k else src)) for k in range(64 // V)]
    b.store64(out, ir.prim(name, *vs))
    return b.build()


def _fn_u64(isa, name):
    b = ir.Builder(f"t_{isa}_{name}", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    b.store64(out, ir.prim(name, ir.prim("load64", src)).cast(ir.U64))
    return b.build()


def _fn_vtable16(isa):
    V = ISAS[isa].bytes
    b = ir.Builder(f"t_{isa}_vtable16", ir.VOID, isa=isa, public=True)
    b.param("src", ir.PTR)
    out = b.param("out", ir.MPTR)
    r = b.let("r", ir.prim("vtable16", *[ir.lift(x, ir.U8) for x in TABLE16]))
    for k in range(V // 8):
        b.store64(out + 8 * k if k else out, ir.prim(f"lane64_{k}", r))
    return b.build()


def _fn_prefix_xor(isa):
    b = ir.Builder(f"t_{isa}_prefix_xor", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    b.store64(out, prefix_xor(b, ISAS[isa], ir.prim("load64", src).cast(ir.U64)))
    return b.build()


def _fn_store32(isa):
    b = ir.Builder(f"t_{isa}_store32", ir.VOID, isa=isa, public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    b.store64(out, 0xFFFFFFFFFFFFFFFF)     # a store wider than 4 bytes would clear these
    b.store64(out + 8, 0xFFFFFFFFFFFFFFFF)
    b.store32(out + 1, ir.prim("load32u", src + 3))
    b.store32(out + 6, ir.prim("load32u", src) ^ 0x80000001)
    b.store32(out + 11, 0xDEADBEEF)
    return b.build()


def _fn_cpu(name):
    """out[0] = the runtime check `name` (1 or 0), from an AVX2 function."""
    b = ir.Builder(f"t_avx2_{name}", ir.VOID, isa="avx2", public=True)
    b.param("src", ir.PTR)
    out = b.param("out", ir.MPTR)
    with b.if_(ir.prim(name)):
        b.store64(out, 1)
    with b.else_():
        b.store64(out, 0)
    return b.build()


def _fn_half(name):
    b = ir.Builder(f"t_avx2_{name}", ir.VOID, isa="avx2", public=True)
    src, out = b.param("src", ir.PTR), b.param("out", ir.MPTR)
    r = b.let("r", ir.prim(name, src))
    for k in range(4):
        b.store64(out + 8 * k if k else out, ir.prim(f"lane64_{k}", r))
    return b.build()


def _module():
    mod = ir.Module("prims")
    for isa, names in VEC_TO_VEC.items():
        mod.functions += [_fn_vec(isa, n) for n in names]
    for isa, names in VEC_TO_SCALAR.items():
        mod.functions += [_fn_scalar(isa, n) for n in names]
    for isa, names in VEC2_TO_SCALAR.items():
        mod.functions += [_fn_scalar2(isa, n) for n in names]
    for isa, names in BLOCK.items():
        mod.functions += [_fn_block(isa, n) for n in names]
    for isa, names in U64_TO_SCALAR.items():
        mod.functions += [_fn_u64(isa, n) for n in names]
    mod.functions += [_fn_store32(isa) for isa, names in STORES.items() if names]
    mod.functions += [_fn_prefix_xor(isa) for isa, names in HELPERS.items() if names]
    mod.functions += [_fn_vtable16(isa) for isa, names in CONSTS.items() if names]
    mod.functions.append(_fn_half("load_half_bcast"))
    mod.functions.append(_fn_cpu("cpu_avx2_pclmul"))
    return mod


# prims_cpu_has("avx2"): the compiler's own CPU check, built into the test library
CPU_C = """#include <string.h>
#ifdef _WIN32
__declspec(dllexport)
#endif
int prims_cpu_has(const char *isa) {
    __builtin_cpu_init();
    if (!strcmp(isa, "ssse3")) return __builtin_cpu_supports("ssse3");
    if (!strcmp(isa, "sse42")) return __builtin_cpu_supports("sse4.2");
    if (!strcmp(isa, "avx2")) return __builtin_cpu_supports("avx2");
    if (!strcmp(isa, "pclmul")) return __builtin_cpu_supports("pclmul");
    return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    mod = _module()
    d = tmp_path_factory.mktemp("prims")
    for name, text in c_backend.generate(mod, "c").items():
        (d / name).write_text(text, encoding="utf-8")
    # what this CPU runs, asked of the compiler's own check (the test runs the lowerings
    # natively, so an ISA the CPU lacks is skipped, not failed)
    (d / "cpu.c").write_text(CPU_C, encoding="utf-8")
    cc = _cc()
    if not cc:
        pytest.skip("no C compiler")
    lib = d / ("prims.dll" if sys.platform == "win32" else "libprims.so")
    cmd = [cc, "-O1", "-std=c99", "-shared", "-DNVOL_BUILD_DLL", "-o", str(lib), str(d / "nvol.c"), str(d / "cpu.c")]
    cmd.insert(1, "-static" if sys.platform == "win32" else "-fPIC")
    subprocess.run(cmd, check=True)
    lib = ctypes.CDLL(str(lib))
    lib.prims_cpu_has.argtypes = [ctypes.c_char_p]
    return mod, lib


def _skip_unless(lib, isa):
    if not lib.prims_cpu_has(isa.encode()):
        pytest.skip(f"this CPU has no {isa}")


# primitives whose contract only admits compare masks (lanes 0x00 / 0xFF)
MASK_ONLY = {"mask_or", "any_nonzero", "bmask64x2", "bmask64x4"}


def _masks(rng, n):
    p = rng.choice([0.0, 0.02, 0.5])
    return bytes(0xFF if rng.random() < p else 0 for _ in range(n))


def _vectors(rng, n):
    kinds = [lambda: rng.randrange(256), lambda: rng.choice([0, 0x0F, 0x80, 0xFF, 0x7F]),
             lambda: rng.randrange(16), lambda: rng.randrange(16) | 0x80 * (rng.random() < 0.2)]
    k = rng.choice(kinds)
    return bytes(k() for _ in range(n))


def _set16_operands(rng):
    """A pcmpistri member list (members, then zeros from a random point; sometimes all 16
    lanes) and a text vector of members, other bytes and an occasional 0."""
    members = rng.sample(range(1, 256), rng.randrange(1, 17))
    s = bytes(members) + bytes(16 - len(members))
    p = rng.choice([0.0, 0.5, 0.9, 1.0])
    others = [c for c in range(256) if c not in members]
    x = bytes(rng.choice(members) if rng.random() < p else (0 if rng.random() < 0.1 else rng.choice(others))
              for _ in range(16))
    return s + x


@pytest.mark.parametrize("isa", ["sse42"])
def test_two_operand_lowerings(built, isa):
    mod, lib = built
    _skip_unless(lib, isa)
    rng = random.Random(23)
    for name in VEC2_TO_SCALAR[isa]:
        f = mod.fn(f"t_{isa}_{name}")
        cfn = getattr(lib, f.name)
        cfn.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        cfn.restype = None
        for _ in range(2000):
            src = _set16_operands(rng)
            out = (ctypes.c_uint8 * 8)()
            cfn(src, ctypes.addressof(out))
            mem = Memory(src, 0x1000)
            mem.add(bytes(8), 0x9000)
            Interpreter(mod, mem).call(f.name, [0x1000, 0x9000])
            assert bytes(out) == mem.region(0x9000), (isa, name, src.hex(), bytes(out).hex())


@pytest.mark.parametrize("isa", ["ssse3", "avx2"])
def test_lowerings_match_reference(built, isa):
    mod, lib = built
    _skip_unless(lib, isa)
    rng = random.Random(11)
    V = ISAS[isa].bytes
    names = VEC_TO_VEC[isa] + VEC_TO_SCALAR[isa] + BLOCK[isa] + U64_TO_SCALAR[isa] + STORES[isa] + HELPERS[isa] + CONSTS[isa] + (["load_half_bcast"] if isa == "avx2" else [])
    for name in names:
        f = mod.fn(f"t_{isa}_{name}")
        cfn = getattr(lib, f.name)
        cfn.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        cfn.restype = None
        for _ in range(300):
            n = 64 if name in BLOCK[isa] else 2 * V
            src = _masks(rng, n) if name in MASK_ONLY else _vectors(rng, n)
            if name in ZERO_CASE and rng.random() < 0.3:
                src = bytes(2 * V)
            out = (ctypes.c_uint8 * V)()
            cfn(src, ctypes.addressof(out))
            mem = Memory(src, 0x1000)
            mem.add(bytes(V), 0x9000)
            Interpreter(mod, mem).call(f.name, [0x1000, 0x9000])
            want = mem.region(0x9000)
            assert bytes(out) == want, (isa, name, src.hex(), bytes(out).hex(), want.hex())



def test_cpu_avx2_pclmul(built):
    """The generated check agrees with the compiler's: AVX2 and PCLMULQDQ."""
    mod, lib = built
    _skip_unless(lib, "avx2")
    cfn = lib.t_avx2_cpu_avx2_pclmul
    cfn.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    cfn.restype = None
    out = (ctypes.c_uint8 * 8)()
    cfn(bytes(8), ctypes.addressof(out))
    assert out[0] == (bool(lib.prims_cpu_has(b"avx2")) and bool(lib.prims_cpu_has(b"pclmul")))


def test_prefix_xor_reference():
    """prefix_xor64's semantics is the low half of the carry-less product with all ones, and
    the shift/xor steps of nvol.isa.prefix_xor (SSSE3 and NEON) compute it."""
    from nvol.primitives import _prefix_xor64
    M = (1 << 64) - 1
    rng = random.Random(34)
    xs = [0, M, 1, 1 << 63, 0x8000000000000001, 0x5555555555555555] + [rng.getrandbits(64) for _ in range(200)]
    for x in xs:
        clmul = 0
        for i in range(64):
            if x >> i & 1:
                clmul ^= (M << i) & M
        assert _prefix_xor64(None, x) == clmul, hex(x)
    for isa in ("ssse3", "neon"):
        mod = ir.Module("px")
        mod.arch = ISAS[isa].arch
        mod.functions.append(_fn_prefix_xor(isa))
        for x in xs[:40]:
            mem = Memory(x.to_bytes(8, "little"), 0x1000)
            mem.add(bytes(8), 0x9000)
            Interpreter(mod, mem).call(f"t_{isa}_prefix_xor", [0x1000, 0x9000])
            assert int.from_bytes(mem.region(0x9000), "little") == _prefix_xor64(None, x), (isa, hex(x))
    b = ir.Builder("t", ir.U64, isa="avx2")
    assert prefix_xor(b, ISAS["avx2"], b.param("x", ir.U64)).name == "prefix_xor64"
