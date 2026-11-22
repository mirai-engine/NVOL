# Trade-offs

Every change that NVOL accepted although some row got worse, in one place. There are two
kinds:

- **Trade-offs:** the new code itself costs more on some rows and less on others, and the
  balance was accepted.
- **Position losses:** a row got worse with code that is not worse. A control shows that only
  its place in memory changed.

Each entry names what was measured, how much each side moved and the evidence. The history and
the raw notes stay where the change is described (the Done list and the items of
[PENDING](PENDING.md)), linked from each entry.

**Machine and method** (unless an entry says otherwise): Intel i5-7400 (Kaby Lake), WSL 2,
`perf stat`, cycles per call = (N calls − 0 calls) / N, best of 5-9. The A/A noise of a row is
up to ±3 % (geomean ±0.3 %). "asm" is NVOL's own assembly (sysv NASM build of
`harness/asm/bench.c`). An "AVX2" or "SSE2" row is the path forced through
`nvol_test_set_avx2`.

## 1. Trade-offs

### 1.1 Summary

| change | what got worse | what got better | why it stays |
|---|---|---|---|
| [`cmov`/`csinv` for a select](#12-cmov-for-a-select-item-17) (item 17) | memchr2 / memchr3 64 B AVX2, ~+1 cycle | ~−1 cycle on most small calls (memchr 16 B AVX2 17.0 → 15.8) | only a single call pays; the iterators call the raw functions, which have no select |
| [regions marked rare, moved out of line](#13-rare-regions-out-of-line-item-17) (item 17) | memmem 4 KiB AVX2, 378-393 → 423-428 | pair −10…−12.5 %, byteset and memchr3 4 KiB AVX2 −9 % and −15 % | that bench finds candidates often, so the "rare" region is common there |
| [return tails copied to each exit](#14-return-tails-copied-item-17) (item 17) | memrchr 64 B AVX2, +1.5-2 cycles | geomean −1.56 % over 104 rows, `.text` smaller | one row against a net gain; no cause found with WSL's counters |
| [memrchr's long loop exit-first](#15-memrchrs-long-loop-exit-first-item-25) (item 25) | memrchr 256-512 B AVX2, ~3 % of code cost | memrchr 4 KiB AVX2 −12…−18 % | long reverse searches gain ~15 %; the maintainer accepted it |
| [Rust `first_non_ascii` inline for 16-31 B](#16-rust-first_non_ascii-inline-for-16-31-b-item-29) (item 29) | 32 B +17 %, 64-256 B +8…+20 % | 16-31 B −65 % (11 → 4 cycles) | the maintainer accepted it; two other forms were worse |
| [SWAR memchr, three needles](#17-swar-memchr-item-28) (item 28) | memchr3 1 B, +1.5 cycles (11.3 → 12.9) | memchr3 3-15 B −7…−53 % | 10 live values, one more than the caller-saved registers |
| [SWAR memchr, word path from 4 B](#17-swar-memchr-item-28) (item 28) | asm memchr / memrchr 4-5 B, +0.5-0.8 cycles | Rust 4-5 B −13…−30 %, ahead of the crate | one threshold serves every backend; Rust's gain is the larger |

### 1.2 `cmov` for a select (item 17)

A `return c ? x : y` is a `cmov` on x86 and a `csel` or `csinv` on aarch64, not a branch.

- **Better:** about one cycle less per small call (memchr 16 B AVX2 17.01 → 15.77, memchr 0 B
  10.77 → 9.90, first_non_ascii 16 B SSE2 17.56 → 16.05).
- **Worse:** memchr2 64 B AVX2 15.4-16.0 → 16.2-16.8, memchr3 64 B AVX2 17.6-17.8 → 18.8-19.2
  (three rounds).
- **Evidence it is the code:** moving the wrapper 0, 16, 32 and 48 bytes into its line does
  not change those rows. The `cmov` puts the select's latency on the result, and a predicted
  branch would hide it.

[Details in PENDING, Done.](PENDING.md)

### 1.3 Rare regions out of line (item 17)

`regalloc.layout_rare` moves a region marked rare (a candidate check in a search loop) to the
end of the function, so the loop has no taken jump around it.

- **Better:** pair 16 B-4 KiB SSE2 −10…−12.5 %, byteset 4 KiB AVX2 −9 %, memchr3 4 KiB AVX2
  −15 %, first_non_ascii 64 B AVX2 −17 %.
- **Worse:** memmem 4 KiB AVX2, 378-393 → 423-428 cycles in both layouts of the control. In
  that bench the packed-pair loop finds a candidate often. The region is not rare there, and
  out of line it costs two taken jumps per candidate.

### 1.4 Return tails copied (item 17)

`regalloc.dup_returns` replaces each jump to a short return tail by a copy of the tail, as
GCC and Clang return in place.

- **Better:** geomean −1.56 % over 104 rows (noise −0.25 %), 17 rows better than −5 %;
  `.text` 177072 → 176688 bytes.
- **Worse:** memrchr 64 B AVX2, +1.5-2 cycles in both layouts of the control, with the same
  µops (56.1). No cause was found with the counters WSL gives.

### 1.5 memrchr's long loop, exit-first (item 25)

From 32 iterations of the unrolled loop (AVX2; 16 for SSE2), the reverse memchr kernels first
run the loop in Clang's shape: the exit test at the top. That removes one misprediction per
call at the exit.

- **Better:** memrchr 4 KiB AVX2 148-157 → 125-136 cycles (−12…−18 %; Clang 117-128).
- **Worse:** memrchr 256 B AVX2 20.0-21.0 → 23.7-24.9, 512 B 25.6-25.9 → 29.2-29.5. Two
  controls split that loss:
  - a 10-byte NOP in the guard's place (a pure shift) gives +0.9…+1.6 cycles;
  - the guard's two instructions add +0.4…+0.9 more, about 3 %.

  Only the guard is code cost. The NOP's share is logged as position below, and the rest of
  the loss is the copy's own shift of the code after it.

### 1.6 Rust `first_non_ascii` inline for 16-31 B (item 29)

The Rust wrapper checks 16-31 bytes with inline SSE2 (the x86_64 baseline). That skips the
dispatch's indirect call and the AVX2 entry's `vzeroupper`, which bstr's inlined static SSE2
does not pay either.

| size | 16 B | 20-31 B | 32 B | 64 B | 256 B | 4 KiB |
|---|---|---|---|---|---|---|
| before | 11.0 | 12.0 | 12.0-12.2 | 12.7-13.1 | 18.0-19.1 | 123 |
| after | **4.0** | **4.0** | 14.0-14.1 | 14.0 | 21.9-22.4 | 100 |
| bstr | 9.0 | 19.8-32.9 | 10.0 | 12.6 | 22.9-23.0 | — |

The 32 B loss is at least partly code (40 → 48 instructions). At 64 B the instructions are
unchanged, so that row is position: Rust's LTO lays the whole binary out anew. Two other forms
were measured and are worse:

- `#[cold]`: no gain (11 cycles at 16-31 B);
- `#[inline(never)]`: 9 cycles, but +25…+33 % from 32 B.

### 1.7 SWAR memchr (item 28)

The scalar kernel (the dispatch's path under 16 bytes, and the path without SIMD) searches
words instead of bytes. Overall: asm geomean −22.3 % over 64 rows (1-16 B, six functions), and
Rust ahead of the memchr crate at 1-2 B and from 4 B up. Two rows pay for it:

- **memchr3 1 B, +1.5 cycles (11.3 → 12.9).** With three needles the u64 check holds 10
  values (three splats, two pointers, the word, the mark, x, t, a 64-bit constant). That is
  one more than x86's nine caller-saved registers, so the function pushes rbx and rbp, and the
  1-byte path pays for it. Two attempts did not remove it:
  - re-reading the word per needle: the lowering's CSE merges the loads back;
  - recomputing `len` from the pointers.

  The general fix is shrink-wrapping (saving callee-saved registers only on the paths that
  use them).
- **asm memchr / memrchr 4-5 B, +0.5-0.8 cycles (15.0 → 15.5-15.8).** There, two u32 checks
  cost about as much as 4-5 byte iterations. The same threshold gives Rust 13.6 → 12.0 at 4 B
  and 17.1 → 12.0 at 5 B, against the crate's 12.5 and 14.3.

One design choice in this change follows the layout, by measurement. The kernel is inlined
into each ISA entry, and there the size test moved memchr2's byte loop across a 64-byte line
(2 B +16.5 %). So two needles check 1-3 bytes in straight-line code. For one needle the
straight-line block overflows a µop-cache window (memchr 2 B +52 %, 1.5 cycles of DSB-to-MITE
switch per call), so one and three needles keep the loop.

## 2. Position losses

The maintainer's rule (2026-11-21): an optimization whose worse row a control shows to be
position only is accepted, and logged here. The control puts the new code at the old
addresses (NOP padding), or regenerates both versions under one layout rule, and shows that
the code alone is not worse. These rows are layout noise that any later edit can move again,
so re-measure them when a row stays bad across unrelated commits. The mechanisms, and the
layout rules that were tried and rejected, are in the
[cycle-gaps report](benchmarks/2026-11-22-cycle-gaps.md).

| commit | row | before → after | control (new code, old addresses) |
|---|---|---|---|
| copy_back (item 17) | memchr 16 B AVX2 | 16.8 → 18.5 | 16.8 |
| copy_back (item 17) | memrchr 64 B AVX2 | 19.9 → 21.6 | 3-byte NOP restores it |
| reloads across labels (item 17) | memmem 256 B SSE2 | 71.5 → 72.4 | 70.8 |
| exit-first memrchr (item 25) | memrchr 256 B AVX2 | 20.0-21.0 → 23.7-24.9 | NOP 22.0-22.4, guard 22.4-23.3 |
| exit-first memrchr (item 25) | memrchr 512 B AVX2 | 25.6-25.9 → 29.2-29.5 | NOP 26.5-26.7, guard 26.8-28.3 |
| functions 64-aligned (item 25) | count_parity 16 / 64 / 256 B SSE2 | 48.7 / 55.1 / 83.8 → 63.2 / 68.2 / 95.1 | entry moved 48 bytes into its line: 48.6 / 54.0 / 84.2 |
| functions 64-aligned (item 25) | count_parity 4 KiB SSE2 | 765.8 → 809.9 | same code (bodies byte-identical) |
| functions 64-aligned (item 25) | byteset 4 / 64 KiB AVX2 | 344.5 / 6041 → 383.8 / 6705 | same code (bodies byte-identical) |
| functions 64-aligned (item 25) | count 64 KiB AVX2, first_non_ascii 64 B AVX2 | 2547 / 17.1 → 2781 / 19.2 | same code (bodies byte-identical) |
| count below one SAD block (item 17) | count 64 B AVX2 | 13.4-13.9 → 16.2-16.8 | same code, loop heads 32-aligned: 11.0 |
| return tails copied (item 17) | memchr 16 B SSE2, count 64 B SSE2 | 13.73 / 15.63 → 15.04 / 16.65 | both 32-aligned: 13.93 → 13.65, 17.04 → 16.98 |
| return tails copied (item 17) | first_non_ascii 4 KiB AVX2 / SSE2 | 92.4 / 210.3 → 98.5 / 236.5 | both 32-aligned: 108.7 → 109.2, 237.2 → 234.9 |
| rare regions out of line (item 17) | first_non_ascii 16 B AVX2 | 14.53 → 15.86 | both 32-aligned: 15.99 → 15.98 |
| `lea` for `(x << k) + y` (item 17) | memmem 256 B / 4 KiB / 64 B SSE2 | 65.9 / 675 / 35.8 → 73.9 / 763 / 39.0 | both 32-aligned: 70.3 → 71.5, 807 → 779, 36.2 → 37.2 |
| SWAR memchr (item 28) | memchr2 16 B AVX2 | 13.7-13.9 → 14.9-15.4 | the vector code is unchanged; HEAD's entry with 665 dead bytes, which put it at the new offset (734): 15.8 |
| SWAR memchr (item 28) | Rust memchr 3 B (`smallcount`) | 12.05 → 13.96 | the same 47 instructions per call as HEAD (the byte loop) |

Notes on the table:

- **64-aligned functions:** no function body changed, so each of those rows is position by
  construction. The count_parity control shows the trade: moved 48 bytes, the SSE2 path gets
  HEAD's cycles back, but the AVX2 path loses its −29 % (81.3 → 116.5 at 64 B).
- **Exit-first rows:** not all position. Of their +3.5-3.8 cycles, the length check itself
  costs 0.4-0.9 (the guard control against the NOP control). That part is the code cost of
  [1.5](#15-memrchrs-long-loop-exit-first-item-25).

## 3. Adding an entry

- **A trade-off:** a row in 1.1 and a section with the before and after of both sides, the
  control that shows the loss is code, and why it stays. If the maintainer decided it, say
  so.
- **A position loss:** a row in section 2 with its control. Without a control it is not a
  position loss; it is an open regression.
