"""The assembly backend function by function: the whole module is assembled with NASM (every
function exported), linked into a shared library and each function called through ctypes on
random inputs, against the reference interpreter running the same IR (results, every byte the
function writes and every call of a visitor).

Both sides run the same IR, so a kernel's own preconditions (a class of the right kind, at least
one vector of input) do not matter here; cases the interpreter rejects (a read outside every
buffer) are skipped, and every function must still run a minimum number of valid cases. The
real buffers are 64-byte aligned like the interpreter's addresses, so both take the same
alignment paths (and write the same scratch bytes).

Every syntax (NASM, GNU as; MASM on Windows) is built from the same machine IR and checked on the same
cases: the interpreter runs once per case and each library is compared against it.

With NVOL_ARCH=aarch64 the same tests run the aarch64 module (A64, AAPCS64, GNU as syntax) under
QEMU (harness/asm/run-a64.sh): NVOL_A64_CC is the cross compiler that assembles and links, and
the primitives are the NEON ones.

Three groups: the scalar functions (with the dispatchers, which resolve through the CPU checks
of a small C stub), the vector kernels (every ISA this CPU runs), and every vector primitive
alone (the functions tests/test_prims.py builds). Teddy runs on a patterns blob built here and
a state built by the interpreter's `nvol_teddy_init`, for each variant (slim/fat, mask 1-4)."""
import ctypes
import os
import random
import shlex
import shutil
import subprocess
import sys
import zlib

import pytest

import asm_abi_stub
import test_prims
from conftest import _cc
from nvol import ir
from nvol.asm import SYNTAX_EXT, asm_module, library
from nvol.asm.masm import find_ml64
from nvol.interp import UB, Interpreter, Memory
from nvol.isa import ISAS
from nvol.kernels import build_modules
from nvol.__main__ import json_cases, utf8_cases
from nvol.kernels.byteset import CLASS_BYTES, PAIR_BYTES
from nvol.kernels.memmem import STATE_WORDS
from nvol.kernels.teddy import STATE_BYTES as TEDDY_STATE
from nvol.kernels.json import STATE_BYTES as JSON_STATE

ARCH = os.environ.get("NVOL_ARCH", "x86_64")
A64 = ARCH == "aarch64"
ABI = "aapcs64" if A64 else "win64" if sys.platform == "win32" else "sysv"
FMT = "win64" if ABI == "win64" else "elf64"
CASES = 300
A64_CC = shlex.split(os.environ.get("NVOL_A64_CC", "clang --target=aarch64-alpine-linux-musl"))

MOD = build_modules()[ARCH]
CPU_C = "int prims_cpu_has(const char *isa) { (void)isa; return 1; }\n" if A64 else test_prims.CPU_C + """
int nvol_cpu_avx2(void) { __builtin_cpu_init(); return __builtin_cpu_supports("avx2"); }
int nvol_cpu_ssse3(void) { __builtin_cpu_init(); return __builtin_cpu_supports("ssse3"); }
int nvol_cpu_sse42(void) { __builtin_cpu_init(); return __builtin_cpu_supports("sse4.2"); }
int nvol_cpu_sse42_pclmul(void) {
    __builtin_cpu_init();
    return __builtin_cpu_supports("sse4.2") && __builtin_cpu_supports("pclmul");
}
int nvol_cpu_avx2_pclmul_bmi1(void) {
    __builtin_cpu_init();
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("pclmul") && __builtin_cpu_supports("bmi");
}
"""
CTX = 0x7E57C0DE0000
VISITOR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64)
CT = {ir.U8: ctypes.c_uint8, ir.U32: ctypes.c_uint32, ir.U64: ctypes.c_uint64,
      ir.USIZE: ctypes.c_size_t, ir.BOOL: ctypes.c_bool, ir.PTR: ctypes.c_void_p,
      ir.MPTR: ctypes.c_void_p}


def _tested(f) -> bool:
    try:
        _gen(random.Random(0), f.name, 8)
        return True
    except KeyError:
        return False


# the NEON primitives (NEON is the baseline of aarch64: every one runs)
NEON_VEC = ["cmpeq", "vor", "mask_or", "vand", "vxor", "vandnot", "vsub8", "vsubs8", "vmin8", "vshuffle", "shin1", "shin2",
            "shin3", "sad0", "vadd64", "vnib_hi", "msb_lanes"]
NEON_SCALAR = ["is_zero", "any_nonzero", "movemask", "lane64_0", "lane64_1", "hsum64"]


def _neon_prims():
    mod = ir.Module("prims")
    mod.functions += [test_prims._fn_vec("neon", n) for n in NEON_VEC]
    mod.functions += [test_prims._fn_scalar("neon", n) for n in NEON_SCALAR]
    mod.functions.append(test_prims._fn_block("neon", "bmask64x4"))
    mod.functions += [test_prims._fn_u64("neon", n) for n in ("popcnt64", "tzcnt64", "ctz64_lax")]
    mod.functions.append(test_prims._fn_store32("neon"))
    mod.functions.append(test_prims._fn_prefix_xor("neon"))
    mod.functions.append(test_prims._fn_vtable16("neon"))
    mod.functions.append(test_prims._fn_vtail3("neon"))
    return mod


PRIMS = _neon_prims() if A64 else test_prims._module()


SYNTAXES = ["gas"] if A64 else ["nasm"] + (["masm"] if ABI == "win64" and find_ml64() else []) + \
    (["gas"] if shutil.which("as") else [])


class Libs(list):
    """[(syntax, library)]: the same functions assembled in every syntax this machine has."""

    def prims_cpu_has(self, isa):
        return self[0][1].prims_cpu_has(isa)


def _assemble(syntax, src, obj):
    if A64:
        subprocess.run(A64_CC + ["-c", "-o", str(obj), str(src)], check=True)
    elif syntax == "nasm":
        subprocess.run(["nasm", "-f", FMT, str(src), "-o", str(obj)], check=True)
    elif syntax == "gas":
        subprocess.run(["as", "--64", "-o", str(obj), str(src)], check=True)
    else:
        subprocess.run([find_ml64(), "-nologo", "-c", f"-Fo{obj}", str(src)], check=True,
                       stdout=subprocess.DEVNULL)


def _build(d, name, functions, cc, mod=None):
    sobj = d / "abi_stub.o"
    if A64:
        stub = d / "abi_stub.s"
        stub.write_text(asm_abi_stub.aapcs64(), encoding="utf-8")
        _assemble("gas", stub, sobj)
    else:
        stub = d / "abi_stub.asm"
        stub.write_text(asm_abi_stub.win64() if ABI == "win64" else asm_abi_stub.sysv(), encoding="utf-8")
        subprocess.run(["nasm", "-f", FMT, str(stub), "-o", str(sobj)], check=True)
    (d / "cpu.c").write_text(CPU_C, encoding="utf-8")
    libs = Libs()
    for syntax in SYNTAXES:
        src = d / f"{name}_{syntax}{SYNTAX_EXT[syntax]}"
        text = library(MOD, ABI, syntax, export_all=True) if functions is None else \
            asm_module(functions, ABI, mod, syntax)
        src.write_text(text, encoding="utf-8")
        obj = d / f"{name}_{syntax}.o"
        _assemble(syntax, src, obj)
        so = d / (f"{name}_{syntax}.dll" if sys.platform == "win32" else f"lib{name}_{syntax}.so")
        # (on Windows the dllexport in cpu.c turns off mingw's automatic export of the rest)
        exp = ["-Wl,--export-all-symbols"] if sys.platform == "win32" else []
        link = A64_CC + ["-fuse-ld=lld", "-fPIC"] if A64 else [cc]
        subprocess.run(link + ["-shared", *exp, "-o", str(so), str(obj), str(sobj), str(d / "cpu.c")], check=True)
        lib = ctypes.CDLL(str(so))
        lib.abi_call.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.abi_call.restype = None
        lib.prims_cpu_has.argtypes = [ctypes.c_char_p]
        libs.append((syntax, lib))
    return libs


@pytest.fixture(scope="module")
def tools():
    if A64:
        if not shutil.which(A64_CC[0]):
            pytest.skip("no aarch64 cross compiler")
        return A64_CC[0]
    if not shutil.which("nasm"):
        pytest.skip("no nasm")
    cc = _cc()
    if not cc:
        pytest.skip("no C compiler")
    return cc


@pytest.fixture(scope="module")
def lib(tools, tmp_path_factory):
    return _build(tmp_path_factory.mktemp("asm"), "k", None, tools)


@pytest.fixture(scope="module")
def primlib(tools, tmp_path_factory):
    return _build(tmp_path_factory.mktemp("asmprims"), "p", PRIMS.functions, tools)


def _skip_unless(lib, isa):
    if isa and isa != "sse2" and not lib.prims_cpu_has(isa.encode()):
        pytest.skip(f"this CPU has no {isa}")


class Case:
    """Regions of memory, the same on both sides: aligned real buffers for the assembly, the
    interpreter's Memory at fixed fake addresses."""

    def __init__(self):
        self.regions = []

    def region(self, data: bytes) -> int:
        self.regions.append(bytes(data))
        return len(self.regions) - 1


def _ptr(region, off):
    return ("ptr", region, off)


def _visit(stop):
    """A visitor argument: it records every call and stops on call number `stop` (or never)."""
    return ("visit", stop)


def _where(p, bases, lens):
    if not p:
        return None
    for k, b in enumerate(bases):
        if b - 64 <= p <= b + lens[k] + 64:
            return (k, p - b)
    return ("outside", p)


def _aligned(data: bytes):
    raw = ctypes.create_string_buffer(len(data) + 128)
    off = (-ctypes.addressof(raw)) % 64
    ctypes.memmove(ctypes.addressof(raw) + off, data, len(data))
    return raw, ctypes.addressof(raw) + off


def _run(libs, mod, name, case: Case, args):
    f = mod.fn(name)
    bases = [0x100000 * (k + 1) for k in range(len(case.regions))]
    mem = Memory(case.regions[0], bases[0])
    for k in range(1, len(case.regions)):
        mem.add(case.regions[k], bases[k])
    lens = [len(r) for r in case.regions]
    seen_i = []

    def ivisit(stop):
        def cb(at, mask):
            seen_i.append((_where(at, bases, lens), mask))
            return len(seen_i) - 1 == stop
        return cb

    iargs = [ivisit(a[1]) if isinstance(a, tuple) and a[0] == "visit" else
             bases[a[1]] + a[2] if isinstance(a, tuple) else a for a in args]
    want = Interpreter(mod, mem).call(name, iargs)
    for syntax, lib in libs:
        _check(syntax, lib, f, name, case, args, want, seen_i, mem, bases, lens)


def _check(syntax, lib, f, name, case, args, want, seen_i, mem, bases, lens):
    bufs = [_aligned(r) for r in case.regions]
    seen_a, keep = [], []

    def avisit(stop):
        def cb(ctx, at, mask):
            # (the kernel must pass the visitor's own ctx)
            seen_a.append((_where(at, [b[1] for b in bufs], lens), mask) if ctx == CTX else "bad ctx")
            # "stop" is any non-zero int: 0x100 has a zero low byte, a kernel that tested al
            # would keep going
            return 0x100 if len(seen_a) - 1 == stop else 0
        fn = VISITOR(cb)
        st = (ctypes.c_void_p * 2)(ctypes.cast(fn, ctypes.c_void_p), CTX)
        keep.extend([fn, st])
        return ctypes.addressof(st)

    # through the ABI trampoline: narrow arguments with garbage in their upper bits, every
    # callee-saved register checked after the call
    vals = []
    for p, a in zip(f.params, args):
        v = avisit(a[1]) if isinstance(a, tuple) and a[0] == "visit" else \
            bufs[a[1]][1] + a[2] if isinstance(a, tuple) else a
        if p.ty in (ir.U8, ir.BOOL):
            v |= 0xA5C3E1F0DEADBE00
        elif p.ty is ir.U32:
            v |= 0xA5C3E1F000000000
        vals.append(v)
    argv = (ctypes.c_uint64 * 8)(*vals)
    out = (ctypes.c_uint64 * 2)()
    lib.abi_call(ctypes.cast(getattr(lib, name), ctypes.c_void_p), argv, out)
    assert out[1] == 0, (syntax, name, "a callee-saved register came back changed")
    mask = {ir.U8: 0xFF, ir.BOOL: 0xFF, ir.U32: 0xFFFFFFFF}.get(f.ret, (1 << 64) - 1)
    got = None if f.ret is ir.VOID else out[0] & mask
    if f.ret in (ir.PTR, ir.MPTR):
        def where(p, bs):
            if not p:
                return None
            for k, b in enumerate(bs):
                if b <= p <= b + len(case.regions[k]):
                    return (k, p - b)
            return ("outside", p)
        want = where(want, bases)
        got = where(got, [b[1] for b in bufs])
    elif f.ret is ir.BOOL:
        want, got = bool(want), bool(got)
    assert got == want, (syntax, name, args, case.regions)
    assert seen_a == seen_i, (syntax, name, "visitor calls", args)
    for k, r in enumerate(case.regions):
        assert ctypes.string_at(bufs[k][1], len(r)) == mem.region(bases[k]), (syntax, name, "region", k, args)


def _bytes(rng, n, alphabet=None):
    if alphabet is None:
        return bytes(rng.randrange(256) for _ in range(n))
    return bytes(rng.choice(alphabet) for _ in range(n))


def _class(set_bytes: bytes) -> bytes:
    mem = Memory(set_bytes or b"\0", 0x1000)
    mem.add(bytes(CLASS_BYTES), 0x9000)
    Interpreter(MOD, mem).call("nvol_byteset_init", [0x1000, len(set_bytes), 0x9000])
    return mem.region(0x9000)


def _pair(s1, s2, i1, i2) -> bytes:
    mem = Memory(s1 or b"\0", 0x1000)
    mem.add(s2 or b"\0", 0x3000)
    mem.add(bytes(PAIR_BYTES), 0x9000)
    Interpreter(MOD, mem).call("nvol_byteset_pair_init", [0x1000, len(s1), 0x3000, len(s2), i1, i2, 0x9000])
    return mem.region(0x9000)


def _teddy_blob(pats) -> bytes:
    """u64 n | n x (u64 offset, u64 len) | pattern bytes (offsets from the blob start)."""
    head = 8 + 16 * len(pats)
    out, data = bytearray(len(pats).to_bytes(8, "little")), bytearray()
    for p in pats:
        out += (head + len(data)).to_bytes(8, "little") + len(p).to_bytes(8, "little")
        data += p
    return bytes(out + data)


def _teddy_state(blob: bytes, avx2: bool) -> bytes:
    mem = Memory(blob, 0x1000)
    mem.add(bytes(TEDDY_STATE), 0x9000)
    Interpreter(MOD, mem, cpu={"avx2": avx2, "ssse3": True}).call("nvol_teddy_init", [0x1000, 0x9000])
    return mem.region(0x9000)


def _finder(needle: bytes) -> bytes:
    mem = Memory(needle, 0x1000)
    mem.add(bytes(8 * STATE_WORDS), 0x9000)
    Interpreter(MOD, mem).call("nvol_memmem_init", [0x1000, 0x1000 + len(needle), 0x9000])
    return mem.region(0x9000)


def _hay(rng, c, alpha, maxlen):
    hay = _bytes(rng, rng.randrange(0, maxlen), alpha)
    h = c.region(hay)
    a = rng.randrange(len(hay) + 1)
    return hay, h, a, rng.randrange(a, len(hay) + 1)


def _gen(rng, name, maxlen):
    """(Case, args) for one random call of `name`."""
    c = Case()
    alpha = list(b"abcxyz\x00\x80\xff")
    if name.startswith(("nvol_memchr", "nvol_memrchr", "nvol_count")):
        k = 3 if name.startswith(("nvol_memchr3", "nvol_memrchr3")) else \
            2 if name.startswith(("nvol_memchr2", "nvol_memrchr2")) else 1
        _, h, a, b = _hay(rng, c, alpha, maxlen)
        return c, [rng.choice(alpha) for _ in range(k)] + [_ptr(h, a), _end(name, h, a, b)]
    if name.startswith("nvol_first_non_ascii"):
        hay = bytes(rng.randrange(128) if rng.random() < 0.97 else rng.randrange(128, 256)
                    for _ in range(rng.randrange(0, maxlen)))
        h = c.region(hay)
        a = rng.randrange(len(hay) + 1)
        return c, [_ptr(h, a), _end(name, h, a, rng.randrange(a, len(hay) + 1))]
    if name.startswith("nvol_is_equal"):
        x = _bytes(rng, rng.randrange(1, 40), alpha)
        y = bytearray(x)
        if rng.random() < 0.5:
            y[rng.randrange(len(y))] ^= 1 << rng.randrange(8)
        hx, hy = c.region(x), c.region(bytes(y))
        return c, [_ptr(hx, 0), _ptr(hy, 0), rng.randrange(len(x) + 1)]
    if name.startswith("nvol_memmem_suffix"):
        nd = _bytes(rng, rng.randrange(1, 24), list(b"abc"))
        h = c.region(nd)
        return c, [_ptr(h, 0), _ptr(h, len(nd))]
    if name == "nvol_memmem_init":
        nd = _bytes(rng, rng.randrange(0, 40), list(b"abc") + list(range(256)))
        n, o = c.region(nd), c.region(bytes(8 * STATE_WORDS))
        return c, [_ptr(n, 0), _ptr(n, len(nd)), _ptr(o, 0)]
    if name.startswith("nvol_memmem_"):
        names = [p.name for p in MOD.fn(name).params]
        if names[-4:] != ["nstart", "nend", "hstart", "hend"]:
            raise KeyError(name)
        small = name.startswith("nvol_memmem_pppre")
        nd = _bytes(rng, rng.randrange(2, 12) if small else rng.randrange(1, 90), list(b"abc"))   # (> 32: Two-Way)
        hay = bytearray(_bytes(rng, rng.randrange(0, maxlen), list(b"abcd")))
        if hay and rng.random() < 0.5:
            p = rng.randrange(len(hay) + 1)
            hay[p:p] = nd
        n, h = c.region(nd), c.region(bytes(hay))
        a = rng.randrange(len(hay) + 1)
        args = [_ptr(n, 0), _ptr(n, len(nd)), _ptr(h, a), _ptr(h, rng.randrange(a, len(hay) + 1))]
        if names[0] == "state":
            args.insert(0, _ptr(c.region(_finder(nd)), 0))
        return c, args
    if name == "nvol_byteset_init":
        s = _bytes(rng, rng.randrange(0, 24), alpha + list(range(1, 40)))
        h, o = c.region(s), c.region(bytes(CLASS_BYTES))
        return c, [_ptr(h, 0), len(s), _ptr(o, 0)]
    if name == "nvol_byteset_pair_init":
        s1 = _bytes(rng, rng.randrange(0, 12), alpha)
        s2 = _bytes(rng, rng.randrange(0, 12), alpha)
        h1, h2, o = c.region(s1), c.region(s2), c.region(bytes(PAIR_BYTES))
        return c, [_ptr(h1, 0), len(s1), _ptr(h2, 0), len(s2), rng.randrange(6), rng.randrange(6), _ptr(o, 0)]
    if name.startswith("nvol_find_byteset_pair"):
        s1 = _bytes(rng, rng.randrange(1, 8), alpha)
        s2 = _bytes(rng, rng.randrange(1, 8), alpha)
        st = c.region(_pair(s1, s2, rng.randrange(5), rng.randrange(5)))
        _, h, a, b = _hay(rng, c, alpha, maxlen)
        args = [_ptr(st, 0), _ptr(h, a), _ptr(h, b)]
        if "mask" in name:
            args.append(_ptr(c.region(bytes(16)), 0))
        if "visit" in name:
            args.append(_visit(rng.choice([-1, 0, 1, 2])))
        return c, args
    if name.startswith("nvol_teddy"):
        kernel = name[-1].isdigit()
        fat = "_fat" in name
        m = int(name[-1]) if kernel else rng.randint(1, 4)
        avx2 = "ssse3" not in name
        npat = rng.randint(33, 64) if fat else rng.randint(1, 16 if m == 1 else 32)
        abc = b"abcdefgh"
        pats = [bytes(rng.choice(abc) for _ in range(m))] + \
            [bytes(rng.choice(abc) for _ in range(rng.randint(m, m + 3))) for _ in range(npat - 1)]
        rng.shuffle(pats)
        blob = _teddy_blob(pats)
        bl = c.region(blob)
        if name == "nvol_teddy_init":
            return c, [_ptr(bl, 0), _ptr(c.region(bytes(TEDDY_STATE)), 0)]
        st = c.region(_teddy_state(blob, avx2))
        hay = bytearray(_bytes(rng, rng.randrange(0, maxlen), list(abc + b"z")))
        for _ in range(rng.randrange(3)):
            if hay:
                q = rng.choice(pats)
                at = rng.randrange(len(hay) + 1)
                hay[at:at] = q
        h = c.region(bytes(hay))
        a = rng.randrange(len(hay) + 1)
        b = rng.randrange(a, len(hay) + 1)
        if "verify64" in name:
            return c, [_ptr(st, 0), _ptr(bl, 0), _ptr(h, a), _ptr(h, 0), _ptr(h, len(hay)),
                       rng.randrange(1 << 64) & rng.choice([0xFF, 0xFFFF, (1 << 64) - 1])]
        return c, [_ptr(st, 0), _ptr(bl, 0), _ptr(h, a), _ptr(h, b)]
    if name.startswith("nvol_json_index"):
        data, _, state = next(json_cases(rng, 1))
        st = c.region(b"".join(w.to_bytes(8, "little") for w in state).ljust(JSON_STATE, b"\0"))
        h, o = c.region(data), c.region(bytes(4 * len(data)))
        return c, [_ptr(st, 0), _ptr(h, 0), len(data) // 64, rng.randrange(1 << 20), _ptr(o, 0)]
    if name.startswith("nvol_utf8_valid"):
        data = next(utf8_cases(rng, 1))
        return c, [_ptr(c.region(data), 0), len(data)]
    if name.startswith(("nvol_find_byteset", "nvol_find_not_byteset", "nvol_copy_until_byteset")):
        s = _bytes(rng, rng.randrange(0, 20), alpha + list(range(1, 32)))
        cls = c.region(_class(s))
        hay, h, a, b = _hay(rng, c, alpha + list(range(1, 32)), maxlen)
        args = [_ptr(cls, 0), _ptr(h, a), _ptr(h, b)]
        if "copy" in name:
            args.append(_ptr(c.region(bytes(len(hay))), 0))
        return c, args
    raise KeyError(name)


def _end(name, h, a, b):
    """The end of a range: a pointer, or a length for the public functions that take one."""
    return b - a if MOD.fn(name).params[-1].ty is ir.USIZE else _ptr(h, b)


SCALAR = [f.name for f in MOD.functions if not f.isa and _tested(f)]
VECTOR = [f.name for f in MOD.functions if f.isa and _tested(f)]


def _cases(lib, name, maxlen, cases=CASES):
    rng = random.Random(zlib.crc32(name.encode()))
    ran = 0
    for _ in range(cases):
        case, args = _gen(rng, name, maxlen)
        try:
            _run(lib, MOD, name, case, args)
            ran += 1
        except UB:
            continue
    assert ran >= cases // 5, (name, ran)


# the interpreter answers the CPU checks with "yes"; these pick their kernel from them
NEEDS_AVX2 = {"nvol_teddy_init", "nvol_teddy_find_raw"}


@pytest.mark.parametrize("name", SCALAR)
def test_scalar_function(lib, name):
    if name in NEEDS_AVX2:
        _skip_unless(lib, "avx2")
    _cases(lib, name, 90)


@pytest.mark.parametrize("name", VECTOR)
def test_vector_kernel(lib, name):
    _skip_unless(lib, MOD.fn(name).isa)
    _cases(lib, name, 160, cases=100)     # the interpreter is slow on vector kernels


@pytest.mark.parametrize("name", [f.name for f in PRIMS.functions])
def test_primitive(primlib, name):
    """Each primitive alone, through the same builder functions as tests/test_prims.py."""
    f = PRIMS.fn(name)
    _skip_unless(primlib, f.isa)
    V = ISAS[f.isa].bytes
    prim = name.split("_", 2)[2]
    rng = random.Random(zlib.crc32(name.encode()))
    for _ in range(CASES):
        if prim == "skip_set16":
            src = test_prims._set16_operands(rng)
        elif prim in test_prims.MASK_ONLY:
            src = test_prims._masks(rng, 64 if prim.startswith("bmask64") else 2 * V)
        else:
            src = test_prims._vectors(rng, 2 * V)
            if prim in test_prims.ZERO_CASE and rng.random() < 0.3:
                src = bytes(2 * V)
            if prim in test_prims.NONZERO and not any(src[:8]):
                src = bytes([1]) + src[1:]
        c = Case()
        s, o = c.region(src), c.region(bytes(V))
        _run(primlib, PRIMS, name, c, [_ptr(s, 0), _ptr(o, 0)])


def _inline_module():
    """A callee that assigns its parameter, inlined into a caller that reads the argument after
    the call: the inlined body must work on a copy (no kernel does this today)."""
    b = ir.Builder("t_bump", ir.U64)
    x = b.param("x", ir.U64)
    with b.while_(x < 1000):
        b.set(x, x + 7)
    b.ret(x)
    callee = b.build()
    b = ir.Builder("t_caller", ir.U64, public=True)
    y = b.param("y", ir.U64)
    p = b.param("p", ir.PTR)
    r = b.let("r", ir.Call("t_bump", [y], ir.U64))
    b.ret(r.wmul(3).wadd(y).wadd(ir.Load8(p).cast(ir.U64)))
    return ir.Module("t", [callee, b.build()])


def test_inline_assigned_param(tools, tmp_path_factory):
    mod = _inline_module()
    lib = _build(tmp_path_factory.mktemp("asminl"), "i", mod.functions, tools, mod)
    rng = random.Random(7)
    for _ in range(50):
        c = Case()
        h = c.region(bytes([rng.randrange(256)]))
        _run(lib, mod, "t_caller", c, [rng.randrange(2000), _ptr(h, 0)])
