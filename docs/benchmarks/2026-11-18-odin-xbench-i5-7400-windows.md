# Odin vs Rust in a single process — i5-7400 (AVX2), Windows 10

Odin dev-2026-10 (LLVM 20.1), rustc 1.99.1 (LLVM 23.1). This replaces the comparison in
[2026-11-18-odin](2026-11-18-odin-i5-7400-windows.md), which measured each language in its own
process.

```
cargo build --release --manifest-path harness/rust-ffi/Cargo.toml
odin run harness/odin/xbench -collection:nvol=generated -o:speed
```

`harness/rust-ffi` is a staticlib that exposes NVOL-Rust and the reference crates (memchr 2.8.3,
aho-corasick 1.1.5) through the C ABI. The Odin bench links it and measures the three
implementations in interleaved batches (Odin, NVOL-Rust, reference, and again) in the same process
and on the same buffer. It reports GB/s, the best of 15 batches.

## Why a single process

The same code gives different results depending on the process:

- `xbench` (Rust) measured memchr with 64 KiB at 94 GB/s in one run and at 85 in another, minutes
  later.
- The `memchr` crate called through FFI from an Odin process gave 81–86.
- Two different Rust binaries gave 83 and 86–90.

Buffer alignment was ruled out: across offsets 0, 16, 32, 48, 64, 2048 and 4112 the variation stays
within ±3 %. Physical pages were also ruled out: 12 different buffers in one process gave 79–84.

That noise between processes (±7 %) is larger than the differences we wanted to measure. So the
"Odin 11–17 % below" gap in the previous report was overestimated. With the same harness and in the
same process, the Odin code from before these changes, the generated C compiled with clang 21, and
the `memchr` crate tied within ±2 %.

## What was really left and how it was fixed

Measured in the same process, before the changes: memchr −9 % and iter −12 % relative to
NVOL-Rust. The cause was in the asm of the main loop (128 bytes per iteration):

| | Rust (LLVM 23) | Odin before | Odin now |
|---|---|---|---|
| fused uops per iteration | 11 | 14 | 11 |
| mask `or` | `vpor` ×3 + `vpmovmskb` | `vpor` ×3 + **`vpsllw $7`** + `vpmovmskb` | `vpor` ×3 + `vpmovmskb` |
| induction variables | 1 (`sub`, `cmp`/`jbe`) | 2 (`lea`, 2× `sub`, `cmp`/`jle`) | counter (`sub`, `dec`/`jne`) |

1. **`vpsllw $7`:** LLVM 20 InstCombine turns `or(sext(a), sext(b))` into
   `sext(or(a, b))` on `<32 x i1>`. When lowered, the result sits in bit 0 and needs a shift
   before the `movemask`. The same happened with `ptestz`. The mask `or` is now its own primitive,
   `mask_or`. Its contract requires 0x00/0xFF lanes, and the interpreter checks it. In C, C++,
   Rust and NEON it lowers the same as `vor`. In Odin it lowers as `simd_max`, which gives the
   same result for those values and which LLVM does not fold.
2. **Two induction variables:** LLVM 20 LSR keeps `cur` and `cur + 32` with the pointer condition
   `cur <= end - 128`. This happens with signed comparison (Odin emits `icmp sle ptr`), with
   unsigned comparison (via `uintptr`), and with any way of aligning the start. Rust's LLVM 23
   produces a clean loop with the same structure. `While` now accepts a `trip` hint, the exact
   iteration count (`(end - cur) >> log2(step)`). On every evaluation the interpreter checks that
   the condition holds exactly while the counter is nonzero, and the Odin backend iterates over
   the counter. C, C++ and Rust ignore the hint and do not change.
   **No longer the case:** `trip` was later removed from the IR. The root cause was Odin's GEPs
   without `inbounds`. The backend now builds the aligned pointer from the address, and the loop
   keeps a single induction variable with no counter. See
   [odin-counters-wsl](2026-11-18-odin-counters-wsl.md).

## Results (two runs)

```
size=65536    memchr_absent   odin=88.91  nvol-rust=89.34  reference=87.68  odin/nvol-rust=-0.5%
size=65536    memchr3_absent  odin=39.89  nvol-rust=39.74  reference=36.18  odin/nvol-rust=+0.4%
size=65536    count_nl        odin=84.56  nvol-rust=84.61  reference=25.95  odin/nvol-rust=-0.1%
size=65536    memchr_iter_nl  odin=9.57  nvol-rust=10.47  reference=9.71  odin/nvol-rust=-8.6%
size=65536    memmem_absent   odin=29.63  nvol-rust=27.44  reference=22.75  odin/nvol-rust=+8.0%
size=65536    teddy16_absent  odin=6.30  nvol-rust=6.29  reference=6.34  odin/nvol-rust=+0.2%
size=1048576  memchr_absent   odin=55.53  nvol-rust=52.94  reference=55.45  odin/nvol-rust=+4.9%
size=1048576  memchr3_absent  odin=33.79  nvol-rust=33.86  reference=31.28  odin/nvol-rust=-0.2%
size=1048576  count_nl        odin=54.83  nvol-rust=55.10  reference=25.32  odin/nvol-rust=-0.5%
size=1048576  memchr_iter_nl  odin=4.96  nvol-rust=5.10  reference=5.19  odin/nvol-rust=-2.8%
size=1048576  memmem_absent   odin=27.87  nvol-rust=25.60  reference=22.88  odin/nvol-rust=+8.8%
size=1048576  teddy16_absent  odin=6.33  nvol-rust=6.35  reference=6.39  odin/nvol-rust=-0.4%

size=65536    memchr_absent   odin=85.66  nvol-rust=86.64  reference=84.35  odin/nvol-rust=-1.1%
size=65536    memchr3_absent  odin=39.66  nvol-rust=39.61  reference=35.76  odin/nvol-rust=+0.1%
size=65536    count_nl        odin=86.66  nvol-rust=82.50  reference=25.94  odin/nvol-rust=+5.0%
size=65536    memchr_iter_nl  odin=9.43  nvol-rust=7.52  reference=9.42  odin/nvol-rust=+25.4%
size=65536    memmem_absent   odin=30.10  nvol-rust=27.66  reference=23.16  odin/nvol-rust=+8.8%
size=65536    teddy16_absent  odin=6.29  nvol-rust=6.33  reference=6.29  odin/nvol-rust=-0.6%
size=1048576  memchr_absent   odin=53.11  nvol-rust=55.66  reference=54.97  odin/nvol-rust=-4.6%
size=1048576  memchr3_absent  odin=33.91  nvol-rust=33.84  reference=31.33  odin/nvol-rust=+0.2%
size=1048576  count_nl        odin=54.58  nvol-rust=54.50  reference=25.33  odin/nvol-rust=+0.1%
size=1048576  memchr_iter_nl  odin=4.96  nvol-rust=5.17  reference=5.16  odin/nvol-rust=-4.0%
size=1048576  memmem_absent   odin=27.67  nvol-rust=25.76  reference=22.86  odin/nvol-rust=+7.4%
size=1048576  teddy16_absent  odin=6.32  nvol-rust=6.35  reference=6.42  odin/nvol-rust=-0.4%
```

In the second run, the nvol-rust 7.52 in `memchr_iter_nl` (64 KiB) is a disturbed batch. In the
other five runs of this session it gave between 10.5 and 11.1.

**Conclusion:**

- **At par with NVOL-Rust:** memchr, memchr3, count and Teddy at both sizes.
- **Above:** memmem, +8 % to +9 %.
- **`memchr_iter`:** at par with the reference crate, which is ripgrep's yardstick (−1 % to −3 %
  with 64 KiB, −4 % to −5 % with 1 MiB). It is ~9 % below NVOL-Rust with 64 KiB, and NVOL-Rust in
  turn beats the crate. It makes one call every ~80 bytes, and the short path has the same
  instructions in both languages. `-microarch:skylake` does not change it (PENDING 2.1a).
  **Update:** resolved; see `2026-11-18-odin-counters-wsl.md`. With counters, Odin executes fewer
  instructions than NVOL-Rust. What remains on the Windows wall clock is code placement.
