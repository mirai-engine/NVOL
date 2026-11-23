from ..ir import Module
from ..isa import ARCH_ISAS, ISAS
from . import ascii, byteset, count, json, memchr, memmem, teddy, utf8

ALL = ("memchr", "count", "ascii", "memmem", "teddy", "byteset", "json", "utf8")   # memmem needs memchr; teddy needs memmem
ARCHS = ("x86_64", "aarch64")
_FAMILIES = {"memchr": memchr, "count": count, "ascii": ascii, "memmem": memmem, "teddy": teddy, "byteset": byteset, "json": json, "utf8": utf8}


def build_module(isa_names=None, kernels=ALL, arch="x86_64") -> Module:
    """One architecture's module, with runtime dispatch on x86_64 and NEON (baseline) on
    aarch64. `isa_names` is the ISA list of memchr, count, ascii and memmem (default SSE2 and
    AVX2 on x86_64). Teddy and byteset build their own fixed set (Teddy SSSE3/AVX2; byteset
    SSE2 chains, SSSE3/AVX2 lookups and the SSE4.2 pcmpistri kernel), which their entries
    dispatch to, whatever the list."""
    if isa_names is None:
        isa_names = ("sse2", "avx2") if arch == "x86_64" else ARCH_ISAS[arch]
    isas = [ISAS[n] for n in isa_names]
    mod = Module(f"nvol_{arch}")
    mod.arch = arch
    for k in kernels:
        _FAMILIES[k].build(isas, mod)
    return mod


def build_modules(kernels=ALL) -> dict:
    """{arch: Module} for every supported architecture (same public API)."""
    mods = {a: build_module(kernels=kernels, arch=a) for a in ARCHS}
    apis = {a: [x[1] for x in m.api] for a, m in mods.items()}
    assert len({tuple(v) for v in apis.values()}) == 1, f"public API differs between archs: {apis}"
    return mods
