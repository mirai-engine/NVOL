# Inventory of the SIMD that ripgrep uses

Reference: `BurntSushi/ripgrep@178cc02` (ripgrep 15.x), dependencies resolved by
its `Cargo.lock` (`cargo vendor`). **ripgrep contains no intrinsics of its own**: all
the SIMD lives in its dependencies. This defines the scope that NVOL has to cover
before starting `grepper`.

## Crates with SIMD code

| Crate | Version | Does it affect ripgrep? | What it provides |
|---|---|---|---|
| `memchr` | 2.8.3 | **Yes, core** | memchr/memrchr 1-3 needles, count, memmem (packed pair + Two-Way + Rabin-Karp + Shift-Or) |
| `aho-corasick` | 1.1.5 | **Yes, core** | Teddy (packed multi-literal search, SSSE3/AVX2/NEON) |
| `bstr` | 1.13.0 | Yes, minor | `first_non_ascii_byte` (SSE2) |
| `encoding_rs` | 0.8.35 | **No** | SIMD only with the `simd-accel` feature (nightly); ripgrep does not enable it |
| `zmij` | 1.0.23 | No | float formatting (JSON output) |
| `getrandom` | 0.4.3 | No | RDRAND/RNDR |

## How ripgrep reaches each routine

| Routine | Caller in ripgrep | Use |
|---|---|---|
| `memchr` / `memrchr` | `grep-searcher` (`lines.rs`, `LineStep`), `regex-automata` | locate line terminators, 1-byte prefilter |
| `memchr_iter().count()` → `count_raw` | `grep-searcher::lines::count` | count lines (`-n`, `-c`, context) |
| `memchr2` / `memchr3` (+ `r`) | `regex-automata` (`util/prefilter/memchr.rs`) | prefilters when the regex starts with 2-3 bytes |
| `memmem::Finder` | `regex-automata` (`util/prefilter/memmem.rs`) | single-literal prefilter |
| Teddy (`aho_corasick::packed::Searcher`) | `regex-automata` (`util/prefilter/teddy.rs`) | prefilter for 2..64 literals |
| `first_non_ascii_byte` | `bstr` (UTF-8 validation/decoding) | output and UTF-8 handling |

## Vector primitives needed

Extracted from the `Vector` traits in `memchr/src/vector.rs` and
`aho-corasick/src/packed/vector.rs` (all the routines above are written
generically over those traits):

| NVOL primitive | SSE2/SSSE3 | AVX2 | NEON | Used by | Status |
|---|---|---|---|---|---|
| `splat` | `_mm_set1_epi8` | `_mm256_set1_epi8` | `vdupq_n_u8` | all | ✅ |
| `load_a` / `load_u` | `_mm_load(u)_si128` | `_mm256_load(u)_si256` | `vld1q_u8` | all | ✅ |
| `cmpeq` | `_mm_cmpeq_epi8` | `_mm256_cmpeq_epi8` | `vceqq_u8` | all | ✅ |
| `vor` / `vand` | `_mm_or/and_si128` | `_mm256_or/and_si256` | `vorrq/vandq_u8` | all | ✅ |
| `movemask` | `_mm_movemask_epi8` | `_mm256_movemask_epi8` | `vshrn_n_u16` (4 bits/lane) | all | ✅ |
| `first_offset` / `last_offset` / `popcnt` / clear-lsb | tzcnt / lzcnt / popcnt | same | rbit+clz / same | all | ✅ (clear-lsb = `m & (m - 1)` in the IR) |
| `is_zero` | `movemask == 0` | `_mm256_testz` | `vmaxvq_u8 == 0` | Teddy |✅ |
| `shift_8bit_lane_right<N>` | `_mm_srli_epi16` + and | `_mm256_srli_epi16` + and | `vshrq_n_u8` | Teddy |✅ |
| `shift_in_{1,2,3}_bytes` | `_mm_alignr_epi8` (SSSE3) | `permute2x128` + `alignr` | `vextq_u8` | Teddy |✅ |
| `shuffle_bytes` | `_mm_shuffle_epi8` (SSSE3) | `_mm256_shuffle_epi8` | `vqtbl1q_u8` | Teddy |✅ |
| `swap_halves`, `interleave_{low,high}`, `load_half` | — | `permute4x64`, `unpack(lo/hi)_epi8`, `broadcastsi128` | — | Teddy Fat |✅ |
| `for_each_64bit_lane` | `_mm_extract_epi64` | `_mm256_extract_epi64` | `vgetq_lane_u64` | Teddy (verification) |✅ |
| `vsub8`, `sad0`, `vadd64`, `hsum64`, `vzero` | `sub_epi8`, `sad_epu8`, `add_epi64` | same, 256 | `vsubq`, `vpaddlq`… | **NVOL count (improvement)** | ✅ |

## Scalar algorithms that also have to be ported

Status: Two-Way (forward), Rabin-Karp and the rarity table ✅ ported. The memmem selector does not
use Shift-Or (not needed).

They are not SIMD, but without them `memmem` is not 1:1: Two-Way (`arch/all/twoway.rs`),
Rabin-Karp (`arch/all/rabinkarp.rs`), Shift-Or (`arch/all/shiftor.rs`), the byte
rarity table `packedpair/default_rank.rs`, and in aho-corasick Teddy's fallback
`packed::rabinkarp`.

## Dispatch we replicate

`memchr` x86_64: `len < 16` → scalar, `len < 32` → SSE2, otherwise → AVX2 if the CPU
supports it (runtime detection, cached). NVOL generates exactly
that tree in C (cpuid/`__builtin_cpu_supports`) and Rust (`is_x86_feature_detected!`).
