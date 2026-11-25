# Glossary of algorithms

Every algorithm NVOL supports: the kernels it generates, the techniques inside them, and the
generator's own algorithms (IR, interpreter, dispatch, assembler). Each entry says what it
does, where it lives, what it is ported from or measured against, and where NVOL goes past
its reference (**Beyond the reference**).

**Where it lives.** `main` holds the families of phases 0-5. The simdjson kernels of phase 6
are on `feat/` branches, pushed and not yet merged (they reach `main`, and grepper, with the
maintainer's merge; `docs/PENDING.md`, "Where it stands"). An entry on a branch names it.

**Targets.** Every kernel is generated for each backend (C, C++, Rust, Odin, Python through
the C library, and NVOL's own assembler in NASM, MASM and GNU as syntax) and each ISA:
x86-64 SSE2, SSSE3, SSE4.2, AVX2 (with the BMI1 and PCLMULQDQ variants where a kernel uses
them), aarch64 NEON, and scalar. Each one is checked against the interpreter (section 7).

## 1. Byte search: memchr's family (memchr 2.8.3)

| Algorithm | What it does | Where |
|---|---|---|
| **memchr, memchr2, memchr3** | The first position of one, two or three bytes: one unaligned chunk at the edge, then an aligned main loop unrolled 4x (one needle) or 2x (two or three), whose compare vectors are OR-ed so one movemask is taken per iteration, and an overlapping unaligned load for the tail instead of a byte loop | `nvol/kernels/memchr.py`, `main` |
| **memrchr, memrchr2, memrchr3** | The same backwards. In NVOL's assembler, long searches first run the loop in the exit-first shape (the test at the top, as Clang lays out a long loop), then the rotated one | `nvol/kernels/memchr.py`, `main` |
| **SWAR memchr** | For 1-15 bytes, where a vector does not fit: the bytes of a 64-bit word compared at once with the "has zero byte" trick against the splatted needles. **Beyond the reference:** memchr runs a byte loop there (PENDING item 28) | `nvol/kernels/memchr.py`, `main` |
| **Iterators** | `memchr_iter` and `memrchr_iter` for one to three needles, double-ended (forward and backward over the same matches), with `size_hint` and a `count()` that calls the count kernel, as memchr's `Memchr{,2,3}` | `nvol/backends/iters.py`, `main` |
| **count (SAD)** | The number of occurrences of a byte: `0 - cmpeq` (one per match) summed in `u8` lanes for up to 63 unrolled iterations, then folded with `psadbw` into `u64` lanes; one horizontal reduction per ~8 KiB instead of a movemask and popcount per vector; the tail one overlapping load with its mask shifted. **Beyond the reference:** 2-3.5x memchr's `memchr_iter().count()` | `nvol/kernels/count.py`, `main` |
| **count_parity** | memchr's `count_raw` as it is (popcount of each vector's movemask), kept for the benchmarks | `nvol/kernels/count.py`, `main` |
| **first_non_ascii** | The index of the first byte at or above 0x80 (bstr 1.13's): a movemask of each vector, since the high bit is the test | `nvol/kernels/ascii.py`, `main` |

## 2. Substring search: memmem (memchr 2.8.3 `memmem::Finder`)

| Algorithm | What it does | Where |
|---|---|---|
| **Searcher choice** | As `memmem/searcher.rs`: an empty needle matches at 0; one byte is memchr; 2-32 bytes is the packed pair (Rabin-Karp when the haystack is shorter than the pair finder needs); longer needles are Rabin-Karp under 16 bytes of haystack, else Two-Way with the packed pair as its prefilter | `nvol/kernels/memmem.py`, `main` |
| **Packed pair** | Two bytes of the needle, the rarest by a byte-frequency table, compared at their offsets in each vector (SSE2, AVX2, NEON); both masks ANDed give the candidates, verified against the whole needle | `nvol/kernels/memmem.py`, `main` |
| **Byte rank** | memchr's table of how common each byte is in typical text (`default_rank.rs`, copied as it is): the packed pair takes the two of lowest rank | `nvol/kernels/rank.py`, `main` |
| **Two-Way** | Crochemore and Perrin's algorithm for long needles: the critical factorization of the needle (maximal suffixes, two orderings), then a scan that never backs up more than the period; linear time, constant space | `nvol/kernels/memmem.py`, `main` |
| **Prefilter state** | memchr's `PrefilterState`: the packed-pair prefilter in front of Two-Way switches itself off when, after 50 tries, it skips fewer than 8 bytes a try on average | `nvol/kernels/memmem.py`, `main` |
| **Rabin-Karp** | A rolling hash of the needle's length over the haystack, compared before the bytes; for short haystacks | `nvol/kernels/memmem.py`, `main` |
| **Finder state** | The needle's precomputed state (rare bytes, offsets, factorization, hash) in little-endian `u64` words, written in one pass by the generated `nvol_memmem_init`; every language's glue only owns the array, and no language reimplements the construction | `nvol/backends/finder.py`, `main` |

## 3. Multiple literals: Teddy (aho-corasick 1.1.5 `packed::Searcher`)

| Algorithm | What it does | Where |
|---|---|---|
| **Teddy** | Up to 64 literals searched at once: the literals put in buckets; for each of the first `m` bytes (m = min(4, shortest literal)), the low and high nibble of every haystack byte looked up with `pshufb` in two tables of bucket bits; the lookups ANDed across the `m` positions give, per haystack position, the buckets that may match there; those are verified against their literals, leftmost-first | `nvol/kernels/teddy.py`, `main` |
| **Slim and Fat** | Slim: 8 buckets, SSSE3 (128 bits) or AVX2 (256 bits), for up to 32 literals. Fat: 16 buckets on AVX2, each 128-bit half of a vector serving 8, for more | `nvol/kernels/teddy.py`, `main` |
| **Builder's refusals** | No Teddy, exactly as aho-corasick refuses one: no literals, an empty one, more than 64, mask length 1 with more than 16, or no SSSE3 | `nvol/kernels/teddy.py`, `main` |
| **Packed Rabin-Karp** | aho-corasick's fallback for haystacks shorter than `16 + m - 1`: 64 buckets over the hash of each literal's first `minlen` bytes, scanned in literal order (it cut a 16-byte search from 3452 to 415 instructions, PENDING item 27) | `nvol/kernels/teddy.py` (`slow`), `main` |
| **Teddy state** | Masks, bucket lists and the Rabin-Karp tables written once by `nvol_teddy_init` into a byte array the glue owns; the variant chosen through a function table in the IR. **Beyond the reference:** construction 7-23x faster | `nvol/backends/packed.py`, `main` |

## 4. Byte sets (ugrep 7.8.5, rapidjson)

| Algorithm | What it does | Where |
|---|---|---|
| **Nibble lookup membership** | The first byte in a set of any size: Wojciech Muła's SIMD byte lookup, two `pshufb` (or NEON `tbl`) over the low nibble with the high bit kept, picking the table for bytes below or above 0x80, against a bit for the high nibble. The cost per vector does not grow with the set. **Beyond the reference:** ugrep tests N bytes with N `cmpeq` and `or`; equal or better for N = 2-16, up to 2.9x | `nvol/kernels/byteset.py`, `main` |
| **Unique-nibble sets** | When every member has its own low nibble (the JSON whitespace ` \t\n\r`): one `pshufb` and one compare are exact | `nvol/kernels/byteset.py`, `main` |
| **Range sets** | A low range `[0, T]` plus up to three other bytes (JSON's string stop set: control bytes, `"`, `\`): an unsigned `min` and a few compares, as rapidjson's and simdjson's own loops | `nvol/kernels/byteset.py`, `main` |
| **find_not** | The first byte outside a set (whitespace skipping) | `nvol/kernels/byteset.py`, `main` |
| **Pair of sets** | The first position where one set matches at one offset and another at a second (ugrep's `pinN_pma`, a packed pair over sets), with a candidate mask | `nvol/kernels/byteset.py`, `main` |
| **copy_until_byteset** | Copy bytes until the first member of a set (rapidjson's string parsing), unrolled `k1`-`k6` and the range forms | `nvol/kernels/byteset.py`, `main` |

## 5. JSON (simdjson 5.2.0), on the phase 6 branches

| Algorithm | What it does | Where |
|---|---|---|
| **64-bit block masks** | One `u64` per 64-byte block, a bit per byte, from two AVX2 or four SSE/NEON vectors (`bmask64x2`, `bmask64x4`); `popcnt64`, `blsr` (`x & (x - 1)`), the sign smear | `feat/ir-mask64` (item 33) |
| **prefix_xor** | Bit `i` of the result is the XOR of bits `0..i`: on AVX2 a carry-less multiply (`pclmulqdq`) by all ones, as simdjson's haswell; elsewhere six shift-and-XOR steps, as its arm64 and fallback | `feat/clmul` (item 34) |
| **Character classification** | Per block, the masks of whitespace and of `{}[]:,` with one `pshufb` and compare per class (simdjson's two nibble tables, its `| 0x20` folding `[` into `{`), and the masks of `"` and `\` | `feat/json-stage1` (item 35) |
| **Escape scanner** | Which quotes a backslash escapes: the odd-length runs of backslashes found with shifts, AND, OR, ANDNOT and an addition whose carry crosses each run; the state carried to the next block | `feat/json-stage1` (item 35) |
| **String scanner** | Which bytes are inside strings: `prefix_xor` of the unescaped quotes, carried across blocks | `feat/json-stage1` (item 35) |
| **Structural indexer** | The structural bits of each block to a list of `u32` positions: `popcnt64` for the count, then `tzcnt` and `blsr` unrolled, written in steps of 4, each block written while the next is scanned, two blocks per iteration; against simdjson's own stage 1: citm_catalog -6.7 %, twitter -16.4 % (with UTF-8 validation on both, -0.4 % and -0.2 %) | `feat/json-stage1` (item 35) |
| **UTF-8 validation (lookup4)** | Keiser and Lemire's algorithm: per vector, the previous one to three bytes shifted in (`prev<1..3>`), three 16-entry lookups (`check_special_cases`), saturating subtractions for the 3- and 4-byte sequences (`check_multibyte_lengths`), ASCII blocks skipped; a scalar validator for the tail. The same set Python's strict decoder accepts (RFC 3629) | `feat/utf8-validation` (item 36) |
| **Eight-digit parsing** | The value of eight ASCII digits: on SSE4.1 `pmaddubsw`, `pmaddwd`, `packusdw`, `pmaddwd` with an 8-byte load (simdjson loads 16, and splits a cache line once in eight numbers: -3.3 % in westmere); on others three SWAR multiplications. And the SWAR test that eight bytes are digits | `feat/json-numbers` (item 37) |
| **copy_and_find** | Copy 32 bytes of a string and return the masks of `\` and `"` in them (simdjson's `backslash_and_quote`) | `feat/json-stage2-strings` (item 37) |
| **SSE4.2 stage 1** | A westmere-class stage 1 (SSE4.2, POPCNT, PCLMULQDQ) before the SSSE3 one; `tzcnt64` without BMI1 as `bsf` plus `cmovz` instead of a branch per slot, and `ctz64_lax` for the indexer's slots; against simdjson's westmere: twitter -2.9 %, citm_catalog -2.0 % | `feat/json-stage1-sse` (item 37b) |

## 6. Dispatch

| Algorithm | What it does | Where |
|---|---|---|
| **Runtime dispatch** | memchr's: for a public operation, an SSE2 entry (under 16 bytes scalar, else SSE2) and an AVX2 entry (under 16 scalar, under 32 SSE2, else AVX2) compiled with the AVX2 target so every callee inlines into it; an ifunc resolved once into a cached function pointer; and the index API on top | `nvol/kernels/dispatch.py`, `main` |
| **CPU checks** | `cpuid` for SSSE3, SSE4.2, AVX2, BMI1, POPCNT, PCLMULQDQ (`cpu_avx2_pclmul`, `cpu_sse42_pclmul`); a test hook forces a lower ISA or the scalar paths | backends, `main` and branches |

## 7. The generator

| Algorithm | What it does | Where |
|---|---|---|
| **IR** | Kernels are written once as typed IR (vectors, masks, pointers, `While`, `If`, let-bound values, primitives) and lowered to every backend; no backend has hand-written kernel code | `nvol/ir.py`, `nvol/primitives.py`, `main` |
| **Reference interpreter** | Runs the IR with strict semantics: every pointer formed must stay in its object (one past the end allowed), every load in bounds and aligned loads aligned, unsigned underflow an error, primitive preconditions asserted. A kernel that passes it against the oracle is correct; the backends are then checked against it differentially | `nvol/interp.py`, `nvol/oracle.py`, `main` |
| **Lowering to machine IR** | Inlining, common subexpressions (per statement, and across statements by variable versions and a memory epoch), rotated loops (the test at the bottom), conditions straight into compare-and-branch, every integer zero-extended in 64 bits (casts free), cold and rare paths marked for layout | `nvol/asm/lower.py`, `nvol/asm/x86.py`, `nvol/asm/a64.py`, `main` |
| **Register allocation** | Target-neutral allocation over live intervals with holes (a value holds its register only in the segments, per block, where it is live, so values whose segments interleave share one), reads and writes of an instruction at separate positions, fixed registers where the code names them (arguments, return value), spills with shared slots, reloads forwarded into labels that only forward jumps reach, constants rematerialized instead of reloaded; then the stack frame. Not done: shrink-wrapping (pushing callee-saved registers after the early exits), a known cost at 16 bytes (PENDING item 17) | `nvol/asm/regalloc.py`, `main` |
| **Peepholes** | Target rewrites after allocation, such as `blsr` for `x & (x - 1)` in BMI1 functions | `nvol/asm/x86.py`, `main` |
| **Encoding and branch relaxation** | The length of every instruction's shortest encoding, every jump's size and every loop head's alignment decided by NVOL and spelled out in the output, so NASM, MASM and GNU as give the same bytes (checked); the 2-byte VEX store form where the assemblers would not pick it | `nvol/asm/encode.py`, `nvol/asm/samebytes.py`, `main` |
| **ABIs** | Win64 and System V on x86-64, AAPCS64 on aarch64: argument registers, callee-saved registers, shadow space, unwind information | `nvol/asm/abi.py`, `main` |
