# simdjson's eight-digit parsing: NVOL against simdjson 5.2.0 (2026-11-24)

PENDING item 37, first branch (`feat/json-numbers`). `nvol_parse_eight_digits_sse42`,
`_scalar` and `nvol_is_eight_digits` against simdjson's own `parse_eight_digits_unrolled`
(haswell, westmere, fallback) and `is_made_of_eight_digits_fast`, each inlined into the same
loop.

**Machine:** Intel i5-7400 (Kaby Lake), idle, WSL 2 Arch (glibc), g++ -O2.
**Harness:** [`harness/simdjson-stage2`](../../harness/simdjson-stage2).
- `setup.sh` builds `digitsbench`. Its unit includes simdjson's amalgamated `simdjson.cpp`,
  where each implementation's code is, and NVOL's C++ header.
- Each pass is a `noinline` function per implementation, with that implementation's target:
  - haswell: `avx2,bmi,pclmul,lzcnt,popcnt`;
  - westmere: `sse4.2,pclmul,popcnt`;
  - fallback: none.

  The routine is inlined in it, over 4096 random eight-digit numbers.
- `same` checks that every implementation gives the same sums. It also checks that both
  digit tests agree on '5' × 8 with each of the 256 byte values at each position.
- `counters.sh`: per call, (20000 passes − 0 passes) / (20000 × 4096), the best of 15 runs
  per counter. Two modes:
  - `tput`: independent calls, summed;
  - `chain`: `acc = acc * 10^8 + f(p)`, the dependence of simdjson's `parse_fraction_digits`.

## Result

| routine | impl | tput instr | tput cycles | chain instr | chain cycles |
|---|---|---:|---:|---:|---:|
| parse, haswell target | simdjson | 10.01 | 3.027 | 11.01 | 4.016 |
| | NVOL sse42 | 11.01 | 3.024 | 12.01 | 4.013 |
| parse, westmere target | simdjson | 11.01 | 3.126 | 12.01 | 4.014 |
| | NVOL sse42 | 11.01 | 3.023 | 12.01 | 4.012 |
| parse, SWAR | simdjson fallback | 14.01 | 3.843 | 15.01 | 4.089 |
| | NVOL scalar | 14.01 | 3.844 | 15.01 | 4.089 |
| is eight digits | simdjson | 13.01 | 3.451 | 14.01 | 4.096 |
| | NVOL | 13.01 | 3.449 | 14.01 | 4.096 |

The westmere `tput` row was repeated three times. NVOL gave 3.023 / 3.022 / 3.025 and
simdjson 3.126 / 3.125 / 3.122.

**Verdict: parity or better.**
- SWAR and the digit test compile to the same instructions as simdjson's.
- The SSE4.1 sequence is also the same, but for the load:
  - **NVOL:** `movq xmm, [p]` (8 bytes), then `paddb`.
  - **simdjson:** a 16-byte load.
    - haswell folds it into `vpaddb xmm, xmm, [p]`: one instruction fewer than NVOL;
    - westmere uses `movdqu` + `paddb`: the same count as NVOL.

## Why simdjson's westmere row is 3.4 % slower

With the numbers 8 bytes apart, one 16-byte load in eight crosses a cache line.
`mem_inst_retired.split_loads` per call, one run of each:

| impl | split loads | cycles |
|---|---:|---:|
| simdjson haswell | 0.125 | 3.078 |
| NVOL sse42 at haswell | 0 | 3.084 |
| simdjson westmere | 0.125 | 3.179 |
| NVOL sse42 at westmere | 0 | 3.093 |

The split costs nothing visible when the load is folded into `vpaddb` (haswell), and about
0.1 cycles per call when it is a separate `movdqu` (westmere). NVOL's 8-byte load never splits
a line: the routine reads exactly the bytes of the number.

The extra instruction NVOL has against simdjson's haswell routine is the price of that bound.
It costs no cycles in either mode, so no 16-byte variant for padded callers was added.
