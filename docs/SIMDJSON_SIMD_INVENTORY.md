# SIMD inventory of simdjson (v5.2.0) versus NVOL

simdjson compiles the same generic code (`src/generic/`) once per architecture (haswell,
icelake, westmere, arm64, fallback, ppc64, lsx, lasx, rvv) and chooses one at runtime.
It reads the input in 64-byte blocks with guaranteed padding (`SIMDJSON_PADDING`), so its
routines do not search for "the first one": they turn each block into 64-bit masks and continue
with bit arithmetic. This is a different model from that of ripgrep, ugrep and rapidjson.

| Routine (haswell) | What it does | NVOL equivalent |
|---|---|---|
| `json_character_block::classify` | masks of whitespace and of `{}[]:,` in the block: one `pshufb` + `cmpeq` per class, with tables built so that each member has its own low nibble (`\| 0x20` merges `[` with `{` and `]` with `}`) | none: the NVOL byteset gives the mask only for the block with candidates and uses two nibble searches |
| `json_string_scanner` | masks of `\` and `"` in the block | plain `cmpeq`; NVOL does not expose per-block masks |
| `json_escape_scanner` | which `"` are escaped (bit arithmetic with additions) | scalar, not applicable |
| `prefix_xor` | string interiors (`pclmulqdq` against all ones) | none |
| `utf8_lookup4_algorithm` | UTF-8 validation (three nibble lookups and continuation checks) | none (PENDING 1.13) |
| `json_minifier` | `classify` + compact the block | none |
| `backslash_and_quote::copy_and_find` (stage 2) | copies 32 bytes and gives the masks of `\` and `"` | `nvol_copy_until_byteset` copies while it searches and gives the position of the first member (not the masks): it works where the first hit is used |
| `parse_eight_digits_unrolled` | 8 ASCII digits to integer (`maddubs`, `madd`, `packus`) | none |
| `is_ascii` | OR of the 64 bytes and `movemask` | none |

Conclusion: no simdjson routine can be replaced today with a call to NVOL. To do
that, NVOL needs a new kernel family, from fixed block to mask, instead of searching
up to the first hit:

1. **Membership mask of a 64-byte block**, choosing the technique according to the set: a
   single `pshufb` + `cmpeq` when each member has a distinct low nibble (or one can be obtained
   with a fixed OR, as simdjson does); otherwise, Muła's nibble search that NVOL already has.
   The single-`pshufb` search already exists in NVOL for "first byte" (the
   *unique-nibble* sets of `byteset`, such as the 4 JSON whitespace bytes); the variant that
   returns the block mask is missing.
2. **Multi-byte equality masks per block** (`\` and `"`).
3. **`prefix_xor`** with carry-less multiplication (`pclmulqdq` / `pmull`) and its scalar
   alternative.
4. **UTF-8 validation** (PENDING 1.13), which ugrep also lacks.
5. **8 digits to integer**.

With 1 to 3, simdjson's stage 1 (which does most of the SIMD work) would sit on NVOL.

## What the generic code needs from each architecture

Checked on 2026-11-22 against simdjson 5.2.0 (`a0d5ad9`, still the current release), in the
haswell layer that `src/generic/` calls: `include/simdjson/haswell/simd.h`, `bitmask.h`,
`bitmanipulation.h`, `numberparsing_defs.h` and `stringparsing_defs.h`. The haswell code is
compiled with `avx2,bmi,pclmul,lzcnt,popcnt`. "NVOL" is what the IR and its primitives have
today; the last column is the PENDING item (phase 6) that adds what is missing.

| operation | where simdjson uses it | NVOL | item |
|---|---|---|---|
| `cmpeq`, `or`, `and`, `movemask`, `testz` | everywhere | `cmpeq`, `vor`, `vand`, `movemask`, `is_zero` | — |
| vector `xor`, `andnot` | `operator^`, `bit_andnot`, `operator~` | neither | 33 |
| integer `xor`, `andnot` | escape and string scanners | `xor` (since 2026-11-22); no `andnot` | 33 |
| 64-bit mask of a 64-byte block | `simd8x64::to_bitmask` (two `vpmovmskb`) | a `u32` per vector on x86 (NEON: `u64`) | 33 |
| signed `>`/`<`, signed `max`/`min` | `simd8<int8_t>` | none | 33 |
| saturating `+`/`-` on `u8` | `gt_bits`, `lt_bits` (UTF-8) | none | 33 |
| byte shift by N with its mask | `shr<N>`, `shl<N>`, `get_bit<N>` | `vshr4` only | 33 |
| `prev<N>` (bytes from the previous block) | UTF-8 (`prev<1..3>`) | `shin1-3`, `hshin1-3` within one vector | 33 |
| `lookup_16` (`pshufb` on a 16-entry table) | `classify`, UTF-8 | `vshuffle` | — |
| `popcnt` on `u64` | structural indexer (`count_ones`) | `popcnt` on `u32` | 33 |
| `tzcnt`, `lzcnt` on `u64` | indexer, `copy_and_find` | `first_offset64`, `last_offset64` | — |
| `blsr` (`x & (x - 1)`) | indexer (`clear_lowest_bit`) | expressible (`wsub`, `and`) | — |
| sign smear `int64(x) >> 63` | string scanner (`prev_in_string`) | expressible as `0 - (x >> 63)` | 33 |
| store of `u32` indices | indexer (`write_index`) | `Store8`, `Store64`, `StoreVec` | 33 |
| carry-less multiplication | `prefix_xor` (`_mm_clmulepi64_si128` against all ones) | none | 34 |
| `pmaddubsw`, `pmaddwd`, `packusdw` (128 bits) | `parse_eight_digits_unrolled` | none | 37 |
| unaligned 32-byte copy plus two masks | `copy_and_find` | `nvol_copy_until_byteset` (copy plus the first member) | 37 |
| `compress` (`thintable_epi8`, `pshufb_combine_table`) | the minifier | none | not in phase 6 |
| 64×64→128-bit multiplication | `full_multiplication` (floating-point numbers) | none | not in phase 6 (scalar) |

The block contract differs from NVOL's. simdjson reads whole 64-byte blocks past the end of
the document because it guarantees `SIMDJSON_PADDING`, while NVOL's kernels read only their
bytes. So NVOL's block kernels take a pointer to 64 readable bytes, a precondition like the
vector kernels' `end - start >= V`. The caller, simdjson's `buf_block_reader`, already copies
the last partial block into a padded buffer.
