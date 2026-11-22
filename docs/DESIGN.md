# NVOL — design

**NVOL (Native Vector Operations Library)**: specify a low-level operation
once, generate equivalent implementations for several languages and
architectures, and **prove with tests** that they all meet the same contract.

## Principles

1. **Semantic correctness before syntactic resemblance.** The source of truth is
   the semantics (primitives + oracle), never the generated code.
2. **Verify instead of trust.** No backend counts as good without passing the
   differential tests; "it compiles" is not a status.
3. **Explicit contracts.** Each primitive declares its preconditions (alignment,
   mask ≠ 0, …); the interpreter checks them.
4. **Performance measured, not assumed.** Each kernel is compared against the
   industry reference implementation (today: `memchr` 2.8.3).
5. **Two references.**
   - **External:** the implementation that NVOL replaces. Each backend ends up equal or better.
   - **Internal:** the in-house assembly (`nvol/asm/`: NASM, MASM, GNU as), mature since
     2026-11-22 (PENDING item 18).
     It does not go through any compiler, so it separates generation problems from the
     problems of each language's toolchain. Each language is compared against it, and every
     difference, above or below, is explained in the parity log. NVOL can also fix and patch
     a low-level problem on its own, without depending on the language maintainers.
   - Its own bar was the best of GCC, Clang and MSVC on NVOL's C. At maturity every row is
     equal or better in instructions, or a listed deviation with its measured cause
     (`docs/PENDING.md`, item 17).

## Architecture

```
 nvol/primitives.py   primitive catalog: signature + contract + semantics in Python
        │
 nvol/ir.py           minimal typed IR: statements Let/Assign/While/If/Return/Store8/Store32/Store64/StoreVec;
                      expressions Var/Const/Bin/Not/Cast, pointers (PtrAdd/PtrSub/PtrDiff/PtrAddr/Load8),
                      Prim, Call, CallTable, TableLoad and Visit; pointers to u8 and vectors
        │   ▲
        │   └── nvol/kernels/*.py  algorithms written in Python with the Builder
        │                          (parameterized by ISA: SSE2/SSSE3/SSE4.2=16B, AVX2=32B, NEON=16B)
        ├──────────────► nvol/interp.py   strict reference interpreter (detects UB)
        │                       │
        │                nvol/oracle.py   "obvious" executable specification ─┐
        │                                                                     │ differential
        ├──► nvol/backends/{c,rust,python,odin}.py ──► generated/<lang>/ ─────┤ tests
        │    (c.py emits C and C++)                                           │
        │    nvol/isa.py: lowering primitive → intrinsic per (ISA, language)  │
        └──► nvol/asm/ (lower → regalloc → printer) ──► generated/{nasm,masm,gas}/ ┘
             in-house x86-64 and A64 instructions; the glue stays in C (c.py, asm_kernels)
```

### Three levels of verification

| Level | What it compares | Where |
|---|---|---|
| 1. Algorithm | interpreted IR vs oracle, with UB checking (out-of-range pointers, misaligned loads, underflow) | `tests/test_interp.py`, `python -m nvol check` |
| 2. Backend | compiled generated code vs oracle or naive search: lengths 0..260 × 64 alignments × match positions, through the three x86 paths forced with the test hook (base, AVX2, SSE4.2 without AVX2) and on aarch64 under QEMU (emulated: correctness only) | `tests/` (C via Python: `test_differential`, `test_byteset`, `test_memmem`, `test_teddy`; each lowering in `test_prims`), `harness/rust`, `harness/odin/tests`, `harness/cpp/smoke.cpp` |
| 3. Parity | generated code vs the `memchr`/`aho-corasick` crates (results and performance), and real programs built on NVOL (ripgrep, ugrep, rapidjson) | `harness/rust` (`cargo test`, `cargo run --bin bench`), `harness/*-e2e` |

`harness/verify.sh` runs levels 1 and 2 in full (plus the build of the C++ header and of the
aarch64 C/C++ with `-Werror`) and fails if any step fails. `tests/test_generated_sync.py`
checks that `generated/` is exactly what the generator writes, because the Rust and Odin
harnesses and the e2e builds compile the committed files.

States of an implementation: `untested` → `tested` (levels 1-2) → `parity`
(level 3 without regression) → `verified` (future: equivalence with SMT/bitvectors
for primitives).

### Why an in-house IR and not "text templates"

The same tree feeds the interpreter and every backend, so the algorithm
is tested **once**, independently of the compiler. A backend can only
go wrong in the *lowering*, which is small and tested differentially.
Adding a language = writing a printer (~150 lines) + the lowering table.

### IR conventions

- One `Module` per architecture (`build_modules()` → `{x86_64, aarch64}`) with the same
  public API. The backends emit all of them in the same file, each under its own
  guard (`#if defined(__aarch64__)`, `#[cfg(target_arch = "aarch64")]`). On
  aarch64 NEON is the base: there is no runtime detection.
- The shape of the `movemask` mask is part of the ISA (`mask_bits`,
  `lane_bits`): x86 = u32 with 1 bit per lane, NEON = u64 with 4 bits per lane
  (memchr's `vshrn` trick). The kernels scale lane counts with
  that, and the reference semantics of each primitive depends on the active ISA.
- The kernels use the `memchr::find_raw` signature: `(needles…, start, end)`, and
  return a pointer to the match or `NULL` (`count` returns an integer). Working
  with pointers end to end avoids converting index↔pointer on each call.
  That conversion cost ~10 % when iterating (see `docs/PENDING.md`, "Done").
- Each public operation `P` has three layers: `P_entry_<isa>` (dispatch
  by length, compiled with the target of its ISA: `sse2`, `avx2`, `sse42` in byteset, `neon`),
  `P_raw(…, start, end)` (ifunc: a function pointer resolved once) and
  `P(…, start, len) -> index` (`NONE` = `SIZE_MAX`). The safe wrappers convert it to
  `Option`/`std::optional`/`None`.
- An IR pass (`prune_unused_lets`) removes `let`s that nobody reads; all IR
  expressions are pure.
- Variable names never repeat within a function and are never
  reserved words of any target language (`ir.RESERVED`).
- Byte sets (`kernels/byteset.py`): exact membership with two `pshufb`/`tbl` (Muła's
  method), constant cost for any set size. Sets of 1-6 bytes use `cmpeq`
  chains (1-3: the `memchr` family, unrolled). The `_mask` variant returns the block and
  all its candidates (one bit per position on x86, bit 4i+3 on NEON) so that the caller can walk
  them without re-entering. If each member has its own low nibble (JSON whitespace,
  digits), one `pshufb` + `cmpeq` against a 16-byte table is enough, and that strategy beats
  the other two. If the set is a low range `[0, T]` plus up to 3 bytes (the set of JSON
  strings: `0x00-0x1F`, `"` and `\`), `min(x, T) == x` (primitive `vmin8`) plus one `cmpeq`
  per extra byte costs less than the nibble search. This is what the hand-written loops of
  rapidjson and simdjson do (variants `_r0`…`_r3`). `find_not_byteset`
  searches for the first byte outside the set with the same kernels and the comparison inverted.
  On x86, the chains and the 16-byte ranges use only SSE2 (`cmpeq`, `pminub`), so they are
  emitted with the `sse2` ISA, without a `target` attribute. They can be inlined into code compiled
  for base x86-64, and the non-AVX2 entry also uses them: before, it fell back to scalar on those
  sets. The nibble searches stay on SSSE3.
- ISA `sse42` (implies SSSE3) and primitive `skip_set16`: one `pcmpistri` (equal any,
  negative polarity, implicit lengths) that gives the first byte of a 16-byte vector outside a
  list of up to 16 members. The byteset class (112 bytes) stores that list when the
  set has 1-16 members and does not include 0, and `nvol_find_not_byteset_sse42` walks it:
  one load to set up and one instruction per 16 bytes. This is what rapidjson's whitespace
  skipping does with SSE4.2. It pays off on short runs, where the setup of the
  other kernels weighs more than the walk. Only the negated search exists: with implicit
  lengths a 0 in the text cuts the comparison short. That is correct for "outside a set
  without 0" but would hide members in the positive search. It is used by the non-AVX2 x86
  entry when the CPU has SSE4.2 (`nvol_cpu_sse42`) and, inline, by code compiled with SSE4.2.
- Dispatch (`ifunc`) with several conditions: the first one that holds wins (AVX2, then
  SSE4.2, then the base), the same in the interpreter and in the four backends. The test
  hook `nvol_test_set_avx2` accepts 0 (base: neither AVX2 nor SSE4.2), 1 (AVX2) and 2 (SSE4.2
  without AVX2).
- `StoreVec` (unaligned vector store) and `nvol_copy_until_byteset`: copies to `dst`
  while it searches and returns the first member or `end`, in a single pass (the string copy loop
  of a JSON parser or writer). It writes whole vectors: `dst` needs room for
  `end - start` bytes, and whatever follows the copied prefix is garbage; no overlap allowed.
- Kernels with a *visitor* (`Visit`, parameter `VISIT`): instead of returning the first block with
  candidates, the kernel passes each one to the caller (block base and mask) and continues while
  the visitor returns false. If the kernel is inlined, the compiler fuses the caller's
  per-candidate work (in ugrep, `predict_match`) with the kernel loop. A kernel that returns
  for each block pays its setup (selector, `broadcast`s) on each re-entry, and that cost
  up to 10 % when most candidates are discarded. In C/C++ it is `const nvol_visitor *`
  (`{fn, ctx}`, which GCC turns into a direct call and inlines); in Rust,
  `&mut impl FnMut(*const u8, u64) -> bool`; in Odin, `^Visitor`; in the interpreter, a Python
  function.
- The unaligned loads and stores of the C prelude use `__builtin_memcpy`, not `memcpy`. The
  *fortify headers* (musl on Alpine, `_FORTIFY_SOURCE` on glibc) wrap `memcpy` in a function
  with overlap checks that ended up inside the loop of each kernel.
- `noinline` marks large kernels that a dispatcher picks: each one has its
  own prologue (on Win64 saving `xmm6-15` is costly) instead of bloating the
  caller's.
- `CallTable(table, index, args)`: an indirect call through a table of
  module functions (same signatures). Teddy stores the index of the variant at
  construction, and each search pays a single indirect call, like a `dyn`.
- `tests/test_prims.py` tests each vector lowering against its reference
  semantics, isolated from the rest.
- Variable names are unique per function: the backends can declare them
  at any depth even if the language does not allow *shadowing* (Odin).
- No division: the kernels use shifts.
- Normal arithmetic cannot overflow (the interpreter treats it as UB); modular
  arithmetic is explicit (`wadd`, `wsub`, `wmul`, `<<` discards bits), as in the
  Rabin-Karp hash.
- Constant 256-byte tables (`Module.tables`, `TableLoad`), e.g. the memmem
  rarity table.
- The search kernels are read-only. The precomputed state of a
  searcher (the memmem `Finder`) is a block of `u64` words that the kernel
  reads with `load64`. A construction function (`nvol_memmem_init`) writes it in one pass
  with `Store64` through a mutable pointer (`MPTR`). The
  other IR writes are `Store8` and `StoreVec` (the copy of
  `nvol_copy_until_byteset`), always through an `MPTR`. The interpreter models several memory
  regions (haystack, needle, state), with writes allowed only where they belong.

## Backends

| Language | Output | Notes |
|---|---|---|
| C99 | `nvol.h` + `nvol.c` | GCC/Clang (`target("avx2")`); branches for MSVC (cpuid+xgetbv): the C++ header compiles with `cl /W4 /WX`, the C and the tests with MSVC are untested (PENDING 2.4) |
| C++17/20 | header-only `nvol.hpp` | `nvol::raw` layer from C++17 (inlineable: ugrep calls the `_entry_avx2` entries); `std::span` / `std::optional` from C++20; everything `inline` with external linkage (a single definition across translation units) |
| Rust | `nvol.rs` (module) | `#[target_feature]`, safe wrappers over `&[u8]` |
| Python | `nvol_ops.py` | ctypes binding over the C lib; no copies for `bytes` and writable buffers (a read-only buffer that is not `bytes` is copied once); `ByteSet` class |
| Odin | `nvol.odin` (package `nvol`) | `#simd[N]u8`, portable intrinsics + LLVM via `foreign`, `when ODIN_ARCH`, `(index, ok)` API |
| NASM (x86-64) | `nvol_x86_64_win64.asm`, `nvol_x86_64_sysv.asm` + `nvol.c` (glue) + `nvol.h` | see below |
| MASM (x86-64) | `nvol_x86_64_win64.asm` + `nvol.c` (glue) + `nvol.h` | `ml64`, Win64 only; the same MIR as NASM |
| GNU as (x86-64) | `nvol_x86_64_win64.s`, `nvol_x86_64_sysv.s` + `nvol.c` (glue) + `nvol.h` | AT&T; the same MIR; COFF and ELF |

### In-house assembly (`nvol/asm/`)

The IR is lowered to instructions with no compiler in between, in three layers so that each new
syntax (MASM, GNU as) is only a printer:

- **`lower.py`, IR → MIR** (instructions on virtual registers), the target-neutral part:
  inlining, CSE, the statement and loop structure, the cold and rare markers. A target
  selects the instructions: **`x86.py`** for x86-64 and **`a64.py`** for aarch64 (A64 and
  NEON). Each also gives the allocator its scratch registers, spill code, peephole and frame.
  Every integer, pointer
  and bool lives zero-extended to 64 bits: `Cast`s are free and only `wadd`/`wsub`/`wmul`/`shl`
  on u8/u32 truncate. `While` loops are rotated (test at the bottom) and conditions are fused into
  `cmp`/`test` + jump. Inlining follows the C rule (what is `NVOL_INLINE` in C); an AVX2
  function encodes the SSE kernels it inlines as VEX. Vector primitives are two-operand (SSE)
  or three-operand (VEX) templates.
- **`regalloc.py`**: allocation in start order, with intervals with holes (one segment per block
  where the value is live) and fixed ranges for the registers the ABI names
  (arguments, `rax`, the `rcx` of shifts, whatever each call clobbers), spills to
  stack slots, and the frame (used callee-saved registers, shadow space, xmm6-15 on Win64). A
  spilled value is split in the single-entry regions that only read it (loops, branch prefixes
  and the code after its definition): there a copy takes a free register.
- **`encode.py`** computes the length of the shortest encoding of each instruction and decides the
  size of each jump (relaxation: all short, the ones that do not reach grow). This way NVOL
  decides the size, not each assembler.
- **`abi.py`** describes System V and Win64; **`nasm.py`**, **`masm.py`** and **`gas.py`** (AT&T)
  print the same list, so the syntaxes are a single implementation.
  - All three write the jump sizes that `encode.py` decided. A loop head is
    aligned with NOPs that NVOL writes, and an assembler that relaxed on its own would shrink a
    jump that NVOL left long to keep it aligned.
  - Loop exits (an `if` whose body ends in `return`) are lowered in place,
    marked. After register allocation, `regalloc.layout_cold` moves them before the
    loop head and inverts the jump, as GCC and Clang do.

  `tests/test_asm_syntaxes.py` checks that they give the same code:
  - NASM and GNU as give the same bytes;
  - MASM gives the same instructions and lengths, and can only change the direction of a
    register-to-register instruction, which `ml64` picks on its own;
  - the lengths from `encode.py` match those of GNU as across the whole library. In ELF each
  global symbol has a local alias (nothing depends on interposition); on Win64 the public ones
  are exported with `-export:` in `.drectve`. Both `ld` and `link.exe` read that; MASM's
  `PROC EXPORT` writes `/EXPORT:`, which `ld` ignores. MASM requires the size of memory operands
  that NASM deduces from the register (`ymmword ptr`).

**aarch64** (`a64.py`, `gas_a64.py`, AAPCS64 in `abi.py`):
- **Instructions:** every one is 4 bytes, so there is no relaxation. `a64.check_ranges` makes a
  branch out of reach a generation error (b.cond, cbz and cbnz reach ±1 MiB; tbz ±32 KiB).
  Immediates come from `movz`/`movn`/`movk`/`orr` (`mov_parts`, with the logical-immediate
  check), symbols from `adrp` + `:lo12:`, and conditions from `cmp` + `b.cc`,
  `cbz`/`cbnz`/`tbz`/`tbnz`, `csel` and `cset`.
- **NEON:** the primitives are the C backend's (`cmeq`, `orr`, `and`, `sub`, `umin`, `tbl`,
  `ext`, `ushr`, `sshr`, `uaddlp`, `addp`, `umaxp`, `dup`). `movemask` is `shrn #4` and a mask
  `0x8888…`, so position i is bit 4i+3, as `nvol.h` documents.
- **Frame:** x29/x30 are saved only when the function calls. The used registers of x19-x28 and
  d8-d15 are saved (AAPCS64 keeps only the low 64 bits of v8-v15, so a call clobbers every
  vector), then the 16-byte spill slots. The frame is one `sub sp`, with `.cfi_*` directives.
- **No resolver:** NEON is the baseline, and each public function with a single target is one
  `b` to it.
- **One reference:** `samebytes aapcs64` checks that GNU as and Clang give the same bytes, the
  same unwind rules and the size `code_size` predicts for each function. The encoding of
  `.eh_frame` is the assembler's choice: GNU as writes the CIE with factors 4/-8 and Clang
  with 1/-4.
- **Tests:** `harness/asm/run-a64.sh` runs `test_asm` and the native suites under QEMU. The
  Python and pytest of the aarch64 sysroot run there, and the native Clang cross-compiles
  inside the tests. QEMU says whether the code is correct, not how fast it is.

Dispatch is the same as in C: a pointer in `.data` that starts at a resolver, the public
symbol jumps through it, and the resolver chooses with the C `nvol_cpu_*` functions. The library
is the `.asm` for the ABI plus `generated/nasm/nvol.c` (the C in `asm_kernels` mode: glue, CPU
checks, test hook); `nvol.h` is the same as the C one. Tests: `tests/test_asm.py` (each function
against the interpreter, through a trampoline that checks the ABI) and the whole library pytest
on the `asm` backend of the `native` fixture.

## License

NVOL, its specifications, tests and generated code are dual-licensed MIT OR Apache-2.0.
Apache-2.0 includes an express patent grant. Third-party notices are in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
