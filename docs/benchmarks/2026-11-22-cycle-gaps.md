# Cycle gaps with the same instructions: where they stand (2026-11-22)

PENDING item 25, with items 27, 28, 29 and 32 of phase 5. This report collects what the
investigation in PENDING found over several sessions: the mechanisms that are established, the
gaps still open with their evidence and what has been ruled out, and what would close each one.
PENDING keeps the raw notes; this is the reading of them.

**Machine:** Intel i5-7400 (Kaby Lake, 4 cores), Windows 10 with WSL 2 (Alpine for musl and
NASM 3.01, Arch for glibc and a `perf` that knows the frontend events). No ARM, no AMD, no
native Linux (PENDING 2.12).

## 1. How a gap is judged

A row is "the same instructions, more cycles" when the instructions per call differ by under
~3 % and the cycles by more than the noise. Instructions are exact. Cycles are the best of 5-9
runs of N calls minus the same with 0 calls. Two copies of the same binary differ by up to
±3 % per row (geomean ±0.3 %).

Five kinds of control separate the code from its placement:

| control | what it holds fixed | used for |
|---|---|---|
| **A/A copy** | an identical binary measured beside the others | the noise of each row |
| **NOP control** | the new code moved back to the old addresses with NOP padding | "is the new code worse, or its place?" |
| **Prefix control** | the old code with ignored prefixes (`0x2e` before `bsf`), so every function keeps the new lengths and addresses | `tzcnt` against `bsf` |
| **Common-rule regeneration** | HEAD and the change both regenerated with every loop head 32-aligned | an in-function change, where labels are offsets and bytes cannot be inserted by hand |
| **Offset scan** | one function moved 0-48 bytes within its cache line | which window decides |

A row that a control shows to be placement only is accepted and logged (the maintainer's
rule, PENDING item 25, "Accepted position losses": 14 rows so far, each with its control).

## 2. What is established

### 2.1 The µop cache (DSB) decides the small calls

Moving `nvol_memchr`'s call site by single bytes (musl binary) changes memchr 16 B AVX2 from
16.8 to 19.6 cycles with identical instructions. Frontend counters explain it. At the fast
offsets every µop comes from the DSB. At the slow ones, 10-20 µops per call come from the
legacy decoder (MITE), and one offset pays about 3 cycles of DSB-to-MITE switch penalty. In the
glibc binary no call offset is fast; moving the *callee* is (16.6 at 2-6 bytes in, MITE from 16
bytes in). Skylake's DSB holds at most 3 ways of 6 µops per 32-byte window; a window that
overflows falls back to MITE.

### 2.2 A loop that straddles a window costs about one µop per iteration

memmem 4 KiB AVX2 swung 30 % on placement alone (559.5 against 386.8 cycles, same code). In the
slow layout the packed-pair loop crosses a 32- and 64-byte boundary and issues ~117 more µops
per call, about one per iteration, with the same mispredictions and no MITE.

### 2.3 Layout made stable, not better

A rule that tries to place loops well was rejected twice: each version was another draw in the
same lottery. What was adopted instead is that a draw stays put (2026-11-22). Every function
starts on a 64-byte line, so an edit to one function no longer moves another. Before, 7 dead
bytes early in the file moved memmem SSE2 64 KiB by +33 %. After, a 64-byte shift of every
later function moves no row beyond the A/A noise.

### 2.4 The JCC erratum, in a compiler's layout

In the Rust build, memchr 3-15 B (item 28) lost 1-2.5 cycles to the crate: the loop exit's `jmp`
crossed a 32-byte boundary, 2-5 MITE µops and up to 1.1 cycles of switch penalty per call.
LLVM's `-x86-branches-within-32B-boundaries` removes it, and NVOL then leads at 3, 8, 12 and
15 B. This belongs to the user's build flags, not to the generated code (TOOLCHAINS, T3: Odin
cannot pass the flag).

The erratum does **not** explain the other compiler rows (PARITY P4): GCC with GNU as's
mitigation (`-Wa,-mbranches-within-32B-boundaries`) gets no faster (first_non_ascii 4 KiB
95.0 → 103.5, memchr 256 B 27.8 → 30.7).

### 2.5 A loop's shape decides the exit misprediction

memrchr 4 KiB AVX2 was +20 % against Clang with fewer instructions. The asm mispredicted the
loop exit once per call and Clang did not. Clang's shape (exit test at the top) removes the
misprediction once the loop runs about 31 times, but costs about 3 cycles when it runs once or
twice. Adopted as an asm-only loop attribute (`While.exit_first`, from 32 iterations), with a
recorded trade-off: −12…−18 % at 4 KiB, about 3 % at 256-512 B.

## 3. What is still open

| gap | measured | ruled out | what would close it |
|---|---|---|---|
| **memrchr 16-64 KiB** (both paths): asm 5607-5710 against GCC 4796-4925 cycles at 64 KiB SSE2, 504-525 against 445-468 at 16 KiB AVX2, same instructions | 3× the RS-full stalls (`resource_stalls.rs`: 1044 against 365 cycles per call in SSE2) | memory (the same L1 replacements, L2 reads and prefetches); the loads' order; GCC's folded-load operand form (worse: 6062-6101); the JCC erratum; 16/32/64 alignment | the port-level dispatch of the loop (`uops_dispatched_port.*`, 4 per run) against GCC's, and a hand-edited asm in GCC's exact schedule |
| **memmem SSE2 4 KiB**: 676-807 cycles on placement alone; GCC 660-710 | the same instructions (2916/2918), branches (527) and mispredictions (1.0/1.2); 3.3 against 2.8 cycles per 16-byte iteration in the slow layout | the frontend (MITE ≈ 0); the loads' order; `tzcnt` | a layout where the packed-pair loop sits inside one 32-byte window by construction; a window-aware pad was tried and rejected (below) |
| **byteset AVX2 4-64 KiB**: +11…+18 % with the same instructions (also in Rust and Odin, PARITY P11) | all µops from the DSB | the JCC erratum; a window-aware pad (below) | a model that predicts the slow layout |
| **memchr 4-6 B in Rust** (item 28): 16.0 / 17.1 / 14.1 cycles at 4 / 5 / 6 B against 12.5 / 14.9 / 14.5 for the crate, JCC-safe build | fewer µops issued than the crate (45/48/51 against 48/51/54), no MITE, no mispredictions, no recovery cycles, no resource stalls | the JCC erratum (that build is mitigated) | not visible to the counters WSL gives: a native machine, or the SWAR path for 1-15 B (below), which removes the byte loop |
| **rapidjson `pretty`** (14b): +0.0 / +0.8 / +1.5 % cycles in 3 rounds | within the row's own ±1 % spread; DSB +3 %, MITE −6 %, switch penalty −11 % | — | nothing to close at this resolution; kept with its spread recorded |
| **PARITY P4 / P11**: the languages' rows with the same instructions and +13…+41 % cycles | the same instruction stream | the JCC erratum (2.4) | as above: placement in each compiler's layout, which NVOL does not control |

**One mechanism behind most of the open rows, and the rule that was tried for it.** Except
memrchr's RS stalls and memchr 4-6 B, every open row looked like placement in the µop cache:
a loop or an entry split across 32-byte windows. The generator knows every instruction's
length (`encode.py`), so a window-aware pad was built and measured on the full matrix.

The pad gave each innermost loop the fewest 32-byte windows and the fewest jumps on a window
boundary. It was rejected in both variants: geomean **+0.99 %** and **+0.84 %** against an A/A
of −0.3 %. Each variant won about as many rows as it lost (−12…−19 % on some, up to +43 % on
others), the same lottery as 2.3.

Its worst row says why. count_parity 64 B AVX2 went from 85 to 122 cycles. It issued the same
µops from the same sources, with no mispredictions, while memory-stall cycles rose from 4 to 28
per call. A model of windows and boundary jumps does not see that cost. So these rows stay
accepted placement until some model predicts the slow layout before it is measured. The
numbers are in PENDING item 25, "A window-aware loop-head pad, tried and rejected".

## 4. Items 27, 28, 29 and 32

| item | state | evidence |
|---|---|---|
| **27** Teddy over short haystacks | **closed** | `nvol_teddy_slow` is aho-corasick's Rabin-Karp: on par in cycles at 8-18 B (−20…+7 %), and equal or below in instructions in all 24 rows; from 64 B up, below aho in instructions in every row ([report](2026-11-21-rust-counters.md)) |
| **28** memchr 1-15 B | cause found; 4-6 B open (above) | the JCC erratum in LLVM's layout (2.4). Still to build: memchr's SWAR path for 1-15 B and for the path without SIMD, a feature of its own with its measurement |
| **29** `first_non_ascii` against bstr | **closed** | −20 % at 256 B, −43 % from 4 KiB; the +2 cycles at 16 B were the indirect call, fixed for Rust with an inline SSE2 path for 16-31 B (−65 %, with a recorded trade-off at 32 B). The "none" semantics already match bstr's public API (`None`); changing it is the maintainer's decision |
| **32** ugrep `-c zq` | measured, residual recorded | +0.4…+0.8 % over the process, +17 % in the kernel for a 2-byte needle. The asm-only 2-byte kernel is 14-21 % faster, but ugrep builds the C with GCC, whose general loop is already well scheduled. The residual is GCC's C of the general kernel against ugrep's inline `chars<2>` loop |
