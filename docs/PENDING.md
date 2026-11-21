# Pending features

Everything found while building NVOL that is still unresolved.
Each item states **what is missing**, **why it matters** and **how it was found**.
Priority in the tables: **P0** blocks parity with ripgrep, **P1** quality/portability,
**P2** improvements. The work order is the one in section 0.

Last review: 2026-11-21. The list was regenerated when item 15 (asm optimization) closed,
and the simdjson phase was added. Item 16 (aarch64 assembly) closed the same day. Items 1-15 keep their numbers; the pending items
continue from 16.

## 0. Work list

Rule: the code NVOL generates must be equal to or better than the reference implementation,
never worse, except for a limit of the target language.

**Two references.**

- **External:** what NVOL replaces (memchr, aho-corasick, ugrep's SIMD, rapidjson,
  simdjson). Each backend must end up equal to or better than it.
- **Internal:** NVOL's own assembler (NASM, MASM and GNU as), once it is mature (item 18).
  - Until then it is a backend under review. Its bar is the best that GCC, Clang and MSVC
    generate with `-O2` from NVOL's C.
  - Once mature, each language (C, C++, Rust, Odin, and Python through the library it
    loads) is compared against it, kernel by kernel. None will match exactly. Each will
    land above it (an optimization of the compiler's own) or below it (a loss).
  - What matters is that each difference has its cause explained, seen in the asm or in the
    counters, and recorded in the parity log (item 20). The difference does not need to be zero.

**What it is for.** The assembler does not depend on any compiler. It therefore separates
generation problems from problems in each language's toolchain: the compiler, its
dependencies or the build. When a low-level problem affects a client, NVOL can fix it and
patch it on its own, without waiting for the maintainers of each language. What is found this
way goes to the toolchain log (item 21), with a reproducer and NVOL's fix or workaround.

Tools: MSVC 14.51 (`cl`, `ml64`), NASM 2.16 and GNU as 2.42 (MinGW) are installed. WSL
(Alpine) has `perf`, `valgrind` and QEMU. A minimal WSL Arch Linux (glibc, rust, gcc, clang,
nasm, perf 7.3.5) is used for ripgrep on Linux and for the frontend counters: its `perf`
knows `idq.*` and `dsb2mite_*`, while Alpine's does not. Anything missing (Intel SDE, z3, ARM libc++, …) is
downloaded. WSL does not expose frontend counters, and Windows gives no counters to a user
program. Sourcetrail indexes C, C++, Rust and Odin; the assembler is worked on without an
indexer. NEON is measured under QEMU and documented as emulated: it serves to compare variants
with each other, not as hardware speed.

**How the work is done.** One item at a time, in this order. When an item starts, it is marked
*(in progress)*. When it is finished:

1. `bash harness/verify.sh` is green.
2. It is struck through here, added to "Done", and anything new that came up is recorded.
3. Commit and push to `main`, and a clean `git status`.

What is discovered midway is recorded as a new item in its phase. It is fixed on the spot only
if it blocks the current item. Performance measurements use counters in WSL on an idle
machine. A borderline row is repeated three times and the noise is recorded.

### Done: phases 0-3 up to item 16

The detail is in "Done".

- **Phase 0, verification** (1-6): `verify.sh` in the repo, `generated/` in sync, SSE4.2
  actually executed, C++ and aarch64 compiled, harness without fixed sizes, cleanup.
- **Phase 1, documentation** (7-8): docs up to date and the report for the lead.
- **Phase 2** (9): rapidjson on par against SSE4.2.
- **Phase 3, the assembler** (10-15): x86-64 code generation with Win64 and System V (10);
  MASM (11); GNU as (12); the three syntaxes give the same machine code (13); review with a
  fuzzer, unwind and edge cases (14); optimization against GCC, Clang and MSVC (15); aarch64
  (NEON, AAPCS64) in GNU as, tested under QEMU (16).

  State when item 15 closed, over 120 rows (12 operations × 5 sizes × 2 paths), against the
  best of GCC and Clang:

  | size | instructions | cycles |
  |---|---|---|
  | 16 B | +5.1 % | +25.6 % |
  | 64 B | +1.9 % | +5.2 % |
  | 256 B | +0.9 % | +1.0 % |
  | 4 KiB | +0.2 % | +0.9 % |
  | 64 KiB | +0.0 % | +1.7 % |

  In instructions, 51 rows are worse (by more than 1 %), 47 on par and 22 below. When item 15
  started, all 120 were worse. Against MSVC, by wall clock, the asm is on par or better in most
  rows. The remaining gaps are items 17 and 25.

### Phase 3. The assembler up to maturity (closing)

16. ~~**aarch64 (NEON) in GNU as**, AAPCS64, tested under QEMU~~ (see Done).
17. **What is left from items 15 and 16**, by weight:
    - **Small sizes (16 B: +5 % instructions, +26 % cycles).** The entry cost of the dispatch
      functions: `push` of callee-saved registers before the early exits (shrink-wrapping),
      and parameters saved on the stack in memmem (+16 %).
      - ~~memmem reloads its first argument from the slot it just stored~~ (`mov [rsp], rdi`
        … `mov r13, [rsp]` in the next block): `reloads` now carries what a block knows into
        a label that only forward jumps reach (see Done). The store stays: the vector path
        reads the slot after its calls.
      - Shrink-wrapping does not close memmem's 16 B row. GCC moves its three early exits
        (needle longer than the haystack, empty, 1 byte) before the pushes, but the bench
        (an 8-byte needle over 16 B) gets past all three, and GCC pushes 6 registers there too.
        At 16 B the call takes the Rabin-Karp path (233 instructions, against 128 at 64 B):
        that is where the gap with GCC (~200) is.
    - **Teddy:** +6/+10 % instructions from 64 B to 64 KiB. At 16 B it gives +55 %: it is the
      slow path `nvol_teddy_slow` (see 1.2a, item 27).
    - **NASM local labels in the ELF symbol table:** `.L36` after `L.nvol_x:` ends up as the
      local symbol `L.nvol_x.L36`. `perf` and `gdb` attribute samples and backtraces to those
      labels and not to the function; GNU as discards its `.L` labels.
    - **2-byte VEX:** commutative operations with the ymm8-15 operand in ModRM.rm need a
      3-byte VEX; swapping the operands makes them fit in 2. `encode.py` knows the lengths,
      so the lowering or the allocator can choose the order.
    - **Win64:** xmm6 is saved in memchr ×4, because the loop uses 7 vector registers.
      Compare with GCC and MSVC.
    - **Justified, not changed:** `count_parity` is the faithful port of memchr's `count_raw`.
      GCC and Clang autovectorize its scalar head and tail, and the asm does not autovectorize.
    - **aarch64 (item 16): +12.1 % static instructions against Clang -O2** over the 31 public
      functions ([report](benchmarks/2026-11-21-a64-static.md); only static, cycles wait for
      item 31). Seen in `nvol_memchr` (16 against 12 instructions):
      - the save as `sub sp` + `stp` instead of a pre-indexed `stp x29, x30, [sp, #-32]!`;
      - a branch where Clang uses `cmp` + `csinv`;
      - ~~a copy not coalesced while its source is live (`mov x19, x1` … `mov x1, x19`)~~:
        `regalloc.copy_back` removes the copy back, on both targets (see Done);
      - `and w0, w0, #255` that the callee repeats.

      The byteset family (+25…+53 %), memmem (+34 %) and Teddy (+36 %) need their loops
      compared by hand.
18. **Maturity:** criterion and declaration. The three syntaxes identical (13), the review
    closed (14), no row worse in instructions than the best compiler except justified
    deviations (17), the cycle gaps with a measured cause (25) and aarch64 tested (16). From
    then on the asm is the internal reference, and README and DESIGN say so.

### Phase 4. The languages against the internal reference

19. **Parity tool:** per kernel and per backend (C with GCC, Clang and MSVC; C++; Rust;
    Odin), against the asm. It compares the instructions of the disassembled kernel, and the
    instructions and cycles per call with counters in WSL, with the same sizes as the
    benches. The output is a regenerable table: `harness/parity`, with a report in
    `docs/benchmarks`.
20. **Parity log** (`docs/PARITY.md`): each row that does not match carries its cause, seen in
    the asm or in the counters, and whether it is accepted, fixed in the generator or reported.
    A row without an explained cause is treated as an open pending item.
21. **Toolchain log** (`docs/TOOLCHAINS.md`): each problem in a compiler, a dependency or a
    build that NVOL found, with the minimal reproducer, the version, NVOL's workaround and
    whether it was reported upstream. It starts with what has already been seen:
    - LLVM 20: the `or` of masks folded to `i1`, and LSR with two induction variables (Odin);
    - Odin: GEP without `inbounds`, `icmp sle` on pointers, no `@(align)` (2.1b);
    - GCC: the inlining budget and the callee-saved registers forced by an opaque call
      (rapidjson);
    - fortify: `memcpy` in musl and glibc;
    - MSVC: C4141;
    - NASM 2.16.01: a `near` jump without `strict` can come out short with `-Ox`, the
      default. Workaround: `strict near` (item 15);
    - NASM 2.16.01: it leaves long (`e9`, `0f 8x`) jumps whose short form fits when two
      jumps depend on each other. This shows at the entry of a rotated loop (`jmp` to the test
      and `jb` backwards), with displacements from 0x7c to 0x7f. There were 10 in the library,
      the same with `-Ox` and `-O9999`; MASM and GNU as shrink them. NASM does shrink a lone
      jump with the same displacement: a minimal reproducer of the pair is missing.
      Workaround: `encode.relax` decides the size and the printer writes `short`/`near`
      (item 13);
    - NASM: `align` in a data section pads with `0x90` (`nop`), not with zeros; `align N,
      db 0` is used (item 13);
    - NASM and ml64 do not choose the store form of a `vmovdqa`/`vmovdqu` from ymm8-15 to
      ymm0-7, which is the only one with a 2-byte VEX (4 bytes instead of 5); GNU as does. The
      printers write those bytes (`db`), because there is no syntax to request it (item 13);
    - GNU as 2.42 (COFF): it does not mark `.drectve` as `IMAGE_SCN_LNK_INFO` (it writes
      `0xc0300040`, writable data; the `i` attribute gives "unsupported section attribute"),
      so `link.exe` ignores its `-export:` entries and builds a DLL without exports. The same
      happens with gcc's `dllexport`; `ld` does read them. Workaround in
      `harness/asm/build_msvc.py`: it reads the section and passes the exports to `link.exe`
      in a `.def`. Confirmed by patching the flag to `0x100a00`, which is what NASM and ml64
      write: with that, `link.exe` exports them (item 12).
22. **C and C++** against the asm, with GCC, Clang and MSVC, including the full suite with
    Clang (2.5) and with MSVC (2.4).
23. **Rust** against the asm (`#[target_feature]`, `#[inline(always)]`, LTO).
24. **Odin** against the asm (LLVM without `inbounds`: 2.1b).

### Phase 5. Unmeasured or with doubtful measurement (may hide something worse)

25. **Cycles with the same instructions, on native Linux with frontend counters**
    (`idq.dsb_uops`, `idq.mite_uops`, `uops_issued`). On an idle machine these remain:
    - memrchr AVX2 at 4 KiB, +23 %: the loop takes 3.7 cycles per iteration against 3.3 for
      Clang;
    - byteset AVX2, +18 % (with 32 alignment it drops to +4 %, but that makes other rows
      worse);
    - pair AVX2, +20 %, with 13 % fewer instructions;
    - memmem SSE2, +22 %;
    - 14b: rapidjson, `pretty` of `citm_catalog` against SSE4.2, +0.6…+0.8 %.

    - **Position of a call site** (found with `copy_back`, item 17). `nvol_memchr` at 16 B
      (AVX2) costs 16.8-17.0 cycles when its 6-byte `call *ptr` starts at 0x69d-0x6a1, and
      18.5-19.6 cycles at 0x69c or at 0x6a2-0x6a8, with identical instructions. The scan
      (best of 11, single-byte NOPs after `push rbx`):

      | call at | 69c | 69d | 69e | 69f | 6a0 | 6a1 | 6a2 | 6a3-6a5 | 6a6-6a8 |
      |---|---|---|---|---|---|---|---|---|---|
      | cycles | 18.5 | 17.0 | 16.8 | 16.9 | 17.0 | 17.0 | 19.0 | 18.9-19.1 | 19.5-19.6 |

      `copy_back` drops one `mov` (3 bytes), which moves the call from 0x69f to 0x69c:
      memchr at 16 B AVX2 goes from 16.8 to 18.5 cycles (3 rounds), and memrchr at 64 B from
      19.9 to 21.6. A 3-byte NOP after `push rbx` puts the same code back at 0x69f, and it
      gives 16.8 again. The cost is therefore the position, not the code. It was accepted,
      with one instruction fewer per call. The mechanism is the µop cache (DSB): measured
      with frontend counters below.

    - **Accepted position losses.** The maintainer's rule (2026-11-21): an optimization
      whose worse row a control shows to be position only is accepted, and logged here. The
      control puts the new code at the old addresses (NOP padding) and shows that the code
      alone is not worse. These rows are layout noise that any later edit moves again, so
      re-measure them when this item gets frontend counters, or when a row stays bad across
      unrelated commits:

      | commit | row | before → after | control (new code, old addresses) |
      |---|---|---|---|
      | copy_back (item 17) | memchr 16 B AVX2 | 16.8 → 18.5 | 16.8 |
      | copy_back (item 17) | memrchr 64 B AVX2 | 19.9 → 21.6 | 3-byte NOP restores it |
      | reloads across labels (item 17) | memmem 256 B SSE2 | 71.5 → 72.4 | 70.8 |

    - **Frontend counters (2026-11-21).** WSL's kernel does count `idq.dsb_uops`,
      `idq.mite_uops` and `dsb2mite_switches.penalty_cycles`; Alpine's `perf` just has no
      event table for them. Arch's does. A first scan over the position of memchr's call (13
      single-byte NOP pads, 4 events, so no multiplexing, best of 9) in the glibc binary,
      where the call lands at 0x3ddc-0x3de8:

      | pad | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
      |---|---|---|---|---|---|---|---|---|---|---|---|---|---|
      | cycles | 18.9 | 18.8 | 18.8 | 18.4 | 18.9 | 18.8 | 18.9 | 19.1 | 18.8 | 18.6 | 18.7 | 17.9 | 19.1 |
      | MITE µops | 9.0 | 0.3 | 0 | 1.2 | 0 | 2.3 | 1.5 | 1.0 | 0 | 0 | 0 | 0 | 0.7 |

      In this binary every pad is slow; the fastest, pad 11, gives 17.9. The legacy-decode
      µops (0-9 per call) do not follow the cycles here: pad 0 and pad 4 both give 18.9,
      with 9 and 0.

      The same scan in the musl binary (Alpine). Its `perf` takes the raw codes: `r0879`
      for `idq.dsb_uops`, `r0479` for `idq.mite_uops`, `r02ab` for the DSB-to-MITE switch
      penalty. The call lands at 0x3bcc-0x3bd8:

      | pad | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
      |---|---|---|---|---|---|---|---|---|---|---|---|---|---|
      | cycles | 18.3 | 16.8 | 16.9 | 16.9 | 16.9 | 17.1 | 18.9 | 19.0 | 19.1 | 19.3 | 19.6 | 19.1 | 19.3 |
      | MITE µops | 0.8 | 0 | 0.3 | 0.2 | 0 | 0.4 | 10.6 | 11.6 | 11.2 | 12.1 | 12.7 | 13.4 | 19.9 |
      | switch penalty | 2.95 | 0 | 1.25 | 0.5 | 0.1 | 0.3 | 2.4 | 2.3 | 2.2 | 2.2 | 2.8 | 2.5 | 1.1 |

      **Here the µop cache explains it.** The fast pads (1-5) decode everything from the DSB.
      From pad 6 up, 10-20 µops per call come from the legacy decoder (MITE) and cost
      2-2.5 cycles. Pad 0 has almost no MITE µops but pays about 3 cycles of switch penalty.
      This is copy_back's case: the call at 0x69c in the object, the slow 18.5.

      In the glibc binary every pad is slow and MITE does not explain it, so something else
      limits that layout: another 32-byte window (the callee's, or the bench loop's), to be
      found with the same events. The fix for the generator would be to place the hot
      dispatch code so that no 32-byte window overflows the DSB (on Skylake, at most 3 ways
      of 6 µops per window). That needs the µop counts per window that
      `encode.py` can give.

    The JCC erratum, alignment (16, 32 or 64) and the order of the loads are already ruled
    out. The asm serves as the control: it is the same kernel without a compiler.
26. ~~1.4: "one memchr per line"~~: measured with counters (see Done).
27. 1.2a: Teddy over short haystacks. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)):
    - **at 16 B, NVOL is 2× to 40× slower than aho-corasick** in every case whose patterns
      have more than one byte (case 6, 64 patterns × 6 B: 3348 cycles against 84). Below
      Teddy's minimum length, `nvol_teddy_slow` tries every pattern at every position, while
      aho-corasick runs its 64-bucket Rabin-Karp. Port the Rabin-Karp (a feature);
    - from 64 B up, NVOL is ahead or on par: +6.9…+27.2 % at 64 B (the −6…+27 % of
      `tedbench` was wall-clock noise), −5.0…+11.6 % from 256 B to 64 KiB. In instructions,
      NVOL is below aho in every row from 64 B up, so the Teddy gap in item 17 is the asm's
      and not the Rust's.
28. 1.6: memchr with 1-15 bytes. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)): NVOL has 4 fewer instructions
    per call (both scan byte by byte, 5 instructions per byte), but from 3 B up it takes 1-2.5
    more cycles: +4…+19 %, the same in 3 rounds. Find the cause: a dependency chain in the
    byte loop, or the entry. Then port memchr's SWAR for the path without SIMD.
29. 1.5: `first_non_ascii` against `bstr`. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)): −20 % cycles at
    256 B, −43 % from 4 KiB, on par at 64 B, but **+2 cycles at 16 B** (11.0 against 9.0, 3
    rounds), to be closed.
    - The semantics of "none": bstr's public `find_non_ascii_byte` returns `None` when every
      byte is ASCII, the same as NVOL. Only its internal `ascii::first_non_ascii_byte` returns
      `len`. The wrappers therefore already match the public API; the maintainer decides
      whether anything should change.
30. ~~1.10: ripgrep end to end on Linux~~ (see Done). What remains from it: the whole-file
    Teddy search (`Moriarty|Lestrade|Mycroft|Hudson`) is +0.54…+0.65 % in cycles in 3 rounds,
    with 2.8 % fewer instructions ([report](benchmarks/2026-11-21-ripgrep-linux.md)).
31. ~~1.7a and 2.1c under QEMU~~: `bench`, `membench` and `tedbench` ran emulated ([report](benchmarks/2026-11-21-a64-qemu.md)).
    What remains:
    - `Finder::find 3B frequent "the"` is −5.0…−7.8 % at every size, a match at the start:
      the call and the prefilter setup;
    - Odin on aarch64 needs an `odin` that targets `linux_arm64` in WSL;
    - real hardware (1.7a, 2.12).
32. **AU21.** ugrep `-c zq` (2-byte memmem). Measured with counters
    (`harness/ugrep-e2e/counters.sh`, [report](benchmarks/2026-11-21-rust-counters.md)): the whole process gives +0.40, +0.80 and
    +0.68 % cycles in 3 rounds, with 0.2 % fewer instructions. The other queries are within
    ±0.65 %. The +2.8 % of the wall clock was mostly noise. What remains is small but
    systematic; look at it per function (`perf record`) on the `chars<2>` path.

### Phase 6. simdjson (1.17)

simdjson processes the input in 64-byte blocks, with guaranteed padding, and turns them into
64-bit masks ([inventory](SIMDJSON_SIMD_INVENTORY.md)). NVOL today searches for "the first
one": no simdjson routine can be replaced with what it generates. A new family is needed, from
block to mask. Each step ships in every backend (C, C++, Rust, Odin, Python and asm) with its
test against the interpreter.

33. **The IR for 64-bit blocks and masks:**
    - bitwise `xor` and `andnot` on integers (today there are only `and`, `or` and shifts);
    - `u64` masks on x86, built from the `movemask` of two AVX2 vectors or four SSE vectors
      (today the x86 mask is `u32`; NEON already uses `u64`);
    - several masks per call: output through a pointer (`Store64` over `MPTR`), or one
      function per mask if inlining merges them.
34. **New primitives,** with lowering in each language and in asm, and tested like the others
    in `test_prims`:
    - carry-less multiplication (`pclmulqdq` on x86, `pmull` on aarch64), with a scalar
      fallback;
    - `pmaddubsw`, `pmaddwd` and `packusdw` for the 8 digits;
    - the classification `pshufb` with its own table (it already exists for byteset's
      *unique-nibble* sets).
35. **Stage 1:** the kernels for a 64-byte block, which cover most of simdjson's SIMD work:
    - `classify`: the masks of whitespace and of `{}[]:,`;
    - the masks of `"` and `\`;
    - the escape scanner: which `"` are escaped, with bit arithmetic and additions;
    - `prefix_xor`: the inside of the strings.
36. **UTF-8 validation** (1.13): the nibble lookup algorithm (Keiser and Lemire). simdjson and
    ugrep's `isutf8` need it.
37. **Stage 2:**
    - `parse_eight_digits_unrolled`;
    - the block's `is_ascii`;
    - a variant of `copy_until` that gives the masks of `\` and `"` (`copy_and_find`).
38. **Integration and measurement:**
    - a patch to simdjson, like the one for rapidjson: its generic code calls NVOL's kernels
      instead of its own;
    - the e2e with its tests, `jsonexamples` and the rapidjson corpus;
    - the end-to-end comparison against its haswell and westmere implementations, with
      counters.

### Phase 7. What the external reference does and NVOL does not

Since item 10 each new kernel also ships in asm, and a new primitive needs its asm lowering
(x86-64 and, from item 16, aarch64) in addition to the lowering for each language.

39. 1.1e (a): `PrefilterState` saturated in `u32`, like memchr, so that it also matches beyond
    4 GiB.
40. 1.1b: reverse `memmem` (`FinderRev`, `rfind`; `bstr` exposes it).
41. 1.1d and 1.2b: `FinderBuilder` options (custom ranker, no prefilter) and Teddy
    `LeftmostLongest`.
42. 1.1c: Shift-Or as a standalone algorithm.
43. 1.7b: wasm32 simd128 (memchr has it).

### Phase 8. Better than the external reference (optional)

44. 1.9: `count` in C. Two accumulators in the kernel lower the asm and Clang, but make GCC
    worse: SSE2 from 264 to 307 cycles, so it was discarded. The asm reassociates the
    subtractions in the lowering. It remains to see whether the C can reassociate without
    losing with GCC. Also 1.11: `memmem_init` 2×.
45. 1.8: AVX-512 / AVX10. The i5-7400 does not have it: correctness is tested with Intel SDE;
    performance is not. It needs 64-bit masks, the same as phase 6 (33).

### Phase 9. Robustness and portability of the generator

46. 2.14 (IR type checker), 2.13 (tests of the passes), 2.16 (aliasing between writes and
    reads), 2.11 (interpreter near page boundaries), 2.3 (Rust lowering, of the scalar
    primitives and of those missing from `test_prims`).
47. 2.6 (atomics in the C dispatch, and the asm's ifunc pointers) and 2.7 (Rust `no_std`).
48. 2.2: iterator, Finder and Teddy glue inside the IR. With that, the glue also ships in asm
    and the C's `asm_kernels` mode shrinks to the CPU checks.
49. 2.10: zero-copy Python binding for read-only buffers.
50. 2.8: CI on GitHub Actions running `harness/verify.sh`, with NASM, and MSVC on Windows.
51. 2.9: formal verification of primitives (z3), and of their asm templates.

### Aside

**For later:** 1.2c, the Teddy variant that aho-corasick does not reach either (Fat Teddy with
a 1-byte mask): it stays documented and will be fixed later.

**Blocked by hardware or by third parties:** 2.12 (AMD, real ARM, native glibc, and native
Linux for the frontend counters of item 25), 2.1b (reporting to Odin: the repo owner's
decision), 3.2 (a tool; it does not affect generated code).

**Closed, no action:** 1.7d (documented differences in which NVOL is equal to or more exact
than the reference); 1.1e (b) and (c) (documented limits: needles of up to 4 GiB, state read
as little-endian). The detail of both is in Done.

## 1. Parity with ripgrep's SIMD

| # | P | Pending | Detail |
|---|---|---|---|
| 1.1b | P2 | **Reverse `memmem` (`FinderRev`, `rfind`)** | It is not on ripgrep's path (`regex-automata` only uses `Finder::find`), but `bstr` exposes it (`rfind`, `FinderReverse`). Reverse Two-Way + reverse Rabin-Karp; memchr does not use SIMD in reverse. |
| 1.1c | P2 | **Shift-Or (`arch/all/shiftor.rs`)** | The selector in memchr 2.8.3's `memmem/searcher.rs` does not use it, so it is not needed for parity. Only if it is to be offered as a standalone algorithm. |
| 1.1d | P2 | **`FinderBuilder` with a custom *ranker* and `PrefilterConfig::None`** | memchr allows changing the rarity table and disabling the Two-Way prefilter. NVOL always uses the default table and the prefilter enabled, which is what `regex-automata` does (`Finder::new`). |
| 1.1e | P2 | **Documented edge differences in `memmem`** | (a) `PrefilterState` uses `usize` instead of a saturated `u32`: it only differs with more than 4 GiB skipped in one call (item 39). (b) and (c) are closed with no action (see Done). |
| 1.2a | P2 | **Teddy `find_iter` over ~64 B haystacks** | With few bytes, the remainders of < 16 + m - 1 bytes fall to the slow path (`nvol_teddy_slow`, direct search) instead of aho-corasick's multi-pattern Rabin-Karp. It gives the same result; in `tedbench` it swung between −6 % and +27 % between runs (the +27.1 % is in `2026-11-18-teddy-i5-7400-windows.md`; the −6 % run was not saved in any report). Measure the slow path separately and, if needed, port the 64-bucket RK. |
| 1.2b | P2 | **Teddy `MatchKind::LeftmostLongest`** | Only LeftmostFirst exists, which is what `regex-automata` uses. LeftmostLongest changes the order of the patterns (by length) at construction. |
| 1.2c | P2 | **Fat Teddy with a 1-byte mask: unreachable (for later)** | Fat requires > 32 patterns, and mask 1 with > 16 patterns is rejected, the same as in aho-corasick: `nvol_teddy_fat_avx2_1` is generated but never chosen. It is aho-corasick's problematic variant: the selection rule makes a variant exist with no path that reaches it. It stays to be fixed later (decide whether to remove it, or whether to allow mask 1 with Fat for 17-64 one-byte patterns, and measure it). |
| 1.5 | P1 | **`first_non_ascii` without a benchmark against `bstr`** | It is tested but not measured; add `bstr = "=1.13.0"` to the harness. Also, bstr returns `len` when there are no non-ASCII bytes and NVOL returns `NONE`: documented; decide whether each language's wrapper should imitate bstr. |
| 1.6 | P1 | **Byte-by-byte fallback below 16 bytes** | memchr uses SWAR (one `usize` at a time) in `arch/all/memchr.rs`. The 16 B benchmarks already put us on par or better, but 1-15 B still needs measuring, and SWAR needs porting for platforms without SIMD. |
| 1.7a | P1 | **NEON: performance unmeasured on real hardware** | Result parity is tested on aarch64 under QEMU (`harness/run-aarch64.sh`), but QEMU cannot measure speed. `bench`, `membench` and `tedbench` still need to run on an ARM64 machine (Apple M*, Graviton, Raspberry Pi 5, Windows on ARM). |
| 1.7b | P2 | **wasm32 simd128** | memchr and aho-corasick do not use it on native ripgrep's path; it does exist in memchr (`arch/wasm32`). Add it as an ISA if grepper is wanted in WASM. |
| 1.8 | P2 | **AVX-512 / AVX10** | memchr does not use it; it would be "better than ripgrep". It requires 64-bit masks (today `MASK` is u32). |
| 1.9 | P2 | **An even faster `count`** | Tested (item 15): two accumulators in the kernel lower the asm (140 → 131 cycles at 4 KiB AVX2) and Clang (136 → 123), but make GCC worse (SSE2: 264 → 307 cycles), so it was discarded. The asm reassociates the subtractions in pairs in the lowering, like GCC. It remains to see how to give the C the same without losing with GCC (item 44). |
| 1.13 | P1 | **ugrep: UTF-8 validation** | The only part of ugrep's SIMD that NVOL does not cover ([inventory](UGREP_SIMD_INVENTORY.md)): `isutf8` (ASCII path + sequence validation). `first_non_ascii` covers the ASCII path; a validation kernel is missing (e.g. Keiser-Lemire's, nibble tables as in `byteset`). AVX-512BW stays out until there is hardware (1.8). |
| 1.17 | P1 | **simdjson: block-to-mask kernels (phase 6, items 33-38)** | No simdjson routine has an equivalent in NVOL ([inventory](SIMDJSON_SIMD_INVENTORY.md)): all of them turn 64 B blocks (with guaranteed padding) into 64-bit masks. Missing: a per-block membership mask (one `pshufb` when possible, Muła otherwise), equality masks for several bytes, `prefix_xor` (`pclmulqdq`/`pmull` and scalar), UTF-8 validation (1.13) and 8 digits to integer. The first three cover stage 1. |
| 1.11 | P2 | **`memmem_init` can be 2× faster** | Inlined by LTO, building a 40 B Finder took 114 ns in one binary and 301 ns in another; now it is `noinline` and takes ~230 ns (memchr: ~240-255 ns). See what LLVM did in the 114 ns case (did it vectorize the rarity scan or the byteset?) and bring it into the IR. |

## 2. Generator (IR, interpreter, backends)

| # | P | Pending | Detail |
|---|---|---|---|
| 2.1b | P2 | **Odin compares pointers as signed and without `inbounds`** | Odin emits `icmp sle ptr` for `a <= b` with multi-pointers (and `jle` in the asm), and all its pointer arithmetic (`p[n:]`, `&p[n]`, `intrinsics.ptr_offset`) as `getelementptr` without `inbounds`, which is what confused LLVM 20's LSR (see Done, memchr_iter). It also does not allow aligning functions (`@(align)`) or passing LLVM options such as `-x86-branches-within-32B-boundaries`. It should be reported upstream. It has no effect on x86-64/aarch64 in user space (addresses < 2^63), but it must be kept in mind if the code runs in kernel space. |
| 2.1c | P2 | **Odin on aarch64: performance unmeasured on real hardware** | Result parity tested under QEMU (`harness/odin/run-aarch64.sh`). |
| 2.16 | P2 | **`Store64` without alias analysis** | The IR allows writing through `MPTR` and reading through `PTR`; nothing stops a kernel from reading what it just wrote through another pointer. Today only `nvol_memmem_init` writes, and into its own buffer. If this is generalized, document or check that the write and read regions do not overlap (the interpreter could mark them). |
| 2.14 | P1 | **Type comparison when lowering `bool` in C** | In C, `bool` is printed as `int`; comparisons such as `small == 0` work, but there is no type check in the IR that prevents mixing `bool` with integers. Add an IR *type checker* before printing. |
| 2.13 | P2 | **Dead `let` elimination is a pass without its own tests** | `ir.prune_unused_lets` was added when moving to the raw signature (there was an unused `len`). It is only tested indirectly; add unit tests for the IR passes. |
| 2.2 | P1 | **Glue outside the IR** | The iterator, Finder and Teddy glue is handwritten per language (`iters.py`, `finder.py`, `packed.py`, and the API part of `odin.py`), tested by the harnesses but not by the interpreter. With mutable structs in the IR it could be generated and verified once. |
| 2.3 | P1 | **Primitive lowering: Rust and the scalar ones are missing** | `tests/test_prims.py` already tests each vector primitive × {SSSE3, AVX2} in **C** against `PrimDef.ref` (300 random vectors each). The same is missing for the **Rust** backend and for the scalar primitives (`first_offset`, `popcnt`, `load16u/32u/64`). |
| 2.4 | P1 | **MSVC path half tested** | The C prelude has branches for MSVC (`_BitScanForward`, `__cpuidex`, `_xgetbv`). Tested: the C++ header compiles with `cl /std:c++20 /W4 /WX` in two linked units and runs (item 6; it used to give C4141, repeated `inline`); the C in `asm_kernels` mode compiles with `cl /W4 /WX`, links with the NASM object through `link.exe` and passes the smoke test (the `asm` step of `verify.sh`, item 10). The full C (the kernels with MSVC intrinsics) under the pytest suite is missing (item 21). |
| 2.5 | P1 | **C suite not run with Clang** | The generated C already compiles with Clang 21 (AArch64, and x86 linked from Odin), but `pytest` only runs with GCC 13.2. The compiler in `tests/` still needs to be parameterized and the suite run with Clang. |
| 2.6 | P1 | **CPU cache and ifunc pointers without atomics in C** | `nvol_cpu_avx2_cache` and the `*_ptr` variables are plain variables written from several threads (a benign race on x86, but UB under C11). Use `_Atomic`/`stdatomic.h` in C11 or `InterlockedExchangePointer` in MSVC; keep the C99 version as an option. Rust already uses `AtomicPtr`. |
| 2.7 | P1 | **Rust requires `std`** | `is_x86_feature_detected!` comes from `std`. For `no_std`, detection must use `core::arch::x86_64::__cpuid` (as memchr does) behind a cfg. |
| 2.8 | P1 | **No CI** | GitHub Actions is missing: `python -m nvol check`, `pytest`, the harness's `cargo test`, the C++ smoke test, and checking that `generated/` is up to date with the generator (empty diff). |
| 2.9 | P2 | **Formal verification of primitives** | Design plan: prove lowering ↔ `ref` equivalence with bitvectors/SMT (z3) for the pure primitives. |
| 2.10 | P2 | **Python binding copies read-only buffers** | A read-only `memoryview` that is not `bytes` is copied once (`_buf`). Avoidable with `ctypes.pythonapi.PyObject_GetBuffer`. |
| 2.11 | P2 | **The interpreter does not test alignments > 64 or high addresses** | It uses bases `0x10000 + [0,64)`. Add cases near page boundaries (4 KiB) to document that reads never cross outside `[start, end)`. |
| 2.12 | P2 | **More sizes/CPUs in benchmarks** | Only the i5-7400, on Windows and on Linux (WSL2 Alpine, musl; the ugrep and rapidjson e2e and the counters run there; WSL2 Arch, glibc, for ripgrep and the frontend counters). AMD (Zen) and real ARM are missing, and glibc's own `memchr` has not been compared yet. |

## 3. Tools

| # | P | Pending | Detail |
|---|---|---|---|
| 3.2 | P2 | **Index of the generated C** | `generated/c` can be indexed with a one-line `compile_commands.json`, useful for reviewing the emitted code. |

## 4. Assembly backends

| # | P | Pending | Detail |
|---|---|---|---|
| 4.1 | P1 | **NASM, MASM and GNU as** | Requested to validate the lowest level without a compiler in between. A common code generator (instruction selection from the IR, register allocation, System V and Win64 conventions, dispatch) and three printers: NASM (done, item 10), MASM (`ml64`) and GNU as in AT&T syntax (done, items 11-13); then aarch64 in GNU as under QEMU (done, item 16). The same differential tests as the C; performance equal to or better than the best compiler (items 11-18). |
| 4.2 | P1 | **The assembler as the internal reference** | Once mature (item 18), each language is compared against it, kernel by kernel, and every difference is explained in the parity log (items 19-24). It serves to find problems in the languages, their compilers and their toolchains, and to be able to fix and patch them without depending on their maintainers (toolchain log, item 21). |

Design of 4.1. Done: x86-64 in NASM (item 10), MASM (11) and GNU as (12), and aarch64 in GNU
as (16); see Done. What remains is maturity (items 17-18):

1. **Machine IR.** Each IR function is lowered to a linear list of x86-64 instructions over
   virtual registers (64-bit integers and vectors), with labels and conditional jumps on the
   flags. Expressions are walked as a tree. Selection folds the usual patterns:
   `[base + index + displacement]`, `lea` for pointer arithmetic, `test` instead of
   `cmp 0`, immediate constants. Each primitive has its sequence per ISA:
   - `splat`: `vpbroadcastb` in AVX2; `imul 0x01010101` + `movd` + `pshufd` in SSE; a
     constant comes from `.rodata`, as in GCC.
   - `first_offset` and `last_offset`: `bsf` and `bsr`, valid because the mask is never zero,
     and not `tzcnt`/`lzcnt`, which require BMI.
   - `popcnt`: the instruction in the SSE4.2 and AVX2 functions (every CPU with those ISAs has
     it); the SWAR sequence in the others.
2. **Register allocation.** Linear scan over live intervals, which extend over the whole loop
   when a value crosses the back edge. There are 15 integer registers and 16 vector
   registers. What lives across a call goes to a callee-saved register or to the stack.
3. **Conventions.** System V (Linux, WSL) and Win64. Win64 preserves `rdi`, `rsi` and
   `xmm6-15`, and each save goes into the prologue only if the register is used.
4. **Calls.** The ifunc pointers (the resolver calls the `nvol_cpu_*` functions of the C glue,
   which does `cpuid` + `xgetbv` and stores the cache), the function tables and the indirect
   calls of the *visitor*.
5. **Three printers** over the same list: NASM (Intel), MASM (`ml64`: `PROC`/`ENDP`,
   `xmmword ptr`) and GNU as (AT&T: reversed operand order, suffixes, `%`).
6. **Tests.** Each output is assembled and linked as a DLL (Win64) or `.so` (System V in WSL),
   and runs the same differential suite as the C through `ctypes`. Performance is measured
   against GCC's C with the same benches.
7. **aarch64** in GNU as, tested under QEMU (emulated: correctness, not speed).

## Done (for reference)

- **ripgrep end to end on Linux (item 30, former 1.10).** It runs in WSL Arch (glibc), with
  the builds on the Linux filesystem (`setup.sh` and `bench.py` take `W`), and
  `harness/ripgrep-e2e/counters.sh` adds `perf stat` and the share of the kernels per query
  ([report](benchmarks/2026-11-21-ripgrep-linux.md)).
  - Outputs: identical on all 14 queries.
  - Cycles: NVOL is equal or better on every query except the whole-file Teddy search
    (+0.6 %, item 30). `-n` is −21.7 %, the memmem queries −2.0…−3.3 % with 16-38 % fewer
    instructions.
  - On Linux, the SIMD kernels take 38-95 % of rg's cycles over a cached file. On Windows,
    per-process noise had hidden that share.

- **"One memchr per line" (item 26, former 1.4), measured with counters.** `linecount`
  (`harness/rust/pcounters.sh`) runs grep-searcher's LineStep over 1 MiB with lines of 8 to
  1000 bytes. NVOL is ahead at every length: −8.8…−11.1 % cycles from 8 to 40 B, −2.1…−4.8 %
  from 64 to 1000 B, and −3.2…−9.6 % instructions. The one positive row (1000 B, +4.6 %) gave
  −3.2 and −4.0 % in two more rounds ([report](benchmarks/2026-11-21-rust-counters.md)). With counters, pinning the CPU is not needed.

- **Reloads across labels in the asm (item 17).** `regalloc.reloads` knew which register held
  a spill slot only inside one block. Now a label that only forward jumps (and the
  fallthrough) reach starts with what all its predecessors agree on. A label behind a
  backward jump, or named by anything other than a jump's target, still starts empty.
  - In memmem's AVX2 and SSE2 entries, `mov r13, [rsp]` becomes `mov r13, rdi`. The address
    of the next loads no longer waits on a store forwarded from the entry. Nothing else
    changes on x86, and nothing at all on A64.
  - Cycles (i5-7400, best of 15, 3 rounds):

    | row | before | after |
    |---|---|---|
    | memmem 256 B AVX2 | 52.3 | 49.0 |
    | memmem 16 B SSE2 | 66.2 | 60.7 |
    | memmem 64 B SSE2 | 38.6 | 36.9 |
    | memmem 16 and 64 B AVX2 | 58.2 and 34.3 | 58.3 and 34.0 |
    | memmem 256 B SSE2 | 71.5 | 72.4 |

    In the control (the new code at the old addresses), the 256 B SSE2 row gives 70.8 and
    the 16 B SSE2 row gives 66.0. The 16 B gain and the 256 B loss are therefore both
    position; the AVX2 256 B and SSE2 64 B gains are the code. Accepted under the
    position-loss rule (item 25).

- **The copy back in the asm (item 17).** After `mov a, b`, a later `mov b, a` in the same
  block, with neither register written in between, is removed (`regalloc.copy_back`, on x86
  and A64). The allocator leaves it when a value lives in a callee-saved register across a
  call and is also an argument of that call: `mov rbx, rsi; lea rdx, [rbx + rdx]; mov rsi,
  rbx; call`.
  - 19 copies are removed on x86 System V and 7 on A64.
  - Every memchr-family and `first_non_ascii` wrapper saves one instruction per call (16 B
    and 64 B, both paths: 49 → 48, 56 → 55, …). memmem is unchanged.
  - In cycles, two AVX2 rows get worse only because of where the call lands (item 25, with
    the table and the NOP control). The change was accepted with that cause measured.

- **aarch64 in NVOL's own assembly (item 16).** The IR is lowered to A64 and NEON, with the
  AAPCS64 calling convention, in GNU as syntax: `generated/gas/nvol_aarch64_aapcs64.s` and
  the C glue in `generated/gas/nvol.c`.

  **One lowering, two targets.**
  - `lower.py` keeps the shared machinery; `x86.py` and `a64.py` select the instructions.
  - `regalloc.py` takes the scratch registers, spill code, peephole and frame from the
    target.
  - The x86 output stayed byte-identical through the split.

  **The A64 rules.**
  - Every instruction is 4 bytes. A branch out of range (b.cond, cbz ±1 MiB; tbz ±32 KiB) is
    an error, not silently widened.
  - x29/x30 are saved only when the function calls. x19-x28 and d8-d15 are saved when used.
    A call clobbers every vector, because only the low 64 bits of v8-v15 survive it.
  - The NEON `movemask` is `shrn #4` and `0x8888…`, the same as in the C.
  - There is no resolver: each public function with a single target is one `b`.

  **Tests, in the `asm-a64` step (`harness/asm/run-a64.sh`).**
  - `samebytes aapcs64`: GNU as (binutils 2.45.1) and Clang 22 give the same bytes (15 047
    instructions), the same function tables and constants, the same unwind rules, and the
    sizes that `a64.code_size` predicts (0 mismatches).
  - The committed library is built and smoke-tested.
  - Under QEMU, the aarch64 Python of the sysroot runs:
    - `test_asm`: 150 tests, every function and NEON primitive against the interpreter,
      through an AAPCS64 trampoline that checks x19-x28 and d8-d15;
    - the native suites (differential, byteset, memmem, Teddy) over the C and over the
      assembly (the `gas-a64` backend): 56 tests.

  **Tested red.** Each mutation fails the step:
  - `cmeq` → `cmhi` (`test_scalar_function[nvol_memchr_raw]`);
  - x19 left unsaved ("a callee-saved register came back changed", in
    `nvol_memmem_find_raw`);
  - a `code_size` 4 bytes off (122 mismatches in `samebytes`).

  **Found along the way:**
  - Threading a jump rebuilt it with its label only, so `cbz x9, .L2` lost its register. x86
    never saw it: its conditional jumps take only a label.
  - The native suites had never run on aarch64:
    - the byteset visitor test decoded the mask as x86's bit i, not as aarch64's bit 4i+3,
      which is the contract in `nvol.h`;
    - the Teddy test asserted x86's rule (mask 1 with more than 16 patterns is rejected),
      while aho-corasick's NEON branch never rejects.

    These were bugs in the tests, not in the generated code.
  - In asm mode, the C prelude's memory helpers have no caller, and Clang with `-Werror`
    rejects an unused `static inline` of a `.c`. GCC does not, which is why x86 never showed
    it. They are now `NVOL_UNUSED`.
  - GNU as and Clang encode the same `.cfi_*` directives with different CIE factors, so
    `.eh_frame` is compared by its rules.

- **Measurements on an idle machine (item 15).** The full counters in WSL (best of 15)
  and the table against MSVC on Windows are in the
  [report](benchmarks/2026-11-20-asm-counters-wsl.md), third part.
  - memchr AVX2 at 4 KiB ended up on par in cycles: the earlier +15 % was noise.
  - Aligning loop heads (16, 32 or 64) does not win in every row.
  - The asm reassociates the subtractions on an accumulator in pairs, like GCC: count AVX2 at
    4 KiB drops from 140 to 135 cycles.

- **Intervals with holes in the asm allocator (item 15).** Each vreg occupies its register
  only in the segments where it is live. There is one segment per block, running from the
  start of the block (if it enters live) or the first access to the end (if it leaves live)
  or the last access. Two values whose segments interleave share a register. On spilling, the
  new value is compared with what conflicts with it in each register, and the cheapest is
  evicted, by static frequency weight.

  This is what the dispatchers with inlined kernels needed: the parameters crossed, in linear
  order, code that the hot path skips. No row gets worse.

  | | before | after |
  |---|---|---|
  | memmem 64 B AVX2 | +35 % | +19.6 % |
  | memmem 64 B SSE2 | +19 % | +10.7 % |
  | median 16 B | +8.5 % | +5.1 % |
  | median 64 B | +4.6 % | +1.9 % |
  | median 256 B | +2.2 % | +0.9 % |

- **Tail inlining and liveness-based fixed ranges in the asm (item 15).**
  - **Tail inlining:** in `return g(...)` with `g` inlined, the `return`s of `g` are those of
    the caller (to `rax`, a tail call or the result of the outer inlining). That way no
    result register stays live across the whole body.
  - **Fixed ranges:** a fixed range inside a block (`mov rax, x; jmp .Lret` at each return)
    conflicts only with a value live there. The registers that the code names inside the
    interval go to the end of the preference order.

  Instruction medians against the best C: 16 B from +11.5 to +8.5 %, 64 B from +5.9 to
  +4.6 %, 256 B from +3.0 to +2.2 %. 58 rows improve; byteset AVX2 at 16 B gains one
  instruction (one more callee-saved register and a shifted alignment NOP).

- **Cross-statement CSE and spill cleanup in the asm (item 15).**
  - **CSE:** the value of an immutable GPR-class `let` is reused in what that block
    dominates. The key carries the version of each variable (each assignment changes it) and,
    in a load, the memory epoch (each store or call changes it). A loop changes in advance
    what its body assigns or writes.
  - **Reloads:** if a register still holds the value of a slot within the block, the load of
    that slot reads the register.
  - **Dead stores:** a store to a slot that the same block writes again before reading it is
    removed.

  Instruction medians against the best C: 16 B from +13.2 to +11.5 %, 64 B from +8.9 to
  +5.9 %, 256 B from +4.6 to +3.0 %. memmem at 64 B AVX2 drops from +44 to +36 %.

- **Spill weights by static frequency in the asm (item 15).** The lowering marks as rare a
  branch of an `if` without `else` inside a loop when that branch loops or returns (the
  candidate verification of a search kernel; this is the Ball and Larus heuristic). Each read
  or write weighs 8 per loop and is divided by 64 for each rare branch or cold region. On
  spilling, the cheapest interval is chosen, instead of the one that ends last. Teddy drops
  7-10 % in instructions (AVX2 at 4 KiB: from +20 to +8 % against the best C) and memmem
  1-2 %.

- **Live range splitting in the asm allocator (item 15).** A spilled value that a
  single-entry region only reads (a whole loop, or the longest prefix of a branch or of the
  code after its single definition) gets a copy on entry. The copy competes for a register
  only in that region, and only if the previous scan left one free over the whole segment;
  if it is spilled anyway, the split is undone. Also:
  - wrapping arithmetic on u8/u32 is done in 32-bit registers, without truncating: the
    Rabin-Karp hash drops from 7 to 3 instructions per byte;
  - `h = f(h, ...)` is computed in the register of `h`;
  - a `mov` between two spilled values uses a single scratch, and the reload that follows the
    store to the same slot is removed.

  memmem at 16 B goes from +67 to +25 % instructions against the best C, and Teddy and memmem
  drop between 1 and 3 %.

- **The asm against GCC, Clang and MSVC, second part (item 15).** The detail of the changes and
  the tables is in the [report](benchmarks/2026-11-20-asm-counters-wsl.md):
  - inverted loops with flag fusion;
  - tail calls, and direct calls through the dispatcher's pointer;
  - spills without scratch in `mov`s, and an exact scratch reservation;
  - common subexpressions per statement;
  - `bt`, and jumps on booleans without materializing them;
  - `sub r, -128`.

  New measurements: `harness/asm/wtime.py` measures against MSVC by wall clock on Windows.
  NVOL's full C compiles with `cl /O2 /W4`, and `bench.c` compiles with MSVC. A spill weight
  by loop depth was tried and discarded, because without frequencies it made memmem worse.

- **The asm against GCC and Clang, first part (item 15).**
  - **Measurement:** `harness/asm/bench.c` + `counters.sh` measure each operation by size and
    path with `perf stat` in WSL, over the asm, the C compiled by GCC and the C compiled by
    Clang ([report](benchmarks/2026-11-20-asm-counters-wsl.md)).
  - **At the start:** the asm was worse than the best C in all 120 rows, from +4 to +125 % in
    instructions.
  - **Fixed:** the six optimizations in the report, plus symbols with size in ELF.
  - **Found along the way:**
    - moving loop exits before register allocation stretched the intervals (copies, spills
      and a 4 KiB frame). Now they are moved afterwards.
    - the first version broke Two-Way: `_into` renamed the result of an inline that a cold
      block still wrote. The fuzzer now generates that pattern (a helper with an early
      `return` assigned inside a loop) and detects it; `test_asm` tests memmem with needles of
      up to 89 bytes.
    - ml64 limits the length of a line, so large constants go in rows of 16.
    - GNU as and ml64 shrank jumps that NVOL left long because of alignment. Now the three
      syntaxes write the decided size: `{disp8}`/`{disp32}`, `SHORT`/`NEAR PTR`, and in NASM
      `strict short`/`strict near`, because without `strict` its optimizer still shrinks a
      `near` (this happened only in ELF).
    - Load folding put two memory operands in one instruction when an operation compared two
      loads (the test functions of the primitives). Now it does not fold into an instruction
      that already has one.
    - `samebytes` misread `objdump -s` with addresses of more than 4 digits and lost the
      function table in ELF. Now the table is read by its symbol.

- **Review of the asm backend (item 14).** In three deliveries.
  - **New tests:**
    - `tests/test_asm_fuzz.py`: random IR programs against the interpreter. There are 48 by
      default; a batch of 600 also passed.
    - `tests/test_asm_encode.py`: about 1300 instructions at the edges of the encoding.
    - Teddy directly in `test_asm`, with its 17 functions.
    - The visitor requests a stop with `0x100`.
    - `tests/test_asm_unwind.py`: a C++ exception thrown from a visitor crosses the kernel and
      is caught, with each syntax, on Win64 and on System V.
  - **Bugs found:**
    - **There was no unwind.** `.pdata`/`.xdata` (Win64) and `.eh_frame` (System V) were
      missing, and that exception terminated the process. Now the three syntaxes give the same
      tables byte for byte: NVOL writes NASM's, and ml64's and GNU as's come from their
      directives.
    - `asm_module` did not emit the module's tables; the fuzzer found it.
    - A `disp8` or an `imm8` with the range off by one was not detected.
    - In NASM, `$` inside a `dd` with several values is the start of the line.
  - **Code review** of `nvol/asm/` (an independent reading, without running anything). It
    found no miscompilation in allocation, the frame, the encoder or the printers for what the
    IR can produce today.
    - **Evaluation order:** in the variable shift, in nested `PtrAdd` and in `CallTable` it
      differed from the interpreter. This only shows with side effects inside an expression,
      which the IR does not have except `Visit`. It was corrected anyway and now matches the
      interpreter.
    - **Cases that do not occur today become explicit errors at generation time:** vectors as
      a parameter or return value of an out-of-line function, an AVX2 kernel inlined into a
      function without VEX, more than 3 spilled registers of one class in one instruction, and
      a frame of 4 KiB or more, which on Win64 would need stack probing (`__chkstk`).
    - **Commented:** the identical `vmovdqa` that the peephole removes in VEX (it would zero
      the upper half, and nothing reads it) and the `jmp` of a single-target dispatcher, which
      is now an explicit `near`.
    - **Reviewed and correct:** intervals and liveness across loops, fixed ranges, spills and
      scratch registers, frame alignment, REX of the byte registers, the VEX store form, the
      termination of `relax`, and the order and scaling of the unwind codes.

- **One reference, not three (item 13).** The three syntaxes give the same machine code:
  - **Comparator:** `nvol/asm/samebytes.py` assembles the library in each syntax and compares
    the disassembly function by function. It masks the bytes that a relocation covers (NASM
    resolves against the section or within it; ml64 and GNU as against the symbol) and drops
    the trailing padding. The function tables and the read-only data are compared as bytes.
  - **Result:**
    - NASM and GNU as give the same bytes in the 43 354 instructions of Win64 and the 42 703
      of System V.
    - MASM gives the same instructions and lengths; in 7 413 it changes the register-to-register
      direction (`8b`/`3b`/`2b`/`33`/`23`/`0b`/`03` instead of
      `89`/`39`/`29`/`31`/`21`/`09`/`01`). It is the only difference the test allows.
  - **Found and fixed:**
    - NASM 2.16.01 left long 10 jumps whose short form fit.
    - It padded `.rdata` with `nop`.
    - NASM and ml64 did not choose the 2-byte VEX form of a `vmovdqa ymm6, ymm9`, and were
      missing that byte.
  - **How it was fixed:** `nvol/asm/encode.py` computes the length of each of the 121 forms
    the backend emits and relaxes the jumps (all short; those that do not reach grow). The
    NASM and MASM printers write the size (`short`/`near`, `SHORT`) and the bytes of the store
    form. GNU as relaxes on its own and serves as the control.
  - **Tests:** the lengths match GNU as's in both ABIs, jumps included (0 mismatches).
    `tests/test_asm_syntaxes.py` runs in the pytest step and in `asm-sysv`.
  - **Tested red:**
    - NASM without the jump sizes;
    - NASM with the `nop` padding;
    - NASM without the store form;
    - the 0F38 map taken as a 2-byte VEX: NASM rejects an out-of-range short jump.

    A `disp8` with the range off by one is not detected, because the library has no such
    case. It goes to item 14.
  - The three toolchain findings go to the list in item 20.

- **GNU as, AT&T (item 12).** `nvol/asm/gas.py` prints the same MIR in AT&T for COFF (Win64,
  MinGW's `as` 2.42) and ELF (System V, Alpine's `as` 2.45.1), in `generated/gas/` (`.s`).
  - **Syntax:**
    - operands go in the reverse of Intel order, also in the three-operand VEX forms and with
      an immediate;
    - `%`, `$`, `disp(base,index,scale)` and `sym+disp(%rip)`;
    - a size suffix only where no register fixes it;
    - `movzbl`/`movzwl`/`movslq`, `movabsq` for 64-bit immediates, and `*` in indirect jumps
      and calls.
  - **Labels:** `.L` labels belong to the file and not to the function, so they carry the name
    of their function.
  - **Symbols in ELF:** `.type`, `.hidden`, `.size`, the local alias `L.name` and `@PLT` for
    the CPU checks. In COFF, `.def`.
  - **Verification:**
    - The object's disassembly matches NASM's instruction by instruction. Only the bytes of
      the function table, which objdump decodes as code, and the padding differ.
    - `test_asm` runs the three syntaxes in each case: 257 tests in 5:22 on Win64.
    - The 65 `native` tests pass over `gas` on Win64 and on System V.
    - The `asm` and `asm-sysv` steps build and test `generated/gas/`.
  - **Tested red:** the memory displacement with the sign inverted only in the GNU as printer
    (`native` crashes and 9 of `test_asm` fail).
  - **Toolchain problem found:** COFF GNU as cannot mark `.drectve` as `LNK_INFO`, and
    `link.exe` ignores its exports. It goes on the list in item 20, with the workaround in
    `build_msvc.py`.

- **MASM (item 11).** `nvol/asm/masm.py` prints the same MIR as NASM in `ml64` syntax
  (Win64), in `generated/masm/` (`python -m nvol gen --lang masm`; `nvol.c` and `nvol.h` are
  the same as NASM's).
  - **Syntax:** `PROC PUBLIC`/`ENDP`, labels local to the `PROC` (`L_<name>`), memory operands
    with `byte ptr` … `ymmword ptr`, RIP-relative symbols without `default rel`, and
    `OPTION CASEMAP:NONE`. The constants go in a `_NVOLK` segment aligned to 64, with an alias
    to `.rdata`, and the function tables go in `_TEXT`.
  - **`ml64` is found** through `$ML64`, the PATH or vswhere: `nvol.asm.masm.find_ml64`.
  - **Exports:** they are written by hand into `.drectve` with `-export:`. MASM's `PROC EXPORT`
    writes `/EXPORT:`, which `link.exe` reads but MinGW's `ld` ignores; with it, the library
    linked by gcc came out without exports.
  - **Tests:**
    - `test_asm` runs the interpreter once per case and compares each syntax against it (the
      257 tests, without doubling the cost).
    - The `masm` backend of `native` runs the library's 65 tests over the forced paths.
    - The `asm` step of `verify.sh` assembles the committed library with `ml64`, links it with
      gcc and with MSVC, and runs the smoke test on it (`harness/asm/assemble.py` chooses the
      assembler).
  - **Tested red:** MASM's `.asm` with `pcmpgtb`, and the negative displacements with the sign
    inverted only in the MASM printer (`native` crashes and 4 of `test_asm` fail).
  - **Found in passing:** `vbroadcasti128` reads 128 bits even though it writes a `ymm`, and
    now the lowering says so. NASM inferred it; `ml64` does not. Also, the Win64 unwind is
    missing in both syntaxes, recorded in item 14.
  - **The bytes differ from NASM's** (for item 13): MASM encodes register-register
    `mov`/`cmp`/`xor` with the `8b`/`3b`/`33 /r` form and NASM with `89`/`39`/`31 /r` (same
    semantics), and the relocations go against the symbol instead of the section.

- **x86-64 assembly backend, NASM (item 10).** `nvol/asm/` lowers the IR to instructions with
  no compiler in between: `lower.py` (IR → MIR over virtual registers), `regalloc.py` (linear
  scan, spills, frame), `abi.py` (Win64 and System V), `nasm.py` (printer). Output:
  `generated/nasm/nvol_x86_64_win64.asm` and `nvol_x86_64_sysv.asm`, plus `nvol.c` in
  `asm_kernels` mode (glue, CPU checks, test hook) and the same `nvol.h`. In four steps:
  1. **Scalar.** Values zero-extended to 64 bits (the `Cast`s are free; only
     `wadd`/`wsub`/`wmul`/`shl` on u8/u32 truncate), rotated `While`, conditions fused into
     compare-and-branch, arguments on the stack.
  2. **Vectors.** The 45 primitives in SSE2, SSSE3, SSE4.2 and AVX2: two operands in SSE,
     three in VEX; `vzeroupper` on leaving an AVX2 function; xmm6-15 saved on Win64 with
     `vmovdqu`.
  3. **Calls.**
     - Inlining with the C rule. An AVX2 function encodes the SSE kernels it inlines in
       128-bit VEX.
     - Calls through the ABI: what they clobber are fixed ranges of the linear scan; in a VEX
       function they are all the vectors, because Win64 only preserves the low 128 bits.
     - `CallTable` through a table of 32-bit offsets in `.text`.
     - Dispatch: a pointer in `.data`, `jmp [rel ptr]` and a resolver that calls
       `nvol_cpu_*`.
     - ELF with local aliases and PLT for external symbols; Win64 with exports through
       `.drectve`.
  4. **System V and the `verify.sh` steps.** `python -m nvol gen` writes `generated/nasm/`
     (the sync test covers it). The `asm` step builds the committed library with gcc and with
     MSVC (`cl /W4 /WX` + `link.exe`) and tests it (`harness/asm/smoke.py`). The `asm-sysv`
     step does the same in WSL with `nasm -f elf64` and runs the whole asm pytest
     (`harness/asm/run-sysv.sh`).

  Tests:
  - **`tests/test_asm.py`**: 211 functions (the Teddy ones need a built pattern and run in
    `test_teddy`), the 45 primitives and an inlining case of its own. Each one against the
    interpreter, through an asm trampoline that puts garbage in the high bits of the narrow
    arguments and checks all callee-saved registers. The visitors are ctypes functions; they
    check their `ctx` and record each chunk.
  - **The `native` fixture** runs over `c` and over `asm`: differential, byteset, memmem and
    Teddy, over the forced paths.

  Everything passes on Win64 and on System V. Tested red:
  - `pcmpeqb` → `pcmpgtb`;
  - not saving xmm6-15;
  - not extending the u8 values;
  - calls without clobbers;
  - `CallTable` with scale 8;
  - the visitor's `ctx` at +0;
  - a parameter assigned without copying when inlining;
  - the committed `.asm` files broken, on Win64 and on System V.

  Bugs found along the way: a `Let` of an existing vector created an integer register, and a
  third scratch was missing for a `[base + index]` store with all three spilled. Cost: the
  pytest step went from 95 to 373-479 s (depending on the run), and `asm-sysv` adds 6-7 min.
  What was seen against GCC stays in item 13.
- **Debts before phase 3.** Row 1.16c of the table was still open with "+1-3 %": closed by
  item 9 (rapidjson equal to or better than SSE2 and SSE4.2 in every row). The README said the
  same; corrected. In the report for the lead, the four rapidjson suggestions from item 8
  described the earlier state: withdrawn and proposed again with the current state. The numbers
  of the open items were cross-checked with the reports: 1.4 said "−4…+10 %" and the reports
  give −2.3…+10.4 % (17th) and +3.4…+6.1 % (18th); 1.2a cites a −6 % that is not in any report
  (recorded). 1.10 and AU21 match.

- **rapidjson on par with SSE4.2 (item 9, closing).** The last gap was `parse_len` on minified
  JSON (+0.5 % instructions), even though the whitespace skip does not run there. With
  rapidjson's loop inside the patch the difference was 0.00 %. The cause was the call to
  `find_checked` / `find_terminated` that remained in the skip for the last bytes. It almost
  never runs, but it forces GCC to keep the parser state in callee-saved registers in every
  function that inlines the skip. In the SSE4.2 build the skip no longer calls (kernel and a
  one-byte-at-a-time tail; terminated at `'\0'`, page by page). Three rounds on an idle
  machine: every row on par or better; `pretty` of `citm` stays at +0.6-0.8 % cycles with the
  same instructions (item 14b).

- **rapidjson: two-byte runs without a call (item 9 and former 9b).** In arrays indented by one,
  each element is preceded by `"\n "`, and the patch called the kernel for each one:
  `text_lines` `parse_len` +1.1 % cycles against SSE2 (it had been left as noise) and `parse`
  +1.0-1.6 % against SSE4.2. By also looking at the third byte before calling, everything
  against SSE2 is equal or better, and `text_lines` `parse` against SSE4.2 falls within the
  noise. Tried and discarded: the SSE2 kernel with four inline comparisons in the base build
  (it uses up the inlining budget again: `parse_len` +2.6-3.7 % instructions).

- **rapidjson `write` and `pretty` on par with SSE4.2 (item 9, part).** On an idle machine,
  `write` of `citm` gave +3-4 % cycles. The cause was in rapidjson's per-character loop: the
  patch's long path repeated `is.Tell() < length`, GCC merged it with the short path's
  `return` and materialized the bool (`setb` + `test`, 2 instructions per character of each
  short string). Written as `p < end` and with `stop != end` at the end, the loop is the
  original's. `write` and `pretty` of `citm` stay at +0.1 % instructions with cycles within
  the noise; `twitter` and `text_lines` improve 1-3 more points. Tried and discarded: the
  inline SSE2 copy kernel.

- **rapidjson `parse_len` on par with SSE4.2 (item 9, part).** The whitespace skip with a known
  length executed ~7 more instructions per run than rapidjson's loop: the length checked twice
  in different forms, the `sets.ready` check, the member list loaded from memory and the
  extension of the `pcmpistri` index inside the loop. The generator exports
  `NVOL_BYTESET_SET16` and `SET16_OK`, the kernel compares the index as `u32` and extends it on
  return, and the patch uses a constant list checked at startup. It is now the same machine
  code as the original. `parse_len` +0.3 % instructions (before +3.1 % on `citm`, +1.6 % on
  `twitter`); `pretty` of `citm` from +2.1 % to +0.9 %. e2e: rapidjson (5 builds, 2067
  results, prestatic) and ugrep (2/2).

- **Repo docs up to date (item 7, AU15-AU19).** README, DESIGN, the Odin xbench, rapidjson,
  audit and ugrep reports, the ugrep and simdjson inventories and this file described states
  already superseded (closed regressions, `trip`, four builds, IR without `pcmpistri` or
  vector writes, old sizes). Historical material is marked as such instead of being deleted.
  No code changes.

- **Closed with no action (former 1.7d and 1.1e b/c).** Documented differences in which NVOL is
  equal to or more exact than the reference. NEON (former 1.7d): (a) memchr: on NEON,
  `all_zeros_except_least_significant(n)` clears `n + 2` bits instead of `4n`. It is correct,
  but it re-verifies candidates already discarded in the packed pair tail. NVOL uses the exact
  mask. (b) aho-corasick 1.1.5 on aarch64 only *records* the pattern limits by mask length (the
  `return None` is missing), so NEON Teddy accepts up to 64 patterns with any mask. NVOL
  replicates that for parity. (c) The NEON movemask (`vshrn` + `& 0x8888…`) is only exact over
  0x00/0xFF lanes; `first_non_ascii` normalizes with `msb_lanes` first. memmem (former 1.1e):
  (b) The critical factorization packs position and period into a `u64` (32+32 bits): needles
  of up to 4 GiB. (c) The Finder state is read as little-endian `u64` (`load64` = native
  memcpy). On big-endian the glue and the kernel still agree, but the interpreter models LE.

- **Code cleanup (item 6, AU9-AU14).** The heaviest part is the C++ header. The kernels,
  entries and wrappers are `inline` with external linkage, but the prelude helpers (loads, CPU
  checks, `NVOL_INLINE`) were `static`: a different definition in each unit (an ODR violation,
  with no compiler warning). In C++ they are now `inline`, and the kernels with an ISA
  attribute use `NVOL_INLINE` instead of `NVOL_FORCE_INLINE inline`, which MSVC rejected with
  `/WX` (C4141). Tested with two units that include the header and link together: GCC
  `-O0`/`-O2` and MSVC `/W4 /WX`. `--isa` now fails with `--arch all` (before, it was ignored
  silently) and with ISAs that do not belong to the architecture. The audit was wrong about
  one thing: `hshin1-3` and `lane64_2/3` are used. e2e: ripgrep (memchr 143, aho-corasick 164,
  ripgrep 1199 with the usual 2 Windows symlink failures), rapidjson and ugrep with the new
  header.

- **Byteset constants exported the same in all backends (item 5, AU4 + AU7 + AU8).** The
  memchr patch for ripgrep had a fixed `[u64; 8]`, and the ugrep and rapidjson patches assumed
  `CHAIN_MAX = 6` and `RANGE == 3` without checking. Now C/C++ (`NVOL_BYTESET_*`), Rust
  (`raw::NVOL_BYTESET_*`), Odin and Python (`BYTESET_*`) export the same list, Python exposes
  `ByteSet.find` / `find_not`, and the patches use `STATE_WORDS` and `static_assert` on
  `NVOL_BYTESET_CHAIN_MAX` and `NVOL_BYTESET_RANGE_EXTRA_MAX`. e2e: ripgrep (memchr 143,
  aho-corasick 164, ripgrep 1199 with the usual 2 Windows symlink failures), ugrep (2/2
  suites, stock and nvol), rapidjson (all 5 builds identical to base in 2067 results,
  prestatic ok).

- **C++ and aarch64 compiled and tested (item 4, AU5 + AU6; former 1.7c).** No test compiled
  the C++ header or the aarch64 C/C++. Now `verify.sh` runs `smoke.cpp` natively and, for
  aarch64, compiles the C and the C++17 raw layer with Clang `-Werror` and runs `smoke.cpp`
  (C++20) under QEMU, with an Alpine sysroot (musl + libstdc++) instead of the ARM libc++ that
  1.7c planned. Tested red: a warning in the NEON code stops the step, and a broken NEON result
  stops it at the first assertion of `smoke.cpp`. `test_interp` interprets both architectures.

- **The SSE4.2 path actually tested (item 3, AU1).** No test reached the `pcmpistri` kernel:
  the pytest sets were unique-nibble or 1-2 bytes, Rust only ran the entry the dispatch chooses
  (AVX2 on this machine) and Odin had no byteset tests. Now it is exercised by pytest (8-17
  byte sets, random negated search, `skip_set16` in `test_prims`), Rust (each x86 entry the CPU
  can run, called directly) and Odin (byteset in the AVX2, base and SSE4.2 rounds). In passing:
  with masked negative polarity (0x30) the result only changes if the text has a 0, so the
  tests insert zeros on purpose.

- **`generated/` verified against the generator (item 2, AU2).** pytest tests what it
  regenerates in a temporary directory; cargo, Odin and the e2e patches compile what is
  committed. Now `tests/test_generated_sync.py` fails, with the diff, if any file in
  `generated/` differs from what the generator writes or is left over.

- **Verification in the repo (item 1, AU3).** `harness/verify.sh` replaces the script that
  lived outside the repo, which filtered the output with `grep` and so reported "OK" with the
  Rust byteset tests failing. Now each step is judged by the exit code of its own command, a
  missing tool makes the run fail (unless explicitly `SKIP`ped) and QEMU is taken from the PATH
  or from WSL.

- **SSE4.2 ISA and `pcmpistri` (part of 1.16c).** ISA `sse42` (implies SSSE3) with the
  primitive `skip_set16` (`pcmpistri`, negative polarity) in the interpreter and in C, C++,
  Rust and Odin. The byteset class grew to 112 bytes with the member list (1-16, without 0),
  and `nvol_find_not_byteset_sse42` walks it. The x86 entry without AVX2 uses it when the CPU
  has SSE4.2, and dispatch with several conditions (AVX2, SSE4.2, base) works the same in the
  four backends: before, C, Rust and Odin only looked at the first. The test hook accepts
  0/1/2 (base, AVX2, SSE4.2), and pytest and the Odin tests run the three paths. Rust and Odin
  export the sizes (Rust `NVOL_BYTESET_CLASS_BYTES`/`PAIR_BYTES`, Odin `BYTESET_CLASS_BYTES`/
  `BYTESET_PAIR_BYTES`; the harnesses had 96 and 224 hardcoded, the sizes at the time).
  rapidjson compiled with `-msse4.2` (build `nvol42`) uses the inline kernel in both
  whitespace skips.

- **rapidjson with known length on par with SSE2 (former 1.16a).** With callgrind
  (instructions per line) the difference was not in the kernel. (1) The opaque call made GCC
  reread the stream and check the parse error after each whitespace skip; the searches are now
  `pure`. (2) `nvol.hpp` in the parser's unit exhausted GCC's `inline-unit-growth` (a unit of
  12070 against 8790 estimated instructions), and `Malloc`, `Consume` and `Stack::Push` ended
  up out of line. The kernels moved to their own unit (`nvol_impl.cpp`,
  `RAPIDJSON_NVOL_IMPLEMENTATION`), like `simdjson.cpp`. (3) The dispatch is chosen once, with
  a pointer that starts at a simple loop for the case before initialization. Against SSE2,
  with counters: `parse_len` −1.1…+0.5 %, `parse` −0.8…−17.8 %, `insitu` −2.5…−23.2 %,
  writing −0.3…−21 %. In the generator, the k chains and the 16-byte ranges are emitted as
  SSE2 (inlinable in base code; the x86 entry without AVX2 uses them). 1.16c against SSE4.2
  remains.

- **Odin `memchr_iter` on par (former 2.1a, closing).** Measured with counters
  (`harness/odin/xcount` + `xcounters.sh`: the twin of `xbench` compiled for `linux_amd64`,
  `perf stat` in WSL). The gap was the setup of each call to the AVX2 kernel: with `trip`,
  LLVM computed the counter and also rebased `cur` on `start` (~5 uops per call, one call every
  ~80 bytes). The root cause: Odin emits GEPs without `inbounds`, and SCEV tied the aligned
  pointer to `start`. Now the Odin backend builds `p + (V - (addr(p) & (V-1)))` as
  `(addr(p) | (V-1)) + 1` (and `p - (addr(p) & m)` as `addr(p) &~ m`), the pointer-comparison
  loop keeps a single induction variable, and `trip` was removed from the IR. Per call at
  4 KiB: 2651 → 2388 instructions (NVOL-Rust 2528). Odin executes the same as or fewer than
  NVOL-Rust in memchr, memchr3, count, memmem, Teddy and byteset. What remains in cycles is
  code placement (at 4 KiB, the same Rust code varies 113-146 cycles depending on where it
  lands) and, in `count` at 4 KiB, a predictor effect that changes sign with size. On Windows,
  by wall clock, `memchr_iter` with 64 KiB varies from −2 % to −12 % depending on the build
  (`-microarch:skylake` alone moves it) with kernels identical to Rust's: placement, not code.
  Bug found in passing: the `foreign _` blocks of intrinsics lacked
  `default_calling_convention = "none"`, and the generated Odin did not compile for
  `linux_amd64`; now the Odin tests also pass there. Detail in
  `docs/benchmarks/2026-11-18-odin-counters-wsl.md`.

- **Teddy: the −2 % rows were noise (former 1.12).** Measured with counters
  (`harness/rust/tedcounters.sh`: `tedcount` compiled for `x86_64-unknown-linux-musl` and
  `perf stat` in WSL). With cached haystacks (4 and 64 KiB), where cycles are deterministic,
  NVOL ties or wins in the 8 cases (−0.5 … +10.5 %) and executes 3-15 % fewer instructions.
  From 256 KiB to 4 MiB the sign changes between runs: cases 0 and 4 run exactly the same loop
  (`slim_avx2_4`) and give −1.2 % to +2.4 %. The loop is aho-corasick's instruction for
  instruction (8 `pshufb`, 3 `perm2i128` + `palignr`, the same table reloaded from the stack
  and the same `vpbroadcastb`). The only difference is the loop bound read from the stack, an
  L1 load that does not show when cached. Tried and discarded: `verify64` without inlining
  frees registers, but each block with candidates reloads 10 `ymm` (−10 to −13 % in Slim
  m=2/Fat m=3).
- **ugrep on par in `[HW]ello`, `-i` and `-cw the` (former 1.14, 1.15).** They were +9 %,
  +5.5 % and +2 %. Counters in a separate build showed that the cost was not in the restarts
  but in the ~400 000 blocks with candidates that `predict_match` rejects: the kernel returned
  for each one and the re-entry paid for the `switch` and the `vpbroadcastb`s. New kernels with
  a *visitor* (`nvol_find_byteset_pair_visit*`, a `Visit` node and a `VISIT` type in the IR, in
  the interpreter and in the C/C++, Rust and Odin backends): the kernel hands each block to the
  caller and continues, and GCC fuses `predict_match` with its loop. An isolated loop
  (`restartbench.cpp`) gives +1 % against ugrep's; end to end, +1.6-1.8 % paired, within the
  noise (control −3 %). Also, the C prelude uses `__builtin_memcpy`: musl's *fortify headers*
  left overlap checks in each `load64` (1379 → 805 lines of assembly in ugrep's function;
  `-cw the` −2.3 %). A candidate cache on resume (no effect) and batched kernels (worse) were
  tried and discarded.

- **Byte sets: negated search and unique-nibble sets.** `nvol_find_not_byteset_raw` (the first
  byte outside the set) and, for sets whose members have distinct low nibbles (JSON
  whitespace, the digits), membership with a single `pshufb` + `cmpeq` instead of the nibble
  search or the `cmpeq` chains. The class grows to 80 bytes and the pair state to 192 (sizes
  at that time; today 112 and 256) (the headers publish them in `NVOL_BYTESET_*`; ugrep uses
  them and did not change). Verified by the interpreter (x86_64 and aarch64), C, Rust (native
  and QEMU) and Odin. In rapidjson it measured the same as the complement with nibble search,
  so the patch keeps the complement (1.16). From the same audit: the rapidjson patch could
  hang if parsing ran from another static initializer before the classes were built (empty
  class: the search for `'\0'` did not terminate). Building them on first use cost 3-11 %
  because of the *guard*; now they are built at startup, and the rare paths fall to a simple
  out-of-line loop (`prestatic.cpp` tests it).

- **Byte sets (former 1.13 a-b) and ugrep over full NVOL.** `nvol/kernels/byteset.py`: the
  first byte of a set, and a pair of sets at two distances, with exact membership by nibbles
  (Muła's method); sets of 1-3 bytes through the `memchr` family, of 4-6 with chains of k
  `cmpeq`; a `_mask` variant with all the candidates of the block and its extension. Verified
  by the interpreter (x86_64 and aarch64), C, Rust (native and QEMU) and Odin. Against ugrep's
  technique (`cmpeq` chains specialized per N): equal or better from N = 2 to 16, up to
  +190 %. ugrep now also routes `pinN_one`/`pinN_pma` through NVOL, using `nvol.hpp`: same
  suite, identical output, large sets −35 %, literals −14…−26 %. Changes to the generator in
  passing: kernels with an ISA target carry `always_inline` in C/C++ (with plain `inline` GCC
  left them out of line); `nvol.hpp` separates the `raw` layer (C++17) from the wrappers
  (C++20); the headers publish the state sizes (`NVOL_BYTESET_*`), whose absence made a
  benchmark overflow an array and measure wrong.

- **ugrep over NVOL, direct phase.** `harness/ugrep-e2e`: a patch to ugrep 7.8.5 connects
  `memchr` (44 sites), `nlcount`, the literal search and `pin{2,3}_one` to NVOL's C library;
  it is compiled in WSL (Linux) alongside the original version. ugrep's suite passes the same
  in both; 14 queries give identical output; literals are 10-27 % faster in total time and the
  controls stay within ±2 % ([report](benchmarks/2026-11-18-ugrep-e2e-wsl.md)). In passing it
  half answered PENDING 1.10: on Linux, with ugrep, the SIMD does weigh in total time.

- **Audit "where NVOL is worse" (former 1.2d).** `bench` and `membench` now measure
  interleaved (`nvol_harness::ab`, order alternated per round). They used to measure each side
  separately, and the bias invented or hid gaps (`memrchr` with 16 B: −25.6 % was noise;
  `Finder::new` of 40 B: −0.6 % hid −9 %). Real gaps found and closed, each one seen in the
  asm:
  - **Rust and C++ safe wrappers** converted `NULL → usize::MAX → None`; LLVM compiled it as
    an sbb/or/cmp/cmov chain after each call (`memchr` with 16 B: −7.7 % → on par; the rest of
    the short rows, +6 % to +22 %). Now they go from the pointer to the `Option` in one step.
  - **`memmem` with 2 B needles** (−4 % to −9 % with the haystack outside L1): `is_equal`,
    inlined in the packed pair loop, used two pointers and a remainder. On Win64 that exhausted
    the 15 registers, and the loop bound was read from the stack every 32 bytes. In indexed
    form the loop is the same as memchr's (9 instructions, all in registers): on par or better
    at all sizes, and `find_iter` with frequent matches went from −10…−28 % to +1…+60 %.
  - **`Finder::new` of 40 B** (−9 %): `nvol_memmem_init` is `noinline` (see 1.11).
  Tried and discarded: `noinline` in the memmem kernels (−8…−17 % with 64 B), a loop with a
  counter, out-of-line verification (−16…−50 % with frequent candidates), a base pointer plus
  distance, and `cold_path()`.

- **ripgrep end to end over NVOL.** `harness/ripgrep-e2e`: patches to memchr 2.8.3 (`*_raw`,
  `count_raw`, `memmem::Finder` and `memmem::find`) and to aho-corasick 1.1.5
  (`packed::Searcher`, Teddy) so that they call `generated/rust/nvol.rs`. ripgrep `178cc02` is
  compiled with `--config patch.crates-io`, without touching the clone. Results:
  - The memchr (142) and aho-corasick (163) suites pass, plus a new test in each crate that
    checks that the default path uses NVOL.
  - ripgrep's full suite gives the same as the original: 1199 pass and the same 2 Windows
    symlink tests fail.
  - The 14 queries over two 256 MiB corpora give byte-for-byte identical output.
  - Performance stays the same within the noise (see 1.10).

  In passing, the Rust Teddy glue used an unqualified `vec!` and did not compile in `no_std`
  crates; now it uses `std::vec!`. Detail in
  `docs/benchmarks/2026-11-18-ripgrep-e2e-i5-7400-windows.md`.

- **Odin on par in memchr (former 2.1a, first part).** Measured in a single process
  (`harness/odin/xbench` + the `harness/rust-ffi` staticlib): across different processes the
  same code varies ±7 %, more than the measured gap. Two real LLVM 20 problems, fixed:
  (1) the `or` of masks was folded into the `i1` domain and cost a `vpsllw $7` per iteration,
  solved with the `mask_or` primitive (contract: 0x00/0xFF lanes), which Odin lowers as
  `simd_max`; (2) LSR kept two induction variables, solved first with a `trip` hint in `While`
  (replaced later: see the following memchr_iter entry). Main loop: 14 → 11 uops, the same as
  Rust. Result: memchr, memchr3, count and Teddy on par with NVOL-Rust; memmem +8 %. In
  passing, a contract gap was closed: `any_nonzero` was lowered on x86 as `movemask != 0`,
  which only looks at the high bit; now it requires 0x00/0xFF lanes and is tested per
  primitive.

- **Odin backend (former 2.1).** `generated/odin/nvol.odin` (package `nvol`) with x86_64
  (SSE2/SSSE3/AVX2 and runtime dispatch via `core:sys/info`) and aarch64 (NEON) under
  `when ODIN_ARCH`. The vectors are `#simd[N]u8`. Most primitives use Odin's portable
  intrinsics (`simd_lanes_eq`, `simd_shuffle` with constant indices for
  `alignr`/`permute`/`unpack`, `simd_shr`…), and those without an equivalent are declared as
  LLVM intrinsics in `foreign _`, the same as `core:simd/x86` (pmovmskb, psadbw, pshufb,
  ptestz, tbl1, uaddlp); Odin's core does not ship most of the AVX2 ones. Idiomatic API with
  `(index, ok)`, `Memchr_Iter`, `Memmem_Finder`, `Teddy` with an allocator. Tests
  (`harness/odin/tests`): exhaustive positional, iterators, memmem (20 000) and Teddy (12 000)
  against naive references, on x86 over the AVX2 **and** SSE2/SSSE3 paths (test hook) and on
  **aarch64 under QEMU** (a `linux_arm64` object linked with Rust's musl via `rust-lld`):
  everything passes. Performance (`harness/odin/bench` + `xbench`): count 3.5× and memmem
  +31 % over the references, Teddy on par; see 2.1a. Generator bugs found: a variable name
  (`max`) shadowed an Odin builtin; the builtins are now reserved.

- **NEON / aarch64 (former 1.7).** All operations (memchr/memrchr 1-3, count SAD and parity,
  first_non_ascii, iterators, memmem with packed pair and Two-Way, Teddy SlimNeon 1-4) are
  also generated for aarch64, in the same `nvol.c`/`nvol.hpp`/`nvol.rs` under
  `#if __aarch64__` / `cfg(target_arch)`. The NEON mask is memchr's (u64, 4 bits per lane,
  positions with `tz >> 2`). The reference semantics models the exact `vshrn`, including that
  on odd lanes it reads bit 3, and that is how the interpreter detected that `first_non_ascii`
  needed to normalize the bytes. New portable primitives: `any_nonzero` (`vpmaxq`, memchr's
  `movemask_will_have_non_zero`), `vnib_hi` (like aho-corasick NEON), `msb_lanes`.
  Verification: the interpreter on both architectures; C compiled for AArch64 with Clang 21
  without warnings; the Rust harness compiled for `aarch64-unknown-linux-musl` and **executed
  under QEMU**: 6/6 tests, including memchr/memmem against the `memchr` crate and Teddy against
  `aho-corasick` (which also use NEON on aarch64).

- **Teddy (former 1.2), with parity.** `aho_corasick::packed::Searcher` with LeftmostFirst, the
  `regex-automata` (ripgrep) path. Construction is the same as aho-corasick's: `None` with no
  patterns, with an empty pattern, with > 64, with mask 1 and > 16, or without SSSE3; Slim128
  (SSSE3), Slim256 (AVX2, falls back to 128 bits below 32 + m - 1 bytes) and Fat256
  (AVX2, > 32 patterns, 16 buckets) × masks of 1-4 bytes = 12 kernels from the same template.
  New primitives, each with an individual test against its semantics (`tests/test_prims.py`):
  `vshuffle` (pshufb), `shin1-3` (alignr; in AVX2 with `permute2x128`), `hshin1-3`, `vshr4`,
  `is_zero`, `lane64_0-3`, `load_half_bcast`, `swap_halves`, `unpacklo8/hi8`,
  `first_offset64`, `cpu_ssse3`; a new SSSE3 ISA. Tests: interpreter 6924 cases (11 of 12
  variants exercised; the 12th is unreachable, 1.2c), C via Python over the SSSE3 and AVX2
  paths, Rust 20 000 cases against `aho_corasick::packed::Searcher` (`find_in` and
  `find_iter`). Performance (`tedbench`, interleaved): throughput −2.9 %…+7.8 %, 64 B
  −3 %…+27 %, construction 7-23× faster; focused A/B 0…+6.7 %. Three causes of gaps found and
  fixed along the way: (1) the AVX2 entry inlined the 12 variants (5490 lines of asm, a
  prologue with 8 GPR + 10 xmm per call) → `noinline` kernels; (2) two levels of dispatch → a
  function table in the IR (`CallTable`) with the index chosen at construction, one indirect
  call like aho-corasick's `dyn SearcherT`; (3) the verification used memchr's `is_equal` (2-
  and 1-byte tails) instead of aho-corasick's (an overlapping last 4-byte read) →
  `nvol_is_equal_ac`.
- **Generator bugs found with Teddy.** Variable renaming could generate a name already in use
  (`r` → `r1` when `r1` already existed); now it never repeats. A variable could be named like
  a reserved word (`switch` in C); now the reserved words of C, C++, Rust, Python and Odin are
  avoided. Public functions with an ISA did not receive the `target` attribute in C.

- **One-shot `memmem` with a 64 B haystack (former 1.1a): from −5 % to +50 %.** The cause was
  building the Finder: 8 independent calls to `init_field(k)`, with the rare byte pair and the
  Rabin-Karp hash each computed twice. Writes were added to the IR (the `MPTR` type, the
  `Store64` statement, `VOID` functions; former 2.15), and `nvol_memmem_init(needle, nend,
  out)` fills the 8 words in one pass. `Finder::new` of 8 B: 21 ns against memchr's 33 ns
  (+57 %); of 40 B (with Two-Way): on par. One-shot of 64 B: 31.8 ns against 47.6 ns.

- **`memmem` (former 1.1), with parity or better.** The ripgrep path (`regex-automata` →
  `memmem::Finder::find`): a selector identical to `memmem/searcher.rs` (empty → 0; 1 byte →
  memchr; 2-32 → SSE2/AVX2 packed pair with Rabin-Karp if the haystack is shorter than the
  pair needs; > 32 → Rabin-Karp if the haystack < 16, otherwise Two-Way with the packed pair
  prefilter and `PrefilterState`), the rarity table copied as is, and the Rabin-Karp shortcut
  for one-shot `find` < 64 B. Finder state = 8 `u64` words that the kernel only reads. Each
  one is computed by `nvol_memmem_init_field` (a pure IR function), so the per-language glue
  is trivial. Tests: interpreter 3000 cases with every branch exercised; C via Python (random,
  alignments 0..63, tails, pathological cases); Rust 40 000 cases against
  `memchr::memmem::find` and `find_iter`. Performance (`membench`): packed pair +13…+63 %,
  Two-Way −1…+4 %, pathological cases on par, `find_iter` +8…+43 %; the only row below: 1.1a.

- **The ~10 % gap when iterating positions (former 1.3), closed.** The cause, seen in the asm:
  the kernels and the caller's loop were almost identical instruction for instruction, but each
  match had ~6 extra instructions in the dependency chain (the caller computed `len = hi - lo`,
  the kernel rebuilt `end`, returned an index `at - start`, and the iterator added `lo + r`
  again). Now the kernels use the `memchr::find_raw` signature: `(start, end) -> *const u8`
  (NULL = nothing), the iterators call `*_raw`, and the `(ptr, len) -> index` API is a thin
  wrapper. Result (`itbench`): forward −1.5 %…+1 % on average (before −3 %, peaks of −11 %),
  backward +8 %…+11 % (before −3.5 %, peaks of −10 %).

- memchr/memrchr with 1-3 needles, `count` (SAD 2-4× faster), `count_parity`, `first_non_ascii`.
- ifunc-style dispatch (cached pointer) in C, C++ and Rust: it closed almost the whole gap of
  the "one memchr per line" pattern.
- `memchr_iter`, `memrchr_iter`, 2 and 3 needles: double-ended, `size_hint`, specialized
  `count()` (Rust); an iterator struct in C, a class with range-for in C++, generators in
  Python.
