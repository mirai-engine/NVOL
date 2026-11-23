# simdjson stage 1: NVOL against simdjson 5.2.0 (2026-11-23)

PENDING item 35. `nvol_json_index_raw` (AVX2 + PCLMULQDQ + BMI1) against simdjson's own stage 1
(`dom_parser_implementation::stage1`, haswell kernel, runtime dispatch), on the same document.

**Machine:** Intel i5-7400 (Kaby Lake), idle, WSL 2 Arch (glibc), gcc -O2.
**Harness:** [`harness/simdjson-stage1`](../../harness/simdjson-stage1): `setup.sh` builds
`stage1bench` from simdjson's single header and NVOL's generated C, then checks that both give
the same positions (citm_catalog 135990, twitter 55263: same). `counters.sh`: per pass,
(N = 2000 passes − 0 passes) / N, the best of 15 runs per counter.

## Result

| file | impl | instr/pass | cycles/pass | branch misses | bytes/cycle |
|---|---|---:|---:|---:|---:|
| citm_catalog (1.7 MB) | simdjson | 3169885 | 946368 | 3275 | 1.83 |
| citm_catalog | NVOL | 3461730 | 991070 | 2086 | 1.74 |
| twitter (632 KB) | simdjson | 1303178 | 387384 | 1342 | 1.63 |
| twitter | NVOL | 1272397 | 366652 | 987 | 1.72 |

NVOL: +4.7 % cycles on citm_catalog, −5.4 % on twitter. **simdjson's rows include its UTF-8
validation**, which NVOL's stage 1 does not do yet (item 36); the parity verdict is the one
measured after 36, with both doing the same work.

## How it got there

Each step is its own commit, A/B interleaved twice against the commit before it.

| commit | change | citm cycles | twitter cycles |
|---|---|---:|---:|
| 7e4b201 | one tzcnt/blsr loop per block (start) | 1449093 | 668322 |
| ee29dbd | indexer unrolled as `bit_indexer::write` (4 slots, then 4 while the count goes past, up to 24) | 1390392 (−4.1 %) | 592037 (−11.3 %) |
| fa05491 | BMI1 for the AVX2 kernel, `_tzcnt_u64` in the indexer | 1383874 (−0.6 %) | 498842 (−15.8 %) |
| ee571a9 | each block written while the next is scanned | 1202968 (−13.0 %) | 430405 (−13.7 %) |
| fe635a5 | two blocks per iteration (`step<128>`) | 991905 (−17.5 %) | 368115 (−14.5 %) |

Total: citm −31.6 %, twitter −44.9 %.

## Rejected on the way

- **`first_offset64(bits | 1 << 63)`** for the unconditional slots (no UB at 0 without a new
  primitive): GCC rematerializes the `movabs` for each `or`, 7 µops per slot against
  simdjson's 4; citm +8 % over the loop. Replaced by the `tzcnt64` primitive (f1419a0).
- **BMI1 with `x ? __builtin_ctzll(x) : 64`**: GCC knows that once `blsr` clears the last bit
  every later tzcnt is 64, and branches on each `blsr`, which brings back a branch per
  position (twitter −0.7 %, 4635 misses). `_tzcnt_u64` is opaque to it, as in simdjson.

## Not measured here

The SSSE3 and scalar kernels, NEON (no ARM hardware; QEMU only checks correctness), and the
Rust, Odin and assembly builds of the kernel (phase 4 compares them against the C).
