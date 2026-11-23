"""C99 / C++17 backend (x86_64: SSE2 + AVX2 with runtime dispatch).

Output: `nvol.h` + `nvol.c` (C), or a header-only `nvol.hpp` (C++).
Compilers: GCC, Clang, MSVC. AVX2 functions get `target("avx2")` on GCC/Clang
so the library builds with plain `-O2` and dispatches at runtime.
"""
from __future__ import annotations

from .. import ir
from . import finder, iters, packed
from ..isa import lowering
from .base import Printer

COMMON_PRELUDE = r"""
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#if defined(_MSC_VER) && !defined(__clang__)
#  include <intrin.h>
#  define NVOL_NOINLINE __declspec(noinline)
#  define NVOL_INLINE static __forceinline
#  define NVOL_FORCE_INLINE __forceinline   /* for callers: the attribute alone */
#  define NVOL_UNUSED
#  define NVOL_MEMCPY memcpy
static __forceinline uint32_t nvol_ctz32(uint32_t x) { unsigned long i; _BitScanForward(&i, x); return (uint32_t)i; }
static __forceinline uint32_t nvol_clz32(uint32_t x) { unsigned long i; _BitScanReverse(&i, x); return 31u - (uint32_t)i; }
static __forceinline uint32_t nvol_ctz64(uint64_t x) { unsigned long i; _BitScanForward64(&i, x); return (uint32_t)i; }
static __forceinline uint32_t nvol_clz64(uint64_t x) { unsigned long i; _BitScanReverse64(&i, x); return 63u - (uint32_t)i; }
static __forceinline uint32_t nvol_popcnt32(uint32_t x) {
    x = x - ((x >> 1) & 0x55555555u); x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
    return (((x + (x >> 4)) & 0x0F0F0F0Fu) * 0x01010101u) >> 24;
}
static __forceinline uint32_t nvol_popcnt64(uint64_t x) { return nvol_popcnt32((uint32_t)x) + nvol_popcnt32((uint32_t)(x >> 32)); }
#else
#  define NVOL_NOINLINE __attribute__((noinline))
#  define NVOL_INLINE static inline __attribute__((always_inline))
#  define NVOL_FORCE_INLINE __attribute__((always_inline))   /* for callers: the attribute alone */
/* the memory helpers below: with the kernels in assembly (asm_kernels) nothing may call
   them, and clang warns about an unused static inline function of a .c */
#  define NVOL_UNUSED __attribute__((unused))
/* the builtin, not memcpy: fortify headers (musl, glibc _FORTIFY_SOURCE) wrap memcpy in an
   inline function with overlap checks that stay in the hot loop of every load64/store64 */
#  define NVOL_MEMCPY __builtin_memcpy
#  define nvol_ctz32(x) ((uint32_t)__builtin_ctz(x))
#  define nvol_clz32(x) ((uint32_t)__builtin_clz(x))
#  define nvol_ctz64(x) ((uint32_t)__builtin_ctzll(x))
#  define nvol_clz64(x) ((uint32_t)__builtin_clzll(x))
/* the bit count as the SWAR idiom, not __builtin_popcount: GCC and Clang turn it into POPCNT in
   a function whose target has it (AVX2, SSE4.2, and what is inlined there) and keep it inline
   elsewhere, where GCC's builtin is a call to libgcc's __popcountdi2 (parity, PENDING item 20) */
static inline NVOL_FORCE_INLINE NVOL_UNUSED uint32_t nvol_popcnt32(uint32_t x) {
    x = x - ((x >> 1) & 0x55555555u); x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
    return (((x + (x >> 4)) & 0x0F0F0F0Fu) * 0x01010101u) >> 24;
}
static inline NVOL_FORCE_INLINE NVOL_UNUSED uint32_t nvol_popcnt64(uint64_t x) {
    x = x - ((x >> 1) & 0x5555555555555555ull);
    x = (x & 0x3333333333333333ull) + ((x >> 2) & 0x3333333333333333ull);
    return (uint32_t)((((x + (x >> 4)) & 0x0F0F0F0F0F0F0F0Full) * 0x0101010101010101ull) >> 56);
}
#endif
static inline NVOL_UNUSED uint32_t nvol_load16u(const uint8_t *p) { uint16_t v; NVOL_MEMCPY(&v, p, 2); return v; }
static inline NVOL_UNUSED uint32_t nvol_load32u(const uint8_t *p) { uint32_t v; NVOL_MEMCPY(&v, p, 4); return v; }
static inline NVOL_UNUSED uint64_t nvol_load64(const uint8_t *p) { uint64_t v; NVOL_MEMCPY(&v, p, 8); return v; }
static inline NVOL_UNUSED void nvol_store64(uint8_t *p, uint64_t v) { NVOL_MEMCPY(p, &v, 8); }
static inline NVOL_UNUSED void nvol_store32(uint8_t *p, uint32_t v) { NVOL_MEMCPY(p, &v, 4); }
static inline NVOL_UNUSED void nvol_store8(uint8_t *p, uint8_t v) { *p = v; }
#ifndef NVOL_VISITOR_DEFINED
#define NVOL_VISITOR_DEFINED
/* visitor kernels (_visit): fn(ctx, chunk, mask) for every chunk with candidates; non-zero stops */
typedef struct nvol_visitor { int (*fn)(void *ctx, const uint8_t *chunk, uint64_t mask); void *ctx; } nvol_visitor;
#endif
""".strip("\n")

X86_PRELUDE = r"""
#include <immintrin.h>
#if defined(_MSC_VER) && !defined(__clang__)
#  define NVOL_TARGET_AVX2
#  define NVOL_TARGET_SSSE3
#  define NVOL_TARGET_SSE4_2
static int nvol_cpu_avx2_detect(void) {
    int r[4]; __cpuid(r, 1);
    if (!((r[2] >> 27) & 1) || !((r[2] >> 28) & 1)) return 0;   /* OSXSAVE, AVX */
    if ((_xgetbv(0) & 6) != 6) return 0;                          /* XMM|YMM state */
    __cpuidex(r, 7, 0); return (r[1] >> 5) & 1;                   /* AVX2 */
}
static int nvol_cpu_ssse3_detect(void) { int r[4]; __cpuid(r, 1); return (r[2] >> 9) & 1; }
static int nvol_cpu_sse42_detect(void) { int r[4]; __cpuid(r, 1); return (r[2] >> 20) & 1; }
static int nvol_cpu_avx2_pclmul_detect(void) {
    int r[4]; __cpuid(r, 1);
    return ((r[2] >> 1) & 1) && nvol_cpu_avx2_detect();          /* PCLMULQDQ */
}
#else
/* every CPU with AVX2 or SSE4.2 has POPCNT (the dispatch does not check it, nor does the asm):
   without the feature, the bit count in those kernels stays a software count. Every AVX2 CPU
   also has PCLMULQDQ; the kernels that use it dispatch on nvol_cpu_avx2_pclmul, which checks
   both (a hypervisor can hide one) */
#  define NVOL_TARGET_AVX2 __attribute__((target("avx2,popcnt,pclmul")))
#  define NVOL_TARGET_SSSE3 __attribute__((target("ssse3")))
#  define NVOL_TARGET_SSE4_2 __attribute__((target("sse4.2,popcnt")))
static int nvol_cpu_avx2_detect(void) { __builtin_cpu_init(); return __builtin_cpu_supports("avx2"); }
static int nvol_cpu_ssse3_detect(void) { __builtin_cpu_init(); return __builtin_cpu_supports("ssse3"); }
static int nvol_cpu_sse42_detect(void) { __builtin_cpu_init(); return __builtin_cpu_supports("sse4.2"); }
static int nvol_cpu_avx2_pclmul_detect(void) {
    __builtin_cpu_init(); return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("pclmul");
}
#endif
static int nvol_cpu_ssse3_cache = -1;
static inline int nvol_cpu_ssse3(void) {
    int v = nvol_cpu_ssse3_cache;
    if (v < 0) { v = nvol_cpu_ssse3_detect(); nvol_cpu_ssse3_cache = v; }
    return v;
}
static int nvol_cpu_sse42_cache = -1;
static inline int nvol_cpu_sse42(void) {
    int v = nvol_cpu_sse42_cache;
    if (v < 0) { v = nvol_cpu_sse42_detect(); nvol_cpu_sse42_cache = v; }
    return v;
}
static int nvol_cpu_avx2_cache = -1;
static inline int nvol_cpu_avx2(void) {
    int v = nvol_cpu_avx2_cache;
    if (v < 0) { v = nvol_cpu_avx2_detect(); nvol_cpu_avx2_cache = v; }
    return v;
}
static int nvol_cpu_avx2_pclmul_cache = -1;
static inline NVOL_UNUSED int nvol_cpu_avx2_pclmul(void) {   /* no kernel dispatches on it yet */
    int v = nvol_cpu_avx2_pclmul_cache;
    if (v < 0) { v = nvol_cpu_avx2_pclmul_detect(); nvol_cpu_avx2_pclmul_cache = v; }
    return v;
}
""".strip("\n")

ARM_PRELUDE = r"""
#include <arm_neon.h>
""".strip("\n")

ARCH_GUARD = {"x86_64": "defined(__x86_64__) || defined(_M_X64)",
              "aarch64": "defined(__aarch64__) || defined(_M_ARM64)"}
ARCH_PRELUDE = {"x86_64": X86_PRELUDE, "aarch64": ARM_PRELUDE}


def _cpp(prelude: str) -> str:
    """The C prelude for the C++ header. Every helper there is `static` in C; in C++ the
    kernels and wrappers are `inline` (external linkage, one definition across units), and an
    inline function that calls a `static` one has a different definition in every unit (an
    ODR violation), so the helpers, the CPU checks and their caches become `inline` too."""
    out = []
    for line in prelude.splitlines():
        if line.startswith("#  define NVOL_INLINE static "):
            line = "#  define NVOL_INLINE " + line[len("#  define NVOL_INLINE static "):]
        elif line.startswith("static "):
            rest = line[len("static "):]
            line = rest if rest.startswith(("inline ", "__forceinline ")) else "inline " + rest
        out.append(line)
    return "\n".join(out)


def _asm(prelude: str) -> str:
    """The x86 prelude when the kernels are assembly (`asm_kernels`): the resolvers in the
    assembly call the cached CPU checks, so they get external linkage."""
    out = []
    for line in prelude.splitlines():
        if line.startswith(("static inline int nvol_cpu_", "static inline NVOL_UNUSED int nvol_cpu_")):
            decl = line.split("{")[0].replace("static inline ", "").replace("NVOL_UNUSED ", "").rstrip()
            out += [decl + ";", decl + " {"]
            continue
        out.append(line)
    return "\n".join(out)


LICENSE = ("/* Generated by NVOL (Native Vector Operations Library). Do not edit.\n"
           " * SPDX-License-Identifier: MIT OR Apache-2.0 */")


class CPrinter(Printer):
    lang = "c"

    def __init__(self, module, cpp: bool = False, asm_kernels: bool = False):
        mods = module if isinstance(module, dict) else {getattr(module, "arch", "x86_64"): module}
        self.mods = mods
        super().__init__(next(iter(mods.values())))     # primary: header + glue
        self.cpp = cpp
        # the kernels of these architectures come from the assembly backend (nvol/asm): C
        # keeps the glue, the CPU checks and the test hook, and declares what the assembly
        # defines. True means x86_64 only (NASM and MASM have no aarch64)
        self.asm = {"x86_64"} if asm_kernels is True else set(asm_kernels or ())
        self.ifuncs: list = []

    def arch_blocks(self, testing_hook: bool):
        """Emit every architecture's functions under its preprocessor guard."""
        primary = self.mod
        for i, (arch, mod) in enumerate(self.mods.items()):
            self.out.append(f"#{'if' if i == 0 else 'elif'} {ARCH_GUARD[arch]}")
            self.mod, self.ifuncs, self._fn_tables_done = mod, [], set()
            if arch in self.asm:
                self.asm_decls(mod, arch)
            else:
                self.out += self.tables() + [""]
                for f in mod.functions:
                    self.function(f)
            if testing_hook:
                self.out += ["#ifdef NVOL_TESTING",
                             "/* test hook: -1 = autodetect, 0 = force the SSE2/SSSE3 paths (no AVX2, no SSE4.2), "
                             "1 = force AVX2, 2 = force SSE4.2 without AVX2 (x86 only) */",
                             "NVOL_API void nvol_test_set_avx2(int v) {"]
                if arch == "x86_64":
                    self.out.append("    nvol_cpu_avx2_cache = v < 0 ? -1 : v == 1;")
                    self.out.append("    nvol_cpu_avx2_pclmul_cache = v < 0 ? -1 : v == 1;")
                    self.out.append("    nvol_cpu_sse42_cache = v < 0 ? -1 : v >= 1;")
                    self.out += [f"    {f.name}_ptr = {f.name}_resolve;" for f in self.ifuncs]
                else:
                    self.out.append("    (void)v;")
                self.out += ["}", "#endif"]
        self.out += ["#else", '#  error "NVOL: unsupported architecture (x86_64 and aarch64 are generated)"',
                     "#endif", ""]
        self.mod = primary

    def ty(self, t):
        if t is ir.VEC:
            return self.isa.vec_type["c"]
        return {ir.U8: "uint8_t", ir.U32: "uint32_t", ir.U64: "uint64_t",
                ir.USIZE: "size_t", ir.BOOL: "int", ir.PTR: "const uint8_t *",
                ir.MPTR: "uint8_t *", ir.VOID: "void", ir.VISIT: "const nvol_visitor *"}[t]

    def const(self, c):
        if c.value is None:
            return "NULL" if c.ty is ir.PTR else "SIZE_MAX"
        if c.ty is ir.USIZE:
            return f"((size_t){c.value}u)"
        if c.ty is ir.U64:
            return f"UINT64_C({c.value})"
        return f"{c.value}u" if c.ty is not ir.BOOL else str(c.value)

    def special_bin(self, op, a, b, ty):
        if op in ("max", "min"):
            return f"(({a}) {'>' if op == 'max' else '<'} ({b}) ? ({a}) : ({b}))"
        # unsigned C arithmetic is modular already (operands are >= 32 bits wide)
        return f"(({self.ty(ty)})({a} {dict(wadd='+', wsub='-', wmul='*')[op]} {b}))"

    def table_load(self, table, idx):
        return f"nvol_tbl_{table}[{idx}]"

    def call_table(self, table, idx, args):
        return f"nvol_fntbl_{table}[{idx}]({', '.join(args)})"

    def emit_fn_tables_for(self, f) -> None:
        """Define each function table right before the first function that uses
        it (its entries are earlier, already-defined functions)."""
        done = self.__dict__.setdefault("_fn_tables_done", set())
        for name in self.tables_used(f):
            if name in done:
                continue
            done.add(name)
            fns = self.mod.fn_tables[name]
            f0 = self.mod.fn(fns[0])
            saved, self.isa = self.isa, None
            params = ", ".join(self.ty(p.ty) for p in f0.params)
            ret = self.ty(f0.ret)
            self.isa = saved
            q = "inline const" if self.cpp else "static const"
            self.line(f"typedef {ret} (*nvol_fntbl_{name}_t)({params});")
            self.line(f"{q} nvol_fntbl_{name}_t nvol_fntbl_{name}[{len(fns)}] = {{")
            for fn in fns:
                self.line(f"    {fn},")
            self.line("};")
            self.line()

    def tables(self) -> list:
        out = []
        for name, data in self.mod.tables.items():
            rows = [", ".join(str(v) for v in data[i:i + 16]) for i in range(0, len(data), 16)]
            static = "inline constexpr" if self.cpp else "static const"
            out.append(f"{static} uint8_t nvol_tbl_{name}[{len(data)}] = {{")
            out += [f"    {r}," for r in rows]
            out.append("};")
        return out

    def visit(self, cb, at, mask): return f"({cb}->fn({cb}->ctx, {at}, {mask}) != 0)"
    def e(self, x) -> str:
        # an aligned pointer from the address bits (Printer.aligned): Clang otherwise keeps
        # two induction variables in the loop that walks it (docs/PARITY.md, P5)
        if type(x) in (ir.PtrAdd, ir.PtrSub):
            a = self.aligned(x)
            if a is not None:
                kind, p, m = a
                cast = "uint8_t *" if x.ty is ir.MPTR else "const uint8_t *"
                if kind == "up":
                    return f"(({cast})(((uintptr_t)({self.e(p)}) | (uintptr_t){m}u) + 1u))"
                return f"(({cast})((uintptr_t)({self.e(p)}) & ~(uintptr_t){m}u))"
        return super().e(x)

    def ptr_add(self, p, n): return f"({p} + {n})"
    def ptr_sub(self, p, n): return f"({p} - {n})"
    def ptr_diff(self, a, b): return f"((size_t)({a} - {b}))"
    def ptr_addr(self, p): return f"((size_t)(uintptr_t)({p}))"
    def load8(self, p): return f"(*({p}))"
    def cast(self, x, t): return f"(({self.ty(t)})({x}))"

    # -- statements ---------------------------------------------------------
    def block(self, stmts):
        for s in stmts:
            if isinstance(s, ir.Let):
                ty = self.ty(s.var.ty)
                if ty.endswith("*"):
                    decl = f"{ty}{'' if s.mut else 'const '}{s.var.name}"
                else:
                    decl = f"{'' if s.mut else 'const '}{ty} {s.var.name}"
                self.line(f"{decl} = {self.e(s.value)};")
            elif isinstance(s, ir.Assign):
                self.line(f"{s.var.name} = {self.e(s.value)};")
            elif isinstance(s, ir.While):
                self.line(f"while {self.e(s.cond) if self.e(s.cond).startswith('(') else '(' + self.e(s.cond) + ')'} {{")
                self.depth += 1; self.block(s.body); self.depth -= 1
                self.line("}")
            elif isinstance(s, ir.If):
                c = self.e(s.cond)
                self.line(f"if {c if c.startswith('(') else '(' + c + ')'} {{")
                self.depth += 1; self.block(s.then); self.depth -= 1
                if s.els:
                    self.line("} else {")
                    self.depth += 1; self.block(s.els); self.depth -= 1
                self.line("}")
            elif isinstance(s, ir.Return):
                self.line("return;" if s.value is None else f"return {self.e(s.value)};")
            elif isinstance(s, ir.Store8):
                self.line(f"nvol_store8({self.e(s.ptr)}, (uint8_t)({self.e(s.value)}));")
            elif isinstance(s, ir.Store64):
                self.line(f"nvol_store64({self.e(s.ptr)}, {self.e(s.value)});")
            elif isinstance(s, ir.Store32):
                self.line(f"nvol_store32({self.e(s.ptr)}, {self.e(s.value)});")
            elif isinstance(s, ir.StoreVec):
                self.line(lowering(self.isa, "c", "store_u").format(self.e(s.ptr), self.e(s.value)) + ";")
            else:
                raise TypeError(s)

    def signature(self, f: ir.Function) -> str:
        params = ", ".join(f"{self.ty(p.ty)}{'' if p.ty in (ir.PTR, ir.MPTR, ir.VISIT) else ' '}{p.name}" for p in f.params)
        return f"{self.ty(f.ret)} {f.name}({params or 'void'})"

    def asm_decls(self, mod, arch: str = "x86_64") -> None:
        """What the C side sees of an assembly library: the ifunc caches and resolvers (for the
        test hook); the public functions are already declared in nvol.h. aarch64 has no
        resolvers: each of its ifuncs has a single NEON target."""
        ext = ".s" if arch == "aarch64" else ".asm"
        self.out.append(f"/* kernels, entries and dispatch: nvol_{arch}_<abi>{ext} (NVOL assembly backend) */")
        for f in mod.functions:
            if not f.ifunc or (len(f.ifunc) == 1 and f.ifunc[0][0] is None):
                continue
            self.isa = None
            params = ", ".join(f"{self.ty(p.ty)}{'' if p.ty in (ir.PTR, ir.MPTR, ir.VISIT) else ' '}{p.name}" for p in f.params)
            self.line(f"typedef {self.ty(f.ret)} (*{f.name}_fn_t)({params});")
            self.line(f"extern {f.name}_fn_t {f.name}_ptr;")
            self.line(f"{self.ty(f.ret)} {f.name}_resolve({params});")
            self.ifuncs.append(f)
        self.line()

    def ifunc(self, f: ir.Function):
        """Resolve once, cache in a function pointer (like glibc/memchr ifuncs)."""
        self.isa = None
        if len(f.ifunc) == 1 and f.ifunc[0][0] is None:      # nothing to choose: direct call
            args = ", ".join(p.name for p in f.params)
            if f.doc:
                self.line(f"/* {f.doc} */")
            attrs = "inline " if self.cpp else "NVOL_API "
            self.line(f"{attrs}{self.signature(f)} {{ return {f.ifunc[0][1]}({args}); }}")
            self.line()
            return
        params = ", ".join(f"{self.ty(p.ty)}{'' if p.ty in (ir.PTR, ir.VISIT) else ' '}{p.name}" for p in f.params)
        args = ", ".join(p.name for p in f.params)
        fn_t, ptr, res = f"{f.name}_fn_t", f"{f.name}_ptr", f"{f.name}_resolve"
        static = "inline " if self.cpp else "static "
        self.line(f"typedef {self.ty(f.ret)} (*{fn_t})({params});")
        self.line(f"{static}{self.ty(f.ret)} {res}({params});")
        self.line(f"{static}{fn_t} {ptr} = {res};")
        self.line(f"{static}{self.ty(f.ret)} {res}({params}) {{")
        self.depth += 1
        default = [t for c, t in f.ifunc if c is None][0]
        self.line(f"{fn_t} f = {default};")
        for cond, target in reversed([(c, t) for c, t in f.ifunc if c is not None]):
            self.line(f"if ({self.e(cond)}) f = {target};")
        self.line(f"{ptr} = f;")
        self.line(f"return f({args});")
        self.depth -= 1
        self.line("}")
        if f.doc:
            self.line(f"/* {f.doc} */")
        attrs = "inline " if self.cpp else "NVOL_API "
        self.line(f"{attrs}{self.signature(f)} {{ return {ptr}({args}); }}")
        self.line()
        self.ifuncs.append(f)

    def function(self, f: ir.Function):
        if f.ifunc:
            return self.ifunc(f)
        self.emit_fn_tables_for(f)
        self.isa = self.isa_of(f)
        if f.doc:
            self.line(f"/* {f.doc} */")
        if f.noinline and not f.public:
            attrs = "NVOL_NOINLINE " + ("inline " if self.cpp else "static ")
            if self.isa and self.isa.target["c"]:
                attrs = f"NVOL_TARGET_{self.isa.target['c'].upper().replace('.', '_')} " + attrs
        elif "_entry_" in f.name:      # called through a pointer: keep out of line
            attrs = ("inline " if self.cpp else "static ")
            if self.isa and self.isa.target["c"]:
                attrs = f"NVOL_TARGET_{self.isa.target['c'].upper().replace('.', '_')} " + attrs
        elif f.public:
            attrs = "inline " if self.cpp else "NVOL_API "
            if self.isa and self.isa.target["c"]:
                attrs = f"NVOL_TARGET_{self.isa.target['c'].upper().replace('.', '_')} " + attrs
        else:
            attrs = "NVOL_INLINE "
            if self.isa and self.isa.target["c"]:
                # always_inline too (like Rust's #[inline(always)]): plain `inline` let GCC
                # keep ISA kernels out of line even when a caller with the same target
                # (an entry, or a C++ user calling a kernel directly) wants them inlined
                # (NVOL_INLINE is static in C, an external-linkage inline in C++)
                attrs = f"NVOL_TARGET_{self.isa.target['c'].upper().replace('.', '_')} NVOL_INLINE "
        self.line(attrs + self.signature(f) + " {")
        self.depth += 1
        self.block(f.body)
        self.depth -= 1
        self.line("}")
        self.line()

    # -- files --------------------------------------------------------------
    def emit_c(self) -> dict:
        h = [LICENSE, "#ifndef NVOL_H", "#define NVOL_H", "#include <stddef.h>", "#include <stdint.h>",
             "#ifdef __cplusplus", 'extern "C" {', "#endif",
             "#ifndef NVOL_API",
             "#  if defined(_WIN32) && defined(NVOL_BUILD_DLL)",
             "#    define NVOL_API __declspec(dllexport)",
             "#  else",
             "#    define NVOL_API",
             "#  endif",
             "#endif",
             "#ifndef NVOL_VISITOR_DEFINED",
             "#define NVOL_VISITOR_DEFINED",
             "/* visitor kernels (_visit): fn(ctx, chunk, mask) for every chunk with candidates; non-zero stops */",
             "typedef struct nvol_visitor { int (*fn)(void *ctx, const uint8_t *chunk, uint64_t mask); void *ctx; } nvol_visitor;",
             "#endif", ""]
        for f in self.mod.functions:
            if f.public:
                self.isa = None
                h.append(f"/* {f.doc} */")
                h.append(f"NVOL_API {self.signature(f)};")
        h += [""] + iters.c_header(self.mod) + byteset_header(self.mod) + finder.c_header(self.mod) + packed.c_header(self.mod)
        h += ["#ifdef __cplusplus", "}", "#endif", "#endif /* NVOL_H */", ""]
        self.out = [LICENSE, '#include "nvol.h"', COMMON_PRELUDE]
        for i, (arch, _) in enumerate(self.mods.items()):
            pre = _asm(ARCH_PRELUDE[arch]) if arch in self.asm and arch == "x86_64" else ARCH_PRELUDE[arch]
            self.out += [f"#{'if' if i == 0 else 'elif'} {ARCH_GUARD[arch]}", pre]
        self.out += ["#endif", ""]
        self.arch_blocks(testing_hook=True)
        self.out += iters.c_source(self.mod) + finder.c_source(self.mod) + packed.c_source(self.mod)
        return {"nvol.h": "\n".join(h), "nvol.c": self.text()}

    def emit_cpp(self) -> dict:
        self.out = [LICENSE, "#pragma once", "#include <cstddef>", "#include <cstdint>",
                    "#include <cstring>",
                    "// raw layer (namespace nvol::raw): C++17; safe wrappers (std::span): C++20",
                    "#if __cplusplus >= 202002L || (defined(_MSVC_LANG) && _MSVC_LANG >= 202002L)",
                    "#  define NVOL_CPP_WRAPPERS 1",
                    "#  include <optional>", "#  include <string_view>", "#  include <span>", "#  include <vector>", "#  include <iterator>",
                    "#endif",
                    "#ifndef NVOL_API", "#  define NVOL_API inline", "#endif",
                    _cpp(COMMON_PRELUDE)]
        for i, (arch, _) in enumerate(self.mods.items()):
            self.out += [f"#{'if' if i == 0 else 'elif'} {ARCH_GUARD[arch]}", _cpp(ARCH_PRELUDE[arch])]
        self.out += ["#endif", "", "namespace nvol {", "namespace raw {", ""]
        self.arch_blocks(testing_hook=False)
        self.out += iters.c_header(self.mod) + iters.c_source(self.mod, cpp=True)
        self.out += byteset_header(self.mod) + finder.c_header(self.mod) + finder.c_source(self.mod, cpp=True)
        self.out += packed.c_header(self.mod) + packed.c_source(self.mod, cpp=True)
        self.out += ["}  // namespace raw", "", "#ifdef NVOL_CPP_WRAPPERS"]
        bytes_t = "std::span<const std::uint8_t>"
        for kind, name, n in self.mod.api:
            if kind in ("memmem", "teddy"):
                continue
            short = name.removeprefix("nvol_")
            needles = ", ".join(f"std::uint8_t n{i + 1}" for i in range(n))
            nargs = "".join(f"n{i + 1}, " for i in range(n))
            sig = f"{needles + ', ' if needles else ''}{bytes_t} hay"
            if kind == "count":
                self.out.append(f"inline std::size_t {short}({sig}) {{ return raw::{name}({nargs}hay.data(), hay.size()); }}")
            else:
                self.out.append(f"inline std::optional<std::size_t> {short}({sig}) {{")
                self.out.append(f"    const std::uint8_t *p = raw::{name}_raw({nargs}hay.data(), hay.data() + hay.size());")
                self.out.append("    return p ? std::optional<std::size_t>(static_cast<std::size_t>(p - hay.data())) : std::nullopt;")
                self.out.append("}")
        self.out += [""] + iters.cpp_wrappers(self.mod) + finder.cpp_wrappers(self.mod) + packed.cpp_wrappers(self.mod)
        self.out += ["#endif  // NVOL_CPP_WRAPPERS", "}  // namespace nvol", ""]
        return {"nvol.hpp": self.text()}



def byteset_header(mod) -> list:
    """Sizes a caller needs to allocate the byte-set states (kernels/byteset.py)."""
    if not any(f.name == "nvol_byteset_init" for f in mod.functions):
        return []
    from ..kernels.byteset import CONSTANTS
    return [f"#define NVOL_BYTESET_{n} {v}   /* {doc} */" for n, v, doc in CONSTANTS]

def generate(module, lang: str = "c", asm_kernels=False) -> dict:
    """module: an ir.Module or {arch: ir.Module}. asm_kernels: the C for a library whose
    kernels come from the assembly backend (C only): True for x86_64, or the set of
    architectures."""
    # the asm-only parts belong to the asm: dropped wherever this prints C (in asm_kernels mode,
    # the architectures whose kernels are not asm)
    asm = ({"x86_64"} if asm_kernels is True else set(asm_kernels or ())) if lang == "c" else set()
    if isinstance(module, dict):
        module = {a: m if a in asm else ir.for_languages(m) for a, m in module.items()}
    elif getattr(module, "arch", "x86_64") not in asm:
        module = ir.for_languages(module)
    p = CPrinter(module, cpp=(lang == "cpp"), asm_kernels=asm_kernels if lang == "c" else False)
    return p.emit_cpp() if lang == "cpp" else p.emit_c()
