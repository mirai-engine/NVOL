# ugrep end to end on NVOL — i5-7400 (AVX2), WSL2 Alpine 3.24 (Linux)

ugrep v7.8.5 (`f2cc252`), Alpine 3.24 g++ (musl), `-O2 -std=gnu++17` for both versions. The same
commit is compiled twice: the original version and one with
`harness/ugrep-e2e/ugrep-7.8.5.patch`. The patch connects all of ugrep's byte search to the C++
header that NVOL generates (`generated/cpp/nvol.hpp`, header-only).

```
sh harness/ugrep-e2e/setup.sh <ugrep repo>     # in WSL; TEST=1 also runs the ugrep suite
python3 harness/ugrep-e2e/bench.py --stock .../ugrep-stock/src/ugrep --nvol .../ugrep-nvol/src/ugrep \
    --en en64.txt --code code64.rs --rounds 41
sh harness/ugrep-e2e/setbench.sh                  # byte sets: NVOL vs ugrep's technique
sh harness/ugrep-e2e/resume-check.sh <original ugrep> <NVOL ugrep> en64.txt code64.rs   # -o/-n, file and pipe
g++ -O2 -std=gnu++17 -Igenerated/cpp harness/ugrep-e2e/restartbench.cpp -o restartbench  # isolated loop
```

## What was replaced

Per-routine detail in [UGREP_SIMD_INVENTORY.md](../UGREP_SIMD_INVENTORY.md).

| ugrep routine | Now | Phase |
|---|---|---|
| libc `memchr` (44 sites: end of line, binaries, `advance_char`) | `nvol_memchr_raw` | direct |
| `nlcount` (line numbers; `popcount` of the mask) | `nvol_count_raw` (SAD) | direct |
| `simd_advance_string_avx2`, `simd_advance_chars_avx2<2,3>` (literals) | `nvol_memmem_find_entry_avx2` with a `Finder` built once per pattern | direct |
| `simd_advance_pattern_pinN_pma_avx2`, N = 1-8, 16 (classes, alternations, `-i`) | set-pair kernels with *visitor* (`predict_match` inside their loop), inlined, chosen with the state's selector | new kernels |
| `simd_advance_pattern_pinN_one_avx2`, N = 4-8, 16 | `nvol_find_byteset_entry_avx2` | new kernels |
| `advance_pattern_pin{2,3}_one` (SSE2 and NEON) | `nvol_memchr2_raw` / `nvol_memchr3_raw` | direct |

Before using the headers, the direct phase was verified by linking the C library with
`-Wl,--wrap` to count the calls. With `-n Sherlock` over 64 MiB there were 438 303 `memchr`,
219 096 `count` and 219 086 `memmem` calls. The same trace showed that, with AVX2, ugrep uses
`pinN_pma` as soon as the pattern is at least one character long: `pinN_one` only runs with
`min = 0` or on SSE2/NEON.

## New kernels: byte sets

`nvol/kernels/byteset.py`. Membership is exact and uses two nibble lookups (`pshufb`/`tbl`,
Muła's method), with a per-vector cost that does not depend on the set size. ugrep uses N `cmpeq`
+ `or`. For small sets, NVOL picks the best technique for each case:

- 1-3 distinct bytes: the `memchr` family, unrolled.
- 4-6: chains of exactly k `cmpeq`.
- 7 or more: nibble lookup.

The `_mask` variant returns the block with all its candidates and how many bytes it covered, so
the caller can walk them without reentering. The one ugrep uses is `_visit`: it passes each block
with candidates to a caller function and continues without returning (see "How the two
regressions were closed").

Microbenchmark (`setbench.c`), against ugrep's technique specialized per N as in its code
(macros, constant N, unrolled loops). GB/s, best of 15 interleaved rounds:

| N | one set, 64 KiB | one set, 1 MiB | pair, 64 KiB | pair, 1 MiB |
|---|---|---|---|---|
| 2 | +16 % | +26 % | +4 % | +4 % |
| 3 | +15 % | +27 % | +7 % | +4 % |
| 4 | +1 % | +1 % | +5 % | +1 % |
| 5 | +1 % | +1 % | +9 % | +2 % |
| 6 | 0 % | −3 % | +3 % | −1 % |
| 7 | +11 % | +16 % | +33 % | +21 % |
| 8 | +25 % | +35 % | +50 % | +39 % |
| 16 | +148 % | +105 % | +190 % | +174 % |

(Texts without matches. With scattered matches, the results are similar: between −2.6 % and
+212 %.) With N = 4-6 both use the same algorithm, so the difference is noise. With large N,
ugrep's technique slows down with each byte it adds and NVOL's does not.

Kernel verification:

- `nvol check`: interpreter against oracle, 1200 cases on x86_64 and 600 on aarch64. It includes
  the mask variant, which must report exactly the block's candidates.
- C via Python (`tests/test_byteset.py`): on the AVX2 and non-AVX2 paths, sweeping alignment and
  positions.
- Rust: native and on aarch64 under QEMU.
- Odin: on both architectures.

## End-to-end correctness

| Test | Original | With NVOL |
|---|---|---|
| ugrep suite (`make test`: `verify.sh` single-threaded and multi-threaded) | ALL TESTS PASSED | ALL TESTS PASSED |
| 18 queries over 64 MiB of English and 64 MiB of Rust code | — | byte-for-byte identical output in all 18 |
| 12 queries with `-o`/`-n` that restart thousands of times, on the file and through a pipe (`resume-check.sh`) | — | byte-for-byte identical output in all 12 |

## Timings

Median of 41 interleaved rounds (ms) and, in parentheses, the minimum. "Paired" is the median of
the per-round ratios. A negative value means NVOL was faster.

This measurement was taken with Windows at 80 % CPU from other processes, so the medians are very
noisy: the control query, which does not go through NVOL, gives −3 %. The minimums and the paired
value are the most reliable.

| corpus | query | path | original | NVOL | median | paired |
|---|---|---|---|---|---|---|
| en | `Sherlock Holmes` | memmem | 30.2 (26.6) | 22.9 (20.0) | −24.2 % | −24.0 % |
| en | `Moriarty` (absent) | memmem | 22.9 (20.0) | 16.1 (14.6) | −29.9 % | −29.2 % |
| en | `-c Watson` | memmem + memchr | 34.7 (31.1) | 26.6 (24.3) | −23.4 % | −21.5 % |
| en | `-n 'Adler Irene'` | memmem + count | 31.1 (28.4) | 23.4 (20.7) | −24.7 % | −25.1 % |
| en | `-c zq` | memmem (2 bytes) | 14.6 (12.8) | 14.3 (12.9) | −1.8 % | +2.8 % |
| en | `-c Wat` | memmem (3 bytes) | 32.2 (29.5) | 26.2 (24.2) | −18.5 % | −18.5 % |
| en | `-cw the` | memmem | 57.5 (49.8) | 55.8 (48.9) | −2.9 % | −2.3 % |
| code | `fn main` | memmem | 24.5 (21.1) | 18.2 (15.3) | −25.5 % | −26.4 % |
| code | `-n unsafe` | memmem + count | 36.6 (31.4) | 26.7 (22.0) | −26.9 % | −28.1 % |
| code | `zqzqzqzq` (absent) | memmem | 22.9 (19.9) | 15.9 (14.2) | −30.6 % | −30.7 % |
| en | `-c [HW]ello` | pair, sets of 2 | 25.6 (20.9) | 23.4 (21.0) | −8.3 % | +1.6 % |
| en | `-c -i 'sherlock holmes'` | pair, sets of 2 | 52.8 (48.3) | 54.9 (49.0) | +4.0 % | +1.8 % |
| en | `-c 'Holmes\|Watson'` | pair | 51.8 (47.6) | 44.8 (40.5) | −13.4 % | −14.6 % |
| code | `-c [QZ][0-9]` | pair, set of 16 | 29.0 (26.3) | 18.7 (16.6) | −35.3 % | −36.0 % |
| code | `-c [QZX][0-9]` | pair, set of 16 | 31.9 (28.8) | 19.8 (18.2) | −37.8 % | −37.9 % |
| code | `-c [{}~]` | pair, set of 3 | 51.6 (46.1) | 46.4 (41.3) | −10.0 % | −10.9 % |
| code | `-c '(Q\|Z)[a-z]'` | pair | 17.9 (15.3) | 16.4 (15.0) | −8.4 % | −5.2 % |
| code | `-c 'unsafe fn\|extern "C"'` | ugrep (control) | 21.6 (19.1) | 20.3 (18.1) | −5.9 % | −2.9 % |

`[HW]ello` and `-i`, which used to give +9 % and +5.5 %, are now at par: +1.6-1.8 % paired and
+0.5-1.4 % on the minimum, less than the noise of the control query. `-cw the` went from +2 % to
−2.3 %. No query is slower with NVOL beyond the noise.

### How the two regressions were closed

In both cases ugrep's predictor accepts and discards hundreds of thousands of candidates. A
separate build with counters measured how much goes through each path:

| query | search calls | blocks with candidates | accepted candidates |
|---|---|---|---|
| `[HW]ello` | 266 | 406 103 | 266 |
| `-i 'sherlock holmes'` | 425 520 | 397 974 | 425 520 |

Almost all the cost was in the blocks with candidates that `predict_match` rejects, not in the
restarts. The kernel returned each of those blocks, ugrep checked its candidates and reentered.
Each reentry paid for a `switch` with an indirect jump, 4 to 10 `vpbroadcastb` and the boundary
calculations. ugrep, by contrast, stays in its loop with the vectors already prepared.

To isolate it, `harness/ugrep-e2e/restartbench.cpp` was built. It reproduces ugrep's loop and
NVOL's over the same text, with an opaque `predict` that rejects everything. Minimum of 31
rounds, in ms:

| loop | `[HW]…o` | `[Hh]…[oO]` |
|---|---|---|
| ugrep (original) | 9.94 | 13.88 |
| NVOL, one block per call (`_mask`) | 10.78 (+8.5 %) | 14.96 (+7.8 %) |
| NVOL in batches (8 blocks per call, discarded) | +11-18 % | |
| NVOL with *visitor* (`_visit`) | 10.06 (+1.2 %) | 14.03 (+1.1 %) |

With the *visitor*, the kernel passes each block with candidates to a caller function and
continues while that function returns false. Once inlined into ugrep, GCC turns the call through
`nvol_visitor` into a direct call and inlines the callback. The resulting loop is instruction for
instruction ugrep's loop. The remaining 1 % is the difference between loading the two positions
with an index (the kernel is generic in `i1`) and the `lcp = 0` case that ugrep specializes. It is
within the variation from code placement.

Two real generator problems came up along the way:

- **`memcpy` with *fortify headers*.** Alpine (musl) wraps `memcpy` in an inline function with
  overlap checks. Those checks stayed inside the loop of every `load64`/`store64`: 4-5 extra
  comparisons and jumps per access. ugrep's function dropped from 1379 to 805 lines of assembly
  by using `__builtin_memcpy` in the prelude. That change alone took `-cw the` from +2 % to
  −2.6 % in that step's measurement. In the final table, together with the *visitor*, it gives
  −2.3 % paired.
- **Batch kernels.** They were tried as an alternative to the *visitor*. They cut reentries by a
  factor of 8, but separating the search from the candidate walk performed worse than the
  one-block version. They did not stay in NVOL.

The earlier history (from +30 % to +9 %) was: the kernel compiled separately (+21-30 %), the
block mask (+24 %), the C++ header (+21 %), precomputed selector and range (+14 %), and
`always_inline` with a `switch` inside ugrep (+9 %). That last step found that the kernels with an
ISA target came out as plain `inline`, and GCC left them out of line.

## Setup notes

- The ugrep repo stores its sources with CRLF. The patch is exported normalized to LF, and
  `git apply` applies it on the WSL clone.
- Inside `wsl -- sh -c '…'`, `$x` variables are lost. Everything that runs in WSL goes in
  scripts.
- Other tests were running in the same WSL distribution during the measurement (load average
  1.5-2.5). The tables above were taken with load average < 1, and the control query stayed
  within ±1 %.
