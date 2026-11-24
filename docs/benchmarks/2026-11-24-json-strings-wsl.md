# simdjson's string copy: NVOL against simdjson 5.2.0 (2026-11-24)

PENDING item 37, second branch (`feat/json-stage2-strings`). The two implementations compared:
- **NVOL:** `nvol_json_copy_and_find_avx2` and `_sse2`;
- **simdjson:** its own `backslash_and_quote::copy_and_find` (haswell, westmere).

Each one is inlined into the same loop.

**Machine:** Intel i5-7400 (Kaby Lake), idle, WSL 2 Arch (glibc), g++ -O2.

**Harness:** [`harness/simdjson-stage2`](../../harness/simdjson-stage2).
- `stringsbench` steps through a document 32 bytes at a time. Each step copies to a buffer and
  adds the two masks to a running sum. Each implementation gets a `noinline` pass with its
  target (haswell or westmere).
- `same` checks that every implementation gives the same masks and the same copy.
- `strings.sh` measures per step: (2000 passes − 0) / (2000 × steps), the best of 15 runs per
  counter.

## Result

Cycles per 32-byte step, two runs:

| file | impl | instr | run 1 | run 2 |
|---|---|---:|---:|---:|
| citm_catalog | simdjson haswell | 12.00 | 5.747 | 5.754 |
| | NVOL avx2 | 12.00 | 5.751 | 5.665 |
| twitter | simdjson haswell | 12.00 | 5.598 | 5.612 |
| | NVOL avx2 | 12.00 | 5.623 | 5.609 |
| citm_catalog | simdjson westmere | 24.00 | 6.614 | 6.628 |
| | NVOL sse2 | 24.00 | 6.586 | 6.627 |
| twitter | simdjson westmere | 24.00 | 6.486 | 6.511 |
| | NVOL sse2 | 24.01 | 6.461 | 6.487 |

**Verdict: parity.**
- **AVX2:** NVOL's loop is byte-identical to simdjson's. Between runs the sign of the
  difference flips; the spread is ±2 %.
- **SSE2:** NVOL now gives simdjson's very instructions too.

## The SSE2 row before parity

The first measurement had NVOL at +0.3 % to +0.8 % in the westmere target on citm_catalog,
over four runs. Both loops had the same 24 instructions, in a different order.

Recompiling with `-falign-loops=16/32/64` kept the difference:

| alignment | NVOL | simdjson |
|---|---:|---:|
| 16 | 6.663 | 6.636 |
| 32 | 6.657 | 6.629 |
| 64 | 6.616 | 6.586 |

So it was not loop placement. The cause was the C lowering of `bmask64x4`:
- NVOL cast every movemask through `uint32_t`;
- simdjson casts only the first; the other three convert from `int`.

The value is the same either way, but GCC orders the `pmovmskb`/`or` chain differently. With
simdjson's casts it gives the same instructions as simdjson's, in the same order (commit
"Write bmask64x4's C lowering with simdjson's casts"; it is neutral on the SSSE3 stage 1).

## Not ported

- **simdjson's fallback `copy_and_find`:** it steps one byte at a time, so there is no 32-byte
  scalar kernel to match.
- **The block's `is_ascii`:** only simdjson's UTF-8 checker calls it, and that is already
  item 36's ASCII path, in `nvol_json_index_raw` and `nvol_utf8_valid`.
