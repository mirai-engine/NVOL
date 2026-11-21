# Inventory of the SIMD that ugrep uses

Reference: `Genivia/ugrep` v7.8.5 (`f2cc252`). ugrep is C++11. Its SIMD is in the bundled RE/flex
library (`lib/`, `include/reflex/`), plus calls to the libc `memchr` from
`src/`. No external dependency contributes SIMD to the search. Runtime dispatch:
`reflex::HW` (cpuid) chooses AVX-512BW, AVX2 or SSE2; on ARM, NEON.

## Routines and their NVOL equivalent

| ugrep routine | Where | What it does | NVOL equivalent | Status |
|---|---|---|---|---|
| `memchr(s, '\n', n)` (libc) | `src/ugrep.cpp` (~15 sites), `src/output.cpp`, `src/query.cpp`, `absmatcher.h`, `fuzzymatcher.h` | End of line, next line, output | `nvol_memchr` | ✅ direct |
| `memchr(buf, 0, n)` (libc) | `src/ugrep.cpp` | Detect binary files | `nvol_memchr` | ✅ direct |
| `nlcount` / `simd_nlcount_avx2` / `_avx512bw` | `lib/simd*.cpp`, `absmatcher.h` | Count `\n` (line numbers): `popcount` of the mask, 128 B per iteration | `nvol_count` (SAD), 2-4× faster than `popcount` in memchr | ✅ direct, improvement expected |
| `simd_advance_string_avx2` (and SSE2, NEON) | `lib/matcher*.cpp` | Literal: byte pair at `lcp`/`lcs` (chosen by ugrep) + `memcmp` | `nvol_memmem` (Finder) | ✅ direct |
| `simd_advance_chars_avx2<LEN>` | `lib/matcher_avx2.cpp` | Short literal (2-8 bytes): first/last pair + `memcmp` | `nvol_memmem` | ✅ direct |
| `simd_advance_pattern_pin1_pma_avx2`, `_chars_pma`, `_string_pma` | `lib/matcher_avx2.cpp` | Byte pair at a fixed distance → candidate → `predict_match` (regex predictor) | `nvol_find_byteset_pair_visit` (1-byte sets; `predict_match` goes as the *visitor*) | ✅ done |
| `simd_advance_pattern_pin{2..8,16}_one_avx2` | `lib/matcher_avx2.cpp` | First byte of **a set of N** (N = 2-8 or 16) + `predict_match` | `nvol_find_byteset` (N = 4-16), `memchr2`/`memchr3` (N = 2, 3) | ✅ done |
| `simd_advance_pattern_pin{2..8,16}_pma_avx2` | `lib/matcher_avx2.cpp` | Pair of **sets** of N bytes at a fixed distance | `nvol_find_byteset_pair_visit` (inlined `_visit` kernels, `predict_match` as the *visitor*) | ✅ done: equal to or better than ugrep from N = 2 to 16 (up to +190 %) |
| `simd_advance_pattern_min4_avx2` | `lib/matcher_avx2.cpp` | Shift-Or (bitap) with hash and `gather` over the RE/flex predictor tables | — | ⚪ specific to the regex engine; out of scope |
| `isutf8` / `simd_isutf8_avx2` | `lib/simd*.cpp`, `src/ugrep.cpp`, `src/output.cpp` | Validate UTF-8 (ASCII fast path + validation) | `nvol_first_non_ascii` covers the fast path | 🟡 full UTF-8 validation missing |
| All `_avx512bw` variants | `lib/*_avx512bw.cpp` | The same with 64 bytes | — | ❌ NVOL has no AVX-512 (PENDING 1.8); neither does the i5-7400, so it cannot be tested here |

## Status (2026-11-18)

`harness/ugrep-e2e` ([report](benchmarks/2026-11-18-ugrep-e2e-wsl.md)). These go through NVOL:
`memchr` (all 44 sites), `nlcount`, the literals and all the set searches (`pinN_one`,
`pinN_pma`), with the new kernels in `nvol/kernels/byteset.py` and the header-only C++ header.
The ugrep suite passes the same as the original; 18 queries give identical output; literals −14 %
to −26 % and large sets −35 %. The `_visit` kernels closed the two frequent-restart regressions
(`[HW]ello` +9 %, `-i` +5.5 %; formerly PENDING 1.15): they ended at +1.6-1.8 % paired, within
the noise of the control query. No query is slower beyond noise.

Missing: full UTF-8 validation (`isutf8`, PENDING 1.13) and AVX-512BW (1.8). `min4` (bitap with
`gather`) belongs to the regex engine and is disabled by default in ugrep.

## How to test it end to end

ugrep ships a regression suite (`tests/`, expected outputs in `tests/out/`, `make test`).
The plan mirrors the ripgrep one: build the original ugrep and the NVOL one, run the suite,
compare the output of a set of queries byte for byte, and measure interleaved timings. The
recommended build is Linux under WSL (`./configure && make`). It is the environment of its CI,
and it also gives the Linux measurement that PENDING 1.10 asks for.
