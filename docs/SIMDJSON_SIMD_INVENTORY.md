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
