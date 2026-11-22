# Parity log

Each language backend against NVOL's own assembly, the internal reference (DESIGN, principle
5; PENDING items 19-24). A row that does not match carries its cause, seen in the code or in
the counters, and whether it is accepted, fixed in the generator or reported. **A row without
an explained cause is an open pending item.**

## How it is measured

`harness/parity/parity.sh` (item 19) runs the operations, sizes and inputs of
`harness/asm/bench.c` against seven builds, in WSL Arch on an i5-7400 (Kaby Lake):

| build | what | compiler |
|---|---|---|
| asm | NASM object + the C glue: the reference | NASM 3.01, gcc 16 for the glue |
| gcc, clang | `generated/c` | gcc 16, clang 23.1.5, `-O2` |
| gxx, clangxx | `generated/cpp`, through `harness/parity/bench_cpp.cpp` | g++ 16, clang++ 23.1.5, `-O2 -std=c++17` |
| rust | `harness/rust` `parbench` | rustc, release, fat LTO |
| odin | `harness/odin/parbench` | odin dev-2026-10 (LLVM), `-o:speed` |

Per call: (counts with N calls − counts with 0 calls) / N, with `perf stat`. Instructions are
exact; cycles are the best of 5. Path 1 is AVX2, path 0 SSE2/SSSE3 (the test hook). C++ and
Rust have no test hook, so they only run on path 1, the CPU's own. Every call scans the whole
buffer (nothing matches).

**Read the table as a call, not a kernel.** The asm is always a call through the ifunc
pointer. C++, Rust and Odin inline the public function, and often the kernel, into the
caller's loop, so they save the call and hoist the setup out of it. That is most of why their
geomeans are below the asm's (P2 below).

## Summary (2026-11-22, after P1)

| build | rows | geomean cycles against asm | rows > +10 % instructions | rows > +10 % cycles |
|---|---|---|---|---|
| gcc | 130 | −3.4 % | 12 | 24 |
| clang | 130 | −1.0 % | 24 | 27 |
| gxx | 65 | −9.8 % | 9 | 8 |
| clangxx | 65 | −9.5 % | 5 | 9 |
| rust | 65 | −9.1 % | 4 | 11 |
| odin | 130 | −11.2 % | 0 | 12 |

## Causes

| # | rows | cause | status |
|---|---|---|---|
| P1 | count and count_parity, gcc (all paths) and rust (AVX2): before, up to +190 % cycles (gcc count_parity 4 KiB SSE2: 2298 against 793) and +112 % (rust count_parity 4 KiB AVX2) | the bit count. `target("avx2")` / `target_feature(enable = "avx2")` does not enable POPCNT, so the AVX2 kernels counted bits in software; on SSE2, GCC's `__builtin_popcount` is a call to libgcc's `__popcountdi2` | **fixed in the generator**: the AVX2 and SSE4.2 targets add `popcnt` (every such CPU has it; the asm assumes it too), and the C counts with the SWAR idiom, which GCC and Clang turn into `popcnt` where the target has it and keep inline elsewhere. After: gcc count_parity 4 KiB SSE2 1102 (+39 %), rust count_parity 4 KiB AVX2 −8 % |
| P2 | most C++, Rust and Odin rows below the asm (odin −11.2 %, rust −9.1 %) | inlining into the caller's loop: no call, setup hoisted | accepted: a property of the asm (always a call), not a defect of either side |
| P3 | count 16 B SSE2: gcc +48 % / clang +24 % instructions | the SWAR count (12 operations) where the asm uses a byte table (2 lookups for a 16-bit mask). A table in the C was tried: better for Clang on SSE2, but it stays a table when the SSE2 kernel is inlined into the AVX2 entry, where SWAR becomes `popcnt` (gcc count 16 B AVX2, instructions / cycles against the asm: +73 % / +55 % with the table, +22 % / +6 % with SWAR) | accepted: the smaller loss |
| P4 | first_non_ascii 64 B-4 KiB (gcc, gxx, rust, odin), memchr 256 B (gcc, gxx), byteset 64 KiB AVX2 (rust, odin), memmem2 4 KiB SSE2 (clang): +15…+41 % cycles with the same instructions (±3 %) | not the code: the same instruction stream. The JCC erratum was checked with GNU as's mitigation (`-Wa,-mbranches-within-32B-boundaries`): gcc first_non_ascii 4 KiB 95.0 → 103.5, memchr 256 B 27.8 → 30.7, no recovery. Placement in the compiler's layout, the same class as the asm's own position effects | open, with item 25 (frontend counters) |
| P5 | clang: first_non_ascii (+13…+16 % instructions at 256 B-4 KiB), memchr (+11…+18 % at 64 B-4 KiB) | Clang's code for the same C has more instructions than GCC's and the asm's; not yet read | **open** (item 22) |
| P6 | byteset_not 16-64 B AVX2: gcc +21…+27 %, clang +16 %, g++ +24 % instructions | the compilers' code for the short path; not yet read | **open** (item 22) |
| P7 | count 256 B AVX2: gcc +15 %, g++ +12 % instructions | GCC's code; not yet read | **open** (item 22) |
| P8 | count_parity SSE2, gcc 4-64 KiB: +9 % instructions, +31…+39 % cycles | the SWAR count in the hot loop (P3), four times per iteration | accepted with P3 |

## The table

`asm` cells are instructions / cycles per call; the others are the difference against the
asm in instructions / cycles. Regenerate with `MD=1 sh harness/parity/parity.sh`.

| op | size | path | asm | gcc | clang | gxx | clangxx | rust | odin |
|---|---|---|---|---|---|---|---|---|---|
| memchr | 16 | 1 | 44 / 17.3 | -2% / -18% | +11% / -24% | -20% / -39% | -5% / -36% | -7% / -30% | -7% / -30% |
| memchr | 16 | 0 | 43 / 17.1 | +7% / -24% | +16% / +4% | — | — | — | +0% / -28% |
| memchr | 64 | 1 | 52 / 17.1 | +0% / +7% | +10% / +12% | -18% / -6% | -4% / -8% | +6% / -23% | -4% / -29% |
| memchr | 64 | 0 | 71 / 20.3 | +3% / +23% | +13% / +44% | — | — | — | +0% / -25% |
| memchr | 256 | 1 | 83 / 23.1 | -1% / +27% | +18% / +18% | -12% / +20% | +10% / +10% | +4% / -13% | -2% / -25% |
| memchr | 256 | 0 | 129 / 28.8 | -0% / +11% | +16% / +26% | — | — | — | -0% / -4% |
| memchr | 4096 | 1 | 473 / 122.5 | -0% / -14% | +16% / +12% | -2% / -12% | +15% / +21% | +1% / +2% | -0% / -9% |
| memchr | 4096 | 0 | 1268 / 283.3 | +0% / +2% | +11% / +15% | — | — | — | -0% / -2% |
| memchr | 65536 | 1 | 6726 / 2501.6 | -0% / +3% | +15% / +6% | -0% / +4% | +15% / +8% | -0% / +1% | +1% / +6% |
| memchr | 65536 | 0 | 19527 / 4823.8 | -0% / +0% | +10% / +7% | — | — | — | +0% / -1% |
| memrchr | 16 | 1 | 51 / 16.6 | -4% / -9% | -2% / -9% | -20% / -40% | -16% / -28% | -8% / -7% | -24% / -40% |
| memrchr | 16 | 0 | 51 / 16.8 | +2% / -10% | +6% / +24% | — | — | — | -18% / -21% |
| memrchr | 64 | 1 | 58 / 19.7 | +0% / -12% | +2% / -2% | -14% / -35% | -10% / -1% | -9% / -18% | -17% / -23% |
| memrchr | 64 | 0 | 67 / 17.6 | +0% / -2% | +4% / +15% | — | — | — | +4% / +26% |
| memrchr | 256 | 1 | 75 / 22.7 | -1% / -19% | +3% / -7% | -12% / -19% | -6% / -7% | +16% / +12% | +7% / +13% |
| memrchr | 256 | 0 | 124 / 28.9 | +0% / +0% | +5% / +11% | — | — | — | +5% / +15% |
| memrchr | 4096 | 1 | 467 / 126.1 | -1% / +8% | +6% / -11% | -3% / -13% | +4% / -9% | +8% / +8% | +7% / -9% |
| memrchr | 4096 | 0 | 1266 / 284.8 | -0% / -8% | +5% / +4% | — | — | — | +5% / +7% |
| memrchr | 65536 | 1 | 6693 / 2826.0 | +0% / -9% | +8% / +1% | +1% / -13% | +8% / +11% | +9% / +4% | +8% / -1% |
| memrchr | 65536 | 0 | 19484 / 5503.2 | +0% / -11% | +5% / -1% | — | — | — | +5% / -1% |
| memchr2 | 16 | 1 | 51 / 14.2 | -2% / -7% | +10% / +13% | -18% / -22% | -4% / -6% | -8% / -15% | -2% / -13% |
| memchr2 | 16 | 0 | 51 / 15.2 | +8% / -1% | +14% / +6% | — | — | — | +2% / -19% |
| memchr2 | 64 | 1 | 65 / 17.4 | +0% / -1% | +5% / +16% | -14% / -14% | -6% / +5% | +8% / -6% | +0% / -13% |
| memchr2 | 64 | 0 | 85 / 20.2 | +1% / +8% | +8% / +13% | — | — | — | +1% / -7% |
| memchr2 | 256 | 1 | 111 / 27.8 | -2% / -4% | +3% / +2% | -10% / -10% | -4% / -9% | +5% / +3% | +0% / -9% |
| memchr2 | 256 | 0 | 193 / 44.4 | +0% / +2% | +4% / +7% | — | — | — | +0% / -3% |
| memchr2 | 4096 | 1 | 1010 / 223.5 | -0% / -2% | +0% / +5% | -1% / +1% | -0% / -2% | +1% / +4% | +0% / +1% |
| memchr2 | 4096 | 0 | 2354 / 548.4 | +0% / +1% | +0% / +1% | — | — | — | -0% / -3% |
| memchr2 | 65536 | 1 | 15404 / 3930.0 | -0% / +3% | +0% / +5% | +0% / +3% | +0% / +4% | -0% / -0% | +0% / +2% |
| memchr2 | 65536 | 0 | 36910 / 8325.1 | -0% / +2% | +0% / +2% | — | — | — | +0% / -1% |
| memchr3 | 16 | 1 | 57 / 17.1 | -2% / -12% | +7% / -6% | -16% / -29% | -5% / -19% | -9% / -23% | +0% / -18% |
| memchr3 | 16 | 0 | 59 / 17.0 | +10% / +1% | +14% / +6% | — | — | — | +3% / -10% |
| memchr3 | 64 | 1 | 73 / 18.1 | -0% / +6% | +3% / +12% | -14% / -10% | -7% / -1% | +8% / +6% | +1% / -7% |
| memchr3 | 64 | 0 | 102 / 24.1 | +3% / +13% | +8% / +16% | — | — | — | +2% / +0% |
| memchr3 | 256 | 1 | 131 / 31.4 | -2% / +5% | +2% / +5% | -9% / -8% | -4% / -2% | +5% / +11% | +1% / -4% |
| memchr3 | 256 | 0 | 246 / 57.0 | +1% / +10% | +3% / +8% | — | — | — | +1% / +1% |
| memchr3 | 4096 | 1 | 1272 / 293.7 | -0% / -0% | +0% / +0% | -1% / -2% | -0% / -3% | +0% / +2% | -0% / -2% |
| memchr3 | 4096 | 0 | 3128 / 752.2 | +0% / -0% | +0% / -1% | — | — | — | +0% / -2% |
| memchr3 | 65536 | 1 | 19504 / 5490.7 | +0% / +1% | -0% / -0% | -0% / +5% | +0% / -3% | +0% / -0% | +0% / -2% |
| memchr3 | 65536 | 0 | 49215 / 11275.8 | +0% / +1% | +0% / +0% | — | — | — | +0% / -0% |
| count | 16 | 1 | 37 / 10.9 | +22% / +6% | -11% / -17% | +8% / -4% | -16% / -17% | +0% / -7% | -16% / -8% |
| count | 16 | 0 | 42 / 11.2 | +48% / +35% | +24% / +16% | — | — | — | -21% / -19% |
| count | 64 | 1 | 44 / 16.6 | +25% / -18% | -5% / -27% | +16% / -22% | -9% / -30% | +5% / -22% | -11% / -27% |
| count | 64 | 0 | 70 / 18.2 | +9% / +5% | -7% / -13% | — | — | — | -13% / -18% |
| count | 256 | 1 | 79 / 19.0 | +15% / +22% | -6% / +6% | +12% / +24% | -8% / +0% | -1% / +14% | -6% / +3% |
| count | 256 | 0 | 115 / 28.0 | +8% / +7% | -4% / -2% | — | — | — | -8% / -10% |
| count | 4096 | 1 | 412 / 119.7 | +10% / -6% | -2% / +9% | +9% / -15% | -3% / +9% | -1% / +13% | -2% / +15% |
| count | 4096 | 0 | 1032 / 241.4 | +7% / +6% | -1% / +7% | — | — | — | -1% / +6% |
| count | 65536 | 1 | 5804 / 2360.3 | +9% / -0% | +0% / +4% | +10% / +7% | +0% / +3% | +1% / +9% | +0% / +8% |
| count | 65536 | 0 | 15705 / 4849.5 | +7% / -10% | -0% / -9% | — | — | — | -0% / -7% |
| count_parity | 16 | 1 | 134 / 62.8 | +16% / -44% | -40% / -67% | +13% / -45% | -42% / -67% | -37% / -66% | -20% / -61% |
| count_parity | 16 | 0 | 132 / 64.3 | -15% / -56% | -5% / -42% | — | — | — | -2% / -53% |
| count_parity | 64 | 1 | 240 / 83.6 | -56% / -64% | -52% / -65% | -58% / -63% | -53% / -67% | -39% / -57% | -33% / -54% |
| count_parity | 64 | 0 | 178 / 69.4 | +4% / -36% | +13% / -15% | — | — | — | -11% / -48% |
| count_parity | 256 | 1 | 277 / 84.9 | -47% / -52% | -45% / -56% | -48% / -55% | -46% / -58% | -33% / -46% | -28% / -46% |
| count_parity | 256 | 0 | 320 / 95.4 | +10% / +4% | +1% / +5% | — | — | — | -29% / -44% |
| count_parity | 4096 | 1 | 848 / 247.8 | -15% / -9% | -15% / -13% | -16% / -9% | -15% / -8% | -11% / -8% | -9% / -7% |
| count_parity | 4096 | 0 | 3139 / 794.6 | +9% / +39% | -15% / +8% | — | — | — | -49% / -47% |
| count_parity | 65536 | 1 | 9967 / 3772.2 | -1% / -1% | -1% / +2% | -1% / +7% | -1% / +4% | -1% / +2% | -1% / +0% |
| count_parity | 65536 | 0 | 48277 / 13040.2 | +9% / +31% | -17% / +6% | — | — | — | -51% / -44% |
| first_non_ascii | 16 | 1 | 40 / 17.8 | -5% / -32% | +2% / -27% | -22% / -37% | -15% / -43% | -45% / -76% | -5% / -44% |
| first_non_ascii | 16 | 0 | 37 / 16.9 | +5% / -30% | +8% / -17% | — | — | — | -3% / -40% |
| first_non_ascii | 64 | 1 | 48 / 16.6 | +0% / -2% | +6% / +10% | -15% / +14% | -9% / -16% | +15% / -21% | -2% / -33% |
| first_non_ascii | 64 | 0 | 62 / 18.7 | +0% / +25% | +3% / +35% | — | — | — | -1% / -13% |
| first_non_ascii | 256 | 1 | 78 / 20.1 | +3% / +26% | +15% / +32% | -8% / +41% | +6% / +19% | +10% / -5% | -1% / +16% |
| first_non_ascii | 256 | 0 | 108 / 24.2 | +0% / +36% | +13% / +30% | — | — | — | -2% / +16% |
| first_non_ascii | 4096 | 1 | 466 / 98.7 | +1% / +17% | +16% / +21% | -1% / +19% | +14% / +22% | +2% / +23% | +0% / +22% |
| first_non_ascii | 4096 | 0 | 1008 / 224.4 | +0% / +12% | +13% / +15% | — | — | — | -0% / -3% |
| first_non_ascii | 65536 | 1 | 6717 / 2372.5 | -0% / -4% | +15% / +3% | -0% / +4% | +16% / +7% | +0% / -1% | -0% / -2% |
| first_non_ascii | 65536 | 0 | 15410 / 4233.9 | -0% / -1% | +13% / +4% | — | — | — | -0% / -3% |
| memmem | 16 | 1 | 222 / 58.8 | -6% / -20% | -9% / -22% | -11% / -26% | -14% / -26% | -13% / -22% | -18% / -29% |
| memmem | 16 | 0 | 220 / 66.2 | -11% / -35% | -9% / -31% | — | — | — | -18% / -35% |
| memmem | 64 | 1 | 126 / 33.2 | -14% / -12% | -15% / -12% | -18% / -22% | -24% / -22% | -21% / -17% | -25% / -24% |
| memmem | 64 | 0 | 144 / 38.2 | -12% / -22% | -10% / -15% | — | — | — | -20% / -29% |
| memmem | 256 | 1 | 180 / 49.1 | -10% / -12% | -11% / -10% | -13% / -13% | -17% / -14% | -15% / -14% | -17% / -18% |
| memmem | 256 | 0 | 276 / 73.9 | -6% / -20% | -5% / -14% | — | — | — | -11% / -21% |
| memmem | 4096 | 1 | 1258 / 437.8 | -1% / -10% | -1% / -7% | -2% / -11% | -2% / -9% | -2% / -7% | -2% / -8% |
| memmem | 4096 | 0 | 2915 / 813.2 | -1% / -18% | -0% / -0% | — | — | — | -1% / -18% |
| memmem | 65536 | 1 | 18558 / 7250.6 | -0% / +1% | -0% / -0% | -0% / +1% | -0% / -1% | -0% / -2% | -0% / -2% |
| memmem | 65536 | 0 | 45124 / 12237.5 | +0% / -10% | +0% / +0% | — | — | — | +0% / -10% |
| memmem2 | 16 | 1 | 264 / 68.0 | -10% / -22% | -12% / -22% | -14% / -27% | -17% / -29% | -16% / -14% | -14% / -23% |
| memmem2 | 16 | 0 | 262 / 73.1 | -14% / -33% | -12% / -28% | — | — | — | -13% / -24% |
| memmem2 | 64 | 1 | 124 / 32.3 | -13% / -10% | -14% / -9% | -17% / -20% | -23% / -15% | -20% / -14% | -23% / -23% |
| memmem2 | 64 | 0 | 144 / 34.9 | -12% / -15% | -10% / -7% | — | — | — | -20% / -22% |
| memmem2 | 256 | 1 | 178 / 47.4 | -9% / -6% | -10% / -6% | -12% / -14% | -16% / -14% | -14% / -11% | -16% / -15% |
| memmem2 | 256 | 0 | 276 / 62.4 | -7% / -4% | -5% / +8% | — | — | — | -11% / -8% |
| memmem2 | 4096 | 1 | 1260 / 399.5 | -1% / -4% | -2% / -2% | -2% / -3% | -2% / -4% | -2% / +1% | -3% / +1% |
| memmem2 | 4096 | 0 | 2917 / 644.1 | -1% / +4% | -0% / +26% | — | — | — | -1% / +3% |
| memmem2 | 65536 | 1 | 18547 / 6750.9 | -0% / +1% | -0% / -2% | -0% / -0% | -0% / -1% | -0% / +5% | -0% / +4% |
| memmem2 | 65536 | 0 | 45146 / 10197.7 | +0% / +2% | +0% / +22% | — | — | — | +0% / +9% |
| byteset | 16 | 1 | 58 / 17.1 | +17% / -6% | +2% / -12% | +16% / -6% | -3% / -11% | -7% / -30% | -7% / -29% |
| byteset | 16 | 0 | 68 / 19.3 | +0% / -1% | +16% / +5% | — | — | — | +0% / -23% |
| byteset | 64 | 1 | 68 / 23.0 | +15% / -16% | +3% / -22% | +13% / -19% | -1% / -23% | -6% / -33% | -6% / -39% |
| byteset | 64 | 0 | 119 / 29.0 | +0% / +1% | +12% / +10% | — | — | — | +0% / -6% |
| byteset | 256 | 1 | 128 / 33.2 | +8% / -8% | +6% / -5% | +7% / -3% | +4% / -5% | -3% / -14% | -3% / -21% |
| byteset | 256 | 0 | 323 / 73.5 | -0% / -0% | +8% / +10% | — | — | — | +0% / -2% |
| byteset | 4096 | 1 | 1328 / 324.8 | +1% / +15% | +10% / +12% | +1% / +1% | +9% / +14% | -0% / +16% | -0% / +7% |
| byteset | 4096 | 0 | 4402 / 995.2 | +0% / +1% | +6% / +7% | — | — | — | +0% / +1% |
| byteset | 65536 | 1 | 20551 / 5976.2 | -0% / +10% | +10% / +4% | +0% / +11% | +10% / +10% | -0% / +21% | -0% / +18% |
| byteset | 65536 | 0 | 69695 / 15448.8 | +0% / +1% | +6% / +7% | — | — | — | +0% / +0% |
| byteset_not | 16 | 1 | 45 / 11.9 | +27% / +25% | +16% / +17% | +24% / +18% | +14% / +12% | +11% / +6% | +7% / -3% |
| byteset_not | 16 | 0 | 68 / 18.2 | +6% / +1% | +12% / +10% | — | — | — | -6% / -6% |
| byteset_not | 64 | 1 | 56 / 16.0 | +21% / +27% | +11% / +7% | +20% / +7% | +9% / +4% | +7% / +4% | +4% / +16% |
| byteset_not | 64 | 0 | 122 / 30.3 | +3% / -1% | +9% / +11% | — | — | — | -6% / -7% |
| byteset_not | 256 | 1 | 122 / 27.7 | +10% / +10% | +5% / +18% | +9% / +8% | +4% / +12% | -2% / +19% | -3% / +9% |
| byteset_not | 256 | 0 | 338 / 78.8 | +1% / +0% | +7% / +8% | — | — | — | -6% / -3% |
| byteset_not | 4096 | 1 | 1443 / 377.9 | +1% / -4% | +0% / +10% | +1% / -3% | +0% / +12% | -9% / +14% | -9% / +17% |
| byteset_not | 4096 | 0 | 4659 / 1067.4 | +0% / -0% | +6% / +6% | — | — | — | -6% / +1% |
| byteset_not | 65536 | 1 | 22570 / 7166.5 | -0% / -1% | -0% / -1% | +0% / -2% | +0% / -4% | -9% / +15% | -9% / +17% |
| byteset_not | 65536 | 0 | 73824 / 16555.0 | -0% / +1% | +6% / +7% | — | — | — | -6% / +1% |
| pair | 16 | 1 | 262 / 62.4 | +6% / +13% | +6% / +11% | +5% / +10% | +6% / +9% | +7% / +8% | +6% / +10% |
| pair | 16 | 0 | 250 / 59.3 | +7% / +13% | +7% / +8% | — | — | — | +6% / +11% |
| pair | 64 | 1 | 86 / 22.3 | +12% / +10% | +0% / +3% | +13% / +13% | -2% / -4% | -1% / +8% | -3% / -7% |
| pair | 64 | 0 | 970 / 228.0 | +7% / +18% | +7% / +8% | — | — | — | +7% / +8% |
| pair | 256 | 1 | 176 / 43.4 | +6% / +2% | +3% / +2% | +6% / +2% | +2% / +2% | -0% / +5% | -2% / -6% |
| pair | 256 | 0 | 3851 / 924.8 | +7% / +12% | +7% / +8% | — | — | — | +7% / +8% |
| pair | 4096 | 1 | 1977 / 466.7 | +1% / -2% | +6% / +4% | +1% / +2% | +6% / +5% | -0% / -1% | -0% / -0% |
| pair | 4096 | 0 | 61457 / 14417.3 | +7% / +14% | +7% / +7% | — | — | — | +7% / +7% |
| pair | 65536 | 1 | 30776 / 8316.3 | +0% / -1% | +7% / +3% | +0% / -0% | +7% / +2% | -0% / +11% | -0% / +10% |
| pair | 65536 | 0 | 983193 / 230803.0 | +7% / +14% | +7% / +8% | — | — | — | +7% / +8% |
| teddy | 16 | 1 | 391 / 93.3 | -12% / -5% | -13% / -13% | -17% / -11% | -19% / -19% | -18% / -19% | -8% / -10% |
| teddy | 16 | 0 | 391 / 93.6 | -12% / -6% | -13% / -13% | — | — | — | -8% / -10% |
| teddy | 64 | 1 | 135 / 35.2 | -2% / +0% | -5% / -3% | -14% / -13% | -23% / -20% | -16% / -15% | -20% / -17% |
| teddy | 64 | 0 | 227 / 55.4 | -0% / +0% | -6% / -2% | — | — | — | -22% / -17% |
| teddy | 256 | 1 | 297 / 93.4 | -3% / +2% | -6% / +2% | -8% / +0% | -14% / -0% | -11% / +0% | -13% / -1% |
| teddy | 256 | 0 | 683 / 164.1 | +2% / +2% | -5% / -2% | — | — | — | -18% / -11% |
| teddy | 4096 | 1 | 3538 / 1442.9 | -4% / -1% | -7% / -1% | -4% / -1% | -8% / -1% | -8% / -0% | -8% / -1% |
| teddy | 4096 | 0 | 9806 / 2364.7 | +3% / +2% | -5% / -3% | — | — | — | -16% / -1% |
| teddy | 65536 | 1 | 55406 / 22775.5 | -4% / +0% | -7% / -0% | -4% / -1% | -7% / -0% | -8% / -1% | -7% / -0% |
| teddy | 65536 | 0 | 155806 / 37281.3 | +3% / +3% | -5% / -3% | — | — | — | -16% / -0% |
