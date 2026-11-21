# rapidjson end to end on NVOL — i5-7400 (AVX2), WSL2 Alpine 3.24 (Linux)

rapidjson `297637b` (master, 2026-11-03), Alpine 3.24 g++, `-O2 -std=gnu++17`. rapidjson is
header-only. The same program (`harness/rapidjson-e2e/jsonbench.cpp`) is compiled five times
against one tree with `rapidjson-297637b.patch` applied. Without `RAPIDJSON_NVOL`, the patch
changes nothing.

| Build | Flags | SIMD |
|---|---|---|
| base | — | none (rapidjson's default) |
| sse2 | `-DRAPIDJSON_SSE2` | original, SSE2 |
| sse42 | `-DRAPIDJSON_SSE42 -msse4.2` | original, `pcmpistri` for whitespace |
| nvol | `-DRAPIDJSON_NVOL` + `nvol_impl.cpp` | NVOL (`nvol.hpp`): AVX2 if the CPU has it, chosen once at startup |
| nvol42 | `-DRAPIDJSON_NVOL -msse4.2` + `nvol_impl.cpp` | NVOL, compiled like `sse42`: the whitespace skips use the `pcmpistri` kernel inline |

```
sh harness/rapidjson-e2e/setup.sh <rapidjson repo>                  # in WSL
sh harness/rapidjson-e2e/check.sh <rapidjson repo> <simdjson repo>
python3 harness/rapidjson-e2e/bench.py --rounds 21 [--stat min]
```

## What was replaced

rapidjson's SIMD consists of two searches, each with several copies (SSE2, SSE4.2, NEON):

| Routine | Searches for | Now |
|---|---|---|
| `SkipWhitespace_SIMD(p)` and `(p, end)` | the first byte that is not `' '`, `\n`, `\r` or `\t` | `byteset` with the complement (252 bytes) |
| `ScanCopyUnescapedString` (×2), `SkipUnescapedString` (read) | the first `"`, `\` or byte < 0x20 | `byteset` (34 bytes) and a single copy (`memcpy`/`memmove`) |
| `Writer::ScanWriteUnescapedString` | same, when writing | same |

rapidjson reads `'\0'`-terminated text without knowing its length. Both sets contain `'\0'`, so
the patch searches in stretches that end at the 4 KiB page boundary. This way no read crosses
into a page the text does not reach. The original version achieves the same with 16-byte aligned
reads.

The caller side has two details:

- Between 35 % and 52 % of whitespace runs are one byte long (`": "`, `", "`). The original
  version consumes the first byte and stays in its inline loop. The patch also checks the second
  byte before calling NVOL, because calling only to confirm it cost 5-12 %.
- The kernels and the dispatch live in their own translation unit: the one that defines
  `RAPIDJSON_NVOL_IMPLEMENTATION` before including `nvol_compat.h` (`nvol_impl.cpp`, like
  simdjson's `simdjson.cpp`). The rest of the program sees `pure` declarations. See "Known
  length" below: this is what closed `parse_len`.
- The classes are built at program start (global variable). At the same moment the search for
  the CPU is chosen (a function pointer). If something parses from another static initializer
  before that, the pointer still points to a simple loop (constant initialization, which precedes
  every constructor). `prestatic.cpp` tests this. Building them on first use (local `static`
  variable) cost 3-11 % because of the *guard* on every call.

## Correctness

`check.sh` runs the five builds over 535 files:

- JSONChecker (`pass*` / `fail*`);
- rapidjson's data and schemas;
- simdjson's `jsonexamples` and `tests`;
- the corpus below;
- 400 random documents (`fuzz.py`): strings of 0-300 bytes with escapes, UTF-8 and invalid
  control bytes, and runs of 0-80 bytes of the four whitespace characters.

Per file and operation it compares the error code and position, or the length and hash of the
output: **2067 identical results** across the five builds (218 errors and 1849 valid documents).
Then it runs `prestatic`, which parses before the classes exist.

## Timings

21 interleaved rounds, µs per document. Other processes were loading the machine during the
measurement (Windows at 100 %). So the table takes **the minimum** of each build (`--stat min`).
The minimum is robust against interference that only adds time. Even so there is ±3-5 % noise:
the three original builds, with the same logic, differ by that much. A negative value means NVOL
was faster.

| corpus | op | sse2 | sse42 | nvol | vs sse2 | vs sse42 |
|---|---|---|---|---|---|---|
| twitter_pretty | parse | 2308 | 2365 | 1917 | **−16.9 %** | **−19.0 %** |
| twitter_pretty | insitu | 1667 | 1593 | 1255 | **−24.7 %** | **−21.2 %** |
| twitter | parse | 2200 | 2306 | 2010 | −8.7 % | −12.8 % |
| twitter | insitu | 1559 | 1574 | 1238 | **−20.6 %** | **−21.3 %** |
| twitter_min | parse | 1922 | 1972 | 1762 | −8.3 % | −10.6 % |
| citm_catalog | parse | 3102 | 3072 | 2897 | −6.6 % | −5.7 % |
| citm_catalog | insitu | 2926 | 2807 | 2679 | −8.4 % | −4.6 % |
| citm_min | parse | 2514 | 2602 | 2490 | −1.0 % | −4.3 % |
| text_lines | insitu | 5561 | 5836 | 5463 | −1.8 % | −6.4 % |
| text_lines | parse | 12215 | 12353 | 13106 | +7.3 % | +6.1 % |
| twitter_pretty | parse_len | 2141 | 2088 | 2166 | +1.1 % | +3.7 % |
| twitter | parse_len | 2040 | 2095 | 2201 | +7.9 % | +5.0 % |
| citm_catalog | parse_len | 2794 | 2599 | 2919 | +4.5 % | **+12.3 %** |
| twitter | write | 1291 | 1296 | 1260 | −2.4 % | −2.8 % |
| twitter_pretty | write | 1262 | 1266 | 1259 | −0.3 % | −0.6 % |
| citm_catalog | write | 1723 | 1710 | 1781 | +3.4 % | +4.1 % |
| text_lines | write | 8548 | 8751 | 9140 | +6.9 % | +4.4 % |
| twitter | pretty | 1588 | 1711 | 1625 | +2.4 % | −5.0 % |
| citm_catalog | pretty | 2699 | 2764 | 3002 | +11.2 % | +8.6 % |

Operations:

- `parse`: `Parse` on `'\0'`-terminated text, the normal case in rapidjson. It uses both searches.
- `parse_len`: `Parse(str, len)`. It uses only the whitespace skip.
- `insitu`: `ParseInsitu`.
- `write` / `pretty`: `Writer` / `PrettyWriter`.

(Against `base`, without SIMD, NVOL is 2-73 % faster at reading and 10-60 % faster at writing long
strings.) The previous measurement, with medians and less load, gave the same picture: `parse`
−2…−22 %, `insitu` up to −25 %, `parse_len` +2…+8 %, writing 0 to +7 %.

**Better:** reading with `'\0'` (`parse`, `insitu`), the default path, is between 1 % and 25 %
faster on all real JSON documents. The main contributors are copying each string stretch in a
single operation and skipping one-byte runs. The exception is `text_lines`, an array of text
lines of 70 bytes on average: there `parse` ties or loses up to 7 %.

**Worse:**

- **`parse_len` (+1 to +12 %).** See the audit: it is not the kernel.
- **Writing (−5 to +11 %).** The strings of `citm` and `twitter` are short (< 40 bytes) and the
  patch makes two passes: search, then `memcpy`. The original version copies while it searches.
  Fixed later: see "Counters" below.

## Counters (after `copy_until` and the range kernel)

With wall-clock time, `parse` (unchanged code) moved ±5-13 % between two builds. That is not
enough to decide 2 % differences. WSL2 exposes the hardware counters, so the measurement uses
`perf stat`: `jsonbench count OP N` runs an operation N times, and `counters.sh` subtracts the
run with N = 0 and takes the minimum of 3 repetitions. Cycles vary < 1 % between runs.

Changes in this batch: writing uses `nvol_copy_until_byteset` (copies while it searches, one
pass). The string set (`0x00-0x1F`, `"`, `\`) uses the range kernel `_r2`
(`min(x, 0x1F) == x` and two `cmpeq`) instead of the nibble lookup (3 `pshufb`).

Cycles per operation, NVOL against SSE2 (negative = NVOL better):

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −8.1 % | +4.0 % | −7.3 % | +0.1 % | +2.2 % |
| citm_min | −4.7 % | +2.6 % | +1.6 % | −0.4 % | +2.3 % |
| twitter | −15.7 % | +3.5 % | −20.1 % | −10.3 % | −6.7 % |
| text_lines | +2.5 % | +1.1 % | −5.7 % | −19.4 % | −19.5 % |

- **Writing:** from +3…+11 % to −20…+2 %. In `citm` the remainder (pretty +2.2 %) is not in NVOL
  code. Almost all its strings are shorter than 16 bytes and never reach the kernel. The sample
  difference is in `Writer::Prefix`, which the patch does not touch (the patch changes how GCC
  inlines and places the writer code).
- **`citm_min` insitu:** with the wrapper calling the nibble kernel it gave +5.9 %; with `_r2`,
  +1.6 %. The remaining cost is the per-call setup (short strings, one call each).
- **`parse_len`** was still +1…+4 %: solved later, see "Known length".

```sh
# in WSL, after setup.sh (needs perf: apk add perf)
N=100 R=3 sh harness/rapidjson-e2e/counters.sh "sse2 nvol" "citm_catalog citm_min twitter text_lines"
```

## Known length (1.16a)

`parse_len` gave +1…+4 % cycles against SSE2 even on `twitter_min`, which has no whitespace
outside strings. Callgrind (exact instructions per source line, builds with `-g`) showed that the
difference was not in the kernel. It was in how GCC compiled the parser around the call. There
were two causes, plus a minor third one:

1. **The kernel call is opaque.** The AVX2 kernel cannot be inlined into a parser compiled for
   baseline x86-64. Facing an unknown call, GCC assumes it may write to memory. So after each
   whitespace skip it rereads the stream (`src_`, `end_`) and rechecks the parse error
   (`IsError()`). In SSE2, the inline loop calls nothing and those checks disappear. Declaring
   the searches `pure` (they only read memory) removes them.
2. **NVOL code used up the unit's inlining budget.** With `nvol.hpp` in the same translation unit
   as the parser, GCC estimates the unit at 12070 instructions against 8790 for SSE2. GCC limits
   how much a unit can grow through inlining (`inline-unit-growth`, 40 %), and NVOL hit that
   limit. `-fopt-info-inline-missed` reports it for `MemoryPoolAllocator::Malloc`, `Consume` and
   `Stack::Push`, which stayed out of line. With `pure` and `--param inline-unit-growth=1000`,
   `parse_len` ties. The fix without flags is simdjson's: the kernels live in their own unit
   (`nvol_impl.cpp`, which defines `RAPIDJSON_NVOL_IMPLEMENTATION`) and the parser sees only
   declarations.
3. **Per-call setup.** The dispatch checked AVX2, then checked whether the classes were built,
   and made a second call: about 7 instructions per whitespace run. Now the class constructor
   chooses the search once (a pointer that points to a simple loop before that), and
   `find_checked` reduces to an indirect jump.

Tried and discarded:

| Variant | Result |
|---|---|
| The rest of the run (from the 3rd byte) in an out-of-line function | Worse (+1-2 % instructions): the inline code size was not the cause. |
| The first 16 bytes inline with NVOL's SSE2 kernel (`find_not_byteset_sse2_k4`) | Worse (`citm_catalog` +13 % instructions): the kernel builds its four constants with one broadcast per call (SSE2 has no `vpbroadcastb`), and the code repeats at every inlined site. |

Along the way, the generator now emits the k-chains and the 16-byte ranges with the `sse2` ISA
instead of `ssse3`. They only use `cmpeq`/`pminub`, so they can be inlined into baseline code.
The x86 entry without AVX2 uses them instead of falling back to scalar code.

Result, `N=100 R=5 counters.sh "sse2 sse42 nvol"`, cycles per operation (negative = NVOL
better):

Against SSE2:

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −12.1 % | −0.6 % | −10.4 % | −0.3 % | −0.3 % |
| citm_min | −8.7 % | −1.1 % | −2.5 % | −1.2 % | −2.9 % |
| twitter | −17.8 % | −1.0 % | −23.2 % | −12.5 % | −10.9 % |
| twitter_min | −10.7 % | −0.5 % | −17.1 % | −12.4 % | −5.9 % |
| text_lines | −0.8 % | +0.5 % | −7.6 % | −21.1 % | −19.8 % |

Against SSE4.2 (`-msse4.2`: `pcmpistri` for whitespace):

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −9.8 % | **+3.9 %** | −8.1 % | +0.9 % | −1.1 % |
| citm_min | −7.0 % | −1.3 % | −3.0 % | +1.2 % | −0.9 % |
| twitter | −17.5 % | +0.2 % | −22.3 % | −10.8 % | −10.3 % |
| twitter_min | −11.0 % | −0.1 % | −17.7 % | −10.3 % | −4.2 % |
| text_lines | +0.3 % | +1.4 % | −5.8 % | −20.8 % | −20.2 % |

- **Against SSE2**, everything is equal or better. `text_lines` `parse_len` (+0.5 %) and `parse`
  are within noise: over six repetitions, `parse` gave between −0.9 % and +0.8 %.
- `citm_min` insitu (+1.6 % before) and `citm` `pretty` (+2.2 %) also closed: they had the same
  causes.
- **Against SSE4.2**, `citm_catalog` `parse_len` remains: +3.9 % cycles and +9.4 % instructions.
  With NVOL also compiled with `-msse4.2` it gives +4.3 %, so the flag is not the cause.
  `citm_catalog` is indented JSON, with about 84 000 short whitespace runs per document. SSE4.2
  resolves each one inline with one `pcmpistri` against a constant, about 7 instructions. NVOL
  makes a call and the AVX2 kernel builds its constants: about 30. NVOL's IR had no `pcmpistri`
  at that time (it does today: `skip_set16` and the `nvol_find_not_byteset_sse42` kernel). The
  `citm` writes (+0.9…+1.2 %, +1.3 % instructions) are strings shorter than 16 bytes, one call
  each. They are at the edge of the noise. See "Against SSE4.2" below.

```sh
# in WSL, after setup.sh (needs perf: apk add perf)
N=100 R=5 sh harness/rapidjson-e2e/counters.sh "sse2 sse42 nvol" "citm_catalog citm_min twitter twitter_min text_lines"
```

## Against SSE4.2 (1.16c)

The `sse42` build is compiled with `-msse4.2` and only runs on CPUs with SSE4.2. The fair
comparison is NVOL compiled the same way: the `nvol42` build (`-DRAPIDJSON_NVOL -msse4.2`). With
SSE4.2 enabled, `nvol_compat.h` includes `nvol.hpp` in the parser's unit, and both whitespace
skips (with length and `'\0'`-terminated) use the new `nvol_find_not_byteset_sse42` kernel
inline. That kernel does one `pcmpistri` against the class's member list, with one load as setup,
the same as rapidjson's loop. The `'\0'`-terminated skip is bounded at the end of the page, like
the rest of the patch. The `nvol` build (baseline x86-64, with dispatch) does not change.

Steps and what was discarded:

| Variant | `citm_catalog` `parse_len` against `sse42` |
|---|---|
| `nvol42` without the inline kernel (call to the AVX2 kernel) | +4.3 % cycles, +9.4 % instructions |
| Hand-written prototype: inline `pcmpistri` loop | −1.1 % |
| Prototype: only the first 16 bytes inline, then the call | +7 %: the indentation runs exceed 16 bytes |
| Generated kernel inline, with the 2nd-byte check first | +1.4 %: that check costs ~10 instructions per run and no longer saves a call |
| Generated kernel inline, right after the 1st byte (the one kept) | −0.1…+1.3 % over three repetitions; +3.1 % instructions |
| An out-of-line copy of the kernel in the parser's unit | +2…+3 % |
| Writing: the SSE2 range copy kernel inline (strings < 64 bytes, or all) | +15 % instructions in `citm`: it builds its constants per call |

Result, `N=100 R=5`, cycles of `nvol42` against `sse42` (the machine was loaded during this run:
`sse42` itself varied up to 6 % between repetitions; the instructions are exact):

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −9.8 % | +4.1 % | −8.8 % | +2.8 % | +0.8 % |
| citm_min | −6.6 % | +0.3 % | −2.7 % | +4.6 % | +2.6 % |
| twitter | −16.9 % | −0.2 % | −23.7 % | −11.2 % | −11.6 % |
| twitter_min | −14.4 % | −1.2 % | −15.8 % | −9.6 % | −8.1 % |
| text_lines | +0.6 % | +1.7 % | −7.5 % | −22.0 % | −20.6 % |

Instructions, `nvol42` against `sse42`: `parse` −9…−21 %, `insitu` −5…−23 %; `parse_len`
+3.1 % for `citm_catalog` and +1.6 % for `twitter`; `citm` `write` +1.3…+1.4 %; `citm`
`pretty` +2.1 %; the rest equal or lower.

What remains, all in `citm`:

- **`parse_len`, +3.1 % instructions** (about 8 per whitespace run). They are the check that the
  classes are built, the check of the remaining length, and the kernel's tail, which rereads the
  last 16 bytes. In cycles, between −0.1 % and +4.1 % depending on the run.
- **`write`, +1.3 % instructions**, the same with or without the inline kernel. rapidjson calls
  `ScanWriteUnescapedString` inside its per-character loop. The call (opaque, and it writes to
  `dst`) makes GCC save six registers per string instead of three and build the result with
  `setb`/`test`. Against `sse2`, the `nvol` build writes equally fast or faster.
- **`pretty`, +2.1 % instructions** with the inline kernel (−1.8 % without it). The inline copies
  of the kernel, one per site, use up GCC's inlining budget, and `Stack::Push<char>` stays out of
  line in `PrettyPrefix`.

```sh
N=100 R=5 sh harness/rapidjson-e2e/counters.sh "sse2 sse42 nvol nvol42" "citm_catalog citm_min twitter twitter_min text_lines"
```

## Against SSE4.2, second round (2026-11-19)

The analysis used instructions (exact: callgrind and `perf stat` agree), because at first the
machine was loaded (Windows at 100 %, cycles ±2-4 %). The final table was taken with the machine
idle.

**Whitespace skip with known length: closed.** Callgrind and the assembly showed ~7 more
instructions per whitespace run than rapidjson's loop:

- the length checked twice: `end - p >= 16` in the patch and `cur <= end - 16` in the kernel,
  written differently, so GCC did not merge them (4 instructions);
- the `sets.ready` check (2);
- the member list loaded from the class built at startup, plus a register copy (1);
- and, inside the loop, the 64-bit extension of the `pcmpistri` index, which the kernel did
  before comparing.

Fixes: the generator exports `NVOL_BYTESET_SET16` (and `SET16_OK`), the offset of the member
list. That list is the only part of the class that `nvol_find_not_byteset_sse42` reads. The patch
uses a constant list (`ws_list`, initialized at compile time and checked at startup against
`nvol_byteset_init`). So it needs no `ready`, and the compiler sees a constant. The guard is
written `p <= end - 16`, the same as the kernel. The kernel compares the index as `u32` and
extends it only on return. The skip becomes **instruction for instruction identical** to
rapidjson's (GCC even shares the constant). `parse_len`: from +3.1 % to +0.3 % instructions on
`citm_catalog`, from +1.6 % to +0.3 % on `twitter`. `citm` `pretty` dropped from +2.1 % to
+0.9 % (less inline code).

**Writing: closed.** With the machine idle (below), `citm` `write` gave +3-4 % cycles in every
run: real. It was not in NVOL but in rapidjson's per-character loop (`WriteString`), which every
string shorter than 16 bytes goes through: 25 instructions per character against 23 in the
original, one extra `setb` + `test`. The patch's long path started with `is.Tell() < length`, the
same expression the short path returns. GCC merged the two, hoisted the load of `os_` and
materialized the bool. Written as a pointer comparison (`p < end`, the same condition), and with
the final result as `stop != end` (`stop < end` is equivalent to `Tell() < length`), the loop
returns to the original's direct `jae`. The SSE2 range kernel inline with a constant class was
also tried. It improves `write` but shifts the cost to `pretty` (inline copies that use up GCC's
inlining budget) and loses on long strings (16 bytes per iteration instead of 32). It was
discarded.

### Measurement with the machine idle

Steam, Discord and Edge closed (background load 8-17 %). Three rounds, `N=100 R=7`, alternating
the builds on each row. The cycle spread of the reference build between rounds was 0.1-1.7 %.
Instructions (exact) and the cycle range of the three rounds; negative = NVOL better.

`nvol42` against `sse42`:

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −19.3 % / −12.5…−13.6 % | +0.3 % / +0.5…+1.1 % | −18.6 % / −9.6…−10.4 % | +0.1 % / +0.1…+0.2 % | +0.1 % / −0.3…+0.6 % |
| citm_min | −9.2 % / −4.8…−4.9 % | +0.3 % / +0.4…+0.7 % | −5.1 % / −2.8…−3.2 % | +0.1 % / +0.1…+1.4 % | +0.1 % / −0.2…+0.2 % |
| twitter | −22.2 % / −17.2…−17.8 % | +0.5 % / −1.4…+0.4 % | −17.8 % / −20.1…−20.6 % | −18.7 % / −10.9…−11.6 % | −15.8 % / −10.0…−10.4 % |
| twitter_min | −18.2 % / −12.0…−12.4 % | +0.4 % / −0.3…+1.0 % | −10.7 % / −15.7…−16.1 % | −18.9 % / −10.9…−12.0 % | −15.8 % / −8.9…−11.2 % |
| text_lines | −21.6 % / +1.0…+1.6 % | 0.0 % / +0.1…+0.3 % | −23.9 % / −8.8…−9.1 % | −46.0 % / −20.8…−21.9 % | −41.5 % / −20.3…−22.8 % |

`nvol` against `sse2`:

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −16.0 % / −11.9…−13.4 % | −0.3 % / −0.1…−1.4 % | −13.5 % / −9.2…−10.6 % | −0.2 % / −1.5…−2.2 % | −4.4 % / −0.5…−1.4 % |
| citm_min | −8.8 % / −6.2…−6.9 % | −0.4 % / −0.8…−1.0 % | −2.6 % / −2.6…−3.5 % | −0.1 % / −0.8…−1.4 % | −4.5 % / −0.3…−1.1 % |
| twitter | −18.3 % / −18.0…−18.9 % | +0.1 % / −1.1…−1.6 % | −12.3 % / −23.7…−24.0 % | −19.3 % / −13.0…−13.6 % | −20.2 % / −11.4…−12.5 % |
| twitter_min | −17.7 % / −11.9…−13.6 % | −0.6 % / −1.3…+0.2 % | −10.0 % / −15.5…−16.1 % | −19.2 % / −13.3…−14.3 % | −20.3 % / −11.7…−12.4 % |
| text_lines | −17.1 % / +0.3…+0.6 % | +0.6 % / +1.1…+1.2 % | −17.3 % / −5.8…−7.8 % | −46.8 % / −21.3…−21.8 % | −43.1 % / −22.0…−22.3 % |

### Third round: two-byte runs and minified documents

What remained with the machine idle was addressed as follows (three rounds each time,
`N=100 R=7`):

- **Two-byte runs.** In `text_lines` each array element is preceded by `"\n "`. The patch
  resolved only one-byte runs inline and called the kernel for these (one call per element):
  `text_lines` `parse_len` +1.1 % cycles against SSE2. Now it also checks the third byte before
  calling. Against SSE2 everything is equal or better: `text_lines` `parse` −1.6…−2.6 %,
  `parse_len` −0.2…−0.5 %, `citm` `parse_len` −0.5…+0.3 %, `twitter` `parse_len` −0.4…−1.1 %.
  Against SSE4.2, `text_lines` `parse` went from +1.0…+1.6 % to −0.9…+0.3 %.
- **Tried and discarded: the four-comparison SSE2 kernel inline in the base build**, with a
  constant class like the `pcmpistri` one. It fixes `text_lines`, but it brings `nvol.hpp` and
  the kernel into the parser's unit and again uses up GCC's inlining budget: `parse_len`
  +2.6-3.7 % instructions, even on `citm_min`, which has no whitespace.

One difference against SSE4.2 remained: **`parse_len` on minified documents**, `citm_min` +0.5 %
instructions and +0.45…+1.05 % cycles, even though the whitespace skip does not run there. Two
experiments located it (exact instructions per operation, callgrind):

| `parse_len` | citm_min | citm | twitter |
|---|---|---|---|
| `nvol42` | +0.56 % | +0.55 % | +0.65 % |
| rapidjson's SSE4.2 loop inside the patch | +0.00 % | +0.00 % | +0.00 % |
| NVOL's kernel with the scalar tail, no call | +0.06 % | +0.22 % | +0.20 % |

The rest of the patch costs nothing. The cost was the call to `find_checked` (and to
`find_terminated`) left in the skip for the case of fewer than 16 bytes before the end. It almost
never runs, but its mere presence makes GCC keep the parser state in callee-saved registers in
every function that inlines the skip (`ParseValue`, `ParseArray`…). In the SSE4.2 build the skip
no longer calls. With known length it runs the kernel and then the byte-by-byte tail, like
`RAPIDJSON_SSE42`. When `'\0'`-terminated it runs the kernel page by page and the end of each page
byte by byte.

Result, three rounds with the machine idle, `nvol42` against `sse42` (instructions / cycle
range):

| corpus | parse | parse_len | insitu | write | pretty |
|---|---|---|---|---|---|
| citm_catalog | −20.0 % / −13.1…−13.5 % | +0.2 % / −0.1…−0.5 % | −19.2 % / −9.5…−10.0 % | 0.0 % / −0.6…+0.8 % | +0.1 % / +0.6…+0.8 % |
| citm_min | −9.2 % / −4.4…−5.7 % | +0.1 % / −0.2…−0.5 % | −5.1 % / −2.3…−3.9 % | +0.1 % / −0.6…+1.1 % | +0.1 % / −2.6…+1.3 % |
| twitter | −22.4 % / −17.0…−17.3 % | +0.3 % / −0.1…−1.2 % | −18.3 % / −19.1…−21.0 % | −18.8 % / −12.6…−13.0 % | −15.8 % / −9.4…−10.8 % |
| twitter_min | −18.2 % / −12.2…−12.3 % | +0.1 % / −0.2…0.0 % | −10.8 % / −15.5…−16.1 % | −18.7 % / −12.2…−13.1 % | −15.8 % / −11.5…−11.9 % |
| text_lines | −21.8 % / −0.2…+0.4 % | 0.0 % / −0.1…−0.7 % | −24.1 % / −9.9…−10.4 % | −46.0 % / −21.2…−22.3 % | −41.5 % / −21.5…−22.4 % |

Every row is at par or better, with one minor exception: `citm_catalog` `pretty`, +0.6…+0.8 %
cycles in all three rounds with **the same instructions** (+0.06 %) and fewer branch
mispredictions. This is code placement, not different code. WSL only exposes instructions, cycles
and branch mispredictions, so separating it is left for native Linux with more counters.

## Audit (historical)

This section predates "Counters", "Known length" and "Against SSE4.2". It describes what was
tried at that time. Both gaps it mentions were closed later (writing with
`nvol_copy_until_byteset` and the `StoreVec` primitive; whitespace with its own TU, the `pure`
searches and the `pcmpistri` kernel).

The audit tested what it takes to close the two gaps.

**Whitespace with known length.** Two things were added to the generator: the negated search
(`nvol_find_not_byteset`, "first byte outside the set", without going through the complement) and
membership with a single `pshufb` + `cmpeq` for sets whose members have distinct low nibbles, like
the four JSON whitespace characters. Minimum of 11 rounds, `parse_len` on `citm_catalog`:

| Variant | µs |
|---|---|
| Original SSE4.2 | 2590 |
| NVOL, nibble lookup on the complement (32 B) | 2779 |
| NVOL, negated with one `pshufb` (32 B) | 2786 |
| NVOL, negated with one `pshufb` (16 B, SSSE3) | 2791 |
| NVOL, everything compiled with `-mavx2` (inlined) | 2804 |

The four NVOL variants give the same result. `twitter_min`, which has no whitespace outside
strings (NVOL is never called there), also gives +4 %. The difference is not in the kernel: it
points to how the parser code was laid out in the binary. Confirming it requires hardware counters
(PENDING 1.16). The patch keeps the complement. The two new capabilities stay in NVOL, verified,
for other uses (simdjson classifies whitespace with the same technique).

**Writing.** The original version copies 16 bytes while it searches. Matching it requires a
"copy until the set" kernel (`copy_and_find` in simdjson), and NVOL's IR did not yet have vector
stores. It is a new primitive in the four backends and in the interpreter (PENDING 1.16).

**Audit fixes.** The patch could hang. If something parsed from another static initializer
before the classes were built, the search for `'\0'` with an empty class never ended. This is
fixed as described above, at no cost on the normal path.

## simdjson

[SIMDJSON_SIMD_INVENTORY.md](../SIMDJSON_SIMD_INVENTORY.md): no simdjson routine can be replaced
with what NVOL generates today. simdjson turns 64-byte blocks into masks; NVOL finds the first
match. This needs a new kernel family (PENDING 1.17).
