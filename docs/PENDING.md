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
    - **Teddy:** +6/+10 % instructions from 64 B to 64 KiB. (At 16 B, 3452 → 415
      instructions with the Rabin-Karp, item 27.) It was the
      slow path `nvol_teddy_slow` (see 1.2a, item 27).
    - ~~**NASM local labels in the ELF symbol table**~~ (`L.nvol_x.L36`, which perf charged
      samples to): jump targets are now offsets from the alias (see Done).
    - ~~**2-byte VEX**~~ **(tried and rejected, 2026-11-21):** commutative operations with
      the ymm8-15 operand in ModRM.rm need a 3-byte VEX, and swapping the operands makes
      them fit in 2. A pass after allocation swapped all 325 in the System V library (276
      `vpcmpeqb`, 39 `vpor`, 10 `vpand`, out of 3497 three-operand register instructions):
      `.text` 170624 → 170304 bytes (−0.19 %), with the same instructions and µops. Over the
      120 asm rows, 8 were better than −3 % and 11 worse than +3 %, including byteset AVX2
      4 KiB +14.2 % and 64 KiB +9.2 % (steady-state loops). By construction everything it
      changes is layout, so there is no code benefit to isolate, and 0.2 % of size does not
      pay for the draw. Not taken.
    - ~~**Win64:** xmm6 is saved in memchr ×4~~ **(justified, not changed, 2026-11-22).** The
      exit test is `(a|b)|(c|d)`, and the four compare vectors stay live for the code that
      finds the match, so the needle, the four vectors and the two ORs make 7 at peak, one
      more than Win64's volatile xmm0-5. The C backend's same kernel compiled for Win64:
      - MSVC 18 (`cl /O2`): saves xmm6 in the SSE2 and the AVX2 entry, like the asm;
      - MinGW GCC 13.2 (`-O2`): saves xmm6 and xmm7 in SSE2, xmm6 in AVX2.

      The chain `((a|b)|c)|d` needs 6 and no save, for one more OR of latency per iteration.
      It is untried: the OR tree is in the shared IR (the language backends would change
      too, as the memrchr dual loop did: Clang +22.5 %), and the save is 4 instructions
      outside the loop.
    - **Justified, not changed:** `count_parity` is the faithful port of memchr's `count_raw`.
      GCC and Clang autovectorize its scalar head and tail, and the asm does not autovectorize.
    - **aarch64 (item 16): +12.1 % static instructions against Clang -O2** over the 31 public
      functions ([report](benchmarks/2026-11-21-a64-static.md); only static, cycles wait for
      item 31). Seen in `nvol_memchr` (16 against 12 instructions):
      - ~~the save as `sub sp` + `stp` instead of a pre-indexed `stp x29, x30, [sp, #-32]!`~~:
        the first save and its load now take the `sub`/`add sp` in their writeback (see Done);
      - ~~a branch where Clang uses `cmp` + `csinv`~~: a select now (see Done);
      - ~~a copy not coalesced while its source is live (`mov x19, x1` … `mov x1, x19`)~~:
        `regalloc.copy_back` removes the copy back, on both targets (see Done);
      - ~~`and w0, w0, #255` that the callee repeats~~: a parameter only passed on is no
        longer extended (see Done).

      ~~The byteset family (+25…+53 %), memmem (+34 %) and Teddy (+36 %) need their loops
      compared by hand~~ **(compared, 2026-11-22).** The static gap is not in the hot
      loops:
      - **The loops are as short as Clang's or shorter.** byteset's single-byte form, per
        16 bytes: NVOL 10 instructions, Clang 12. Clang tests at the top and the bottom
        (two branches per iteration) and carries a counter for the tail mask; NVOL's loops
        are rotated (one branch).
      - **The table counts structure.** Each NVOL entry counts with what it tail-calls (the
        `_u`, `_k1`, `_k2`… variants, and memmem's `pp`, `pp2`, `pppre`, `tw` and `rk`).
        Clang inlines them into one body, where shared code is counted once. memmem's
        packed-pair loop also carries its candidate check inline (the word compare), which
        Clang's merged body shares.
      - **One loop gain found, deferred:** Clang's post-indexed load (`ldr q2, [x1], #16`)
        folds the pointer increment, one instruction per iteration. The found path then has
        to subtract 16, in cold code that is reached from inside the iteration. Without ARM
        hardware it is a static count only (2.12, item 31), so it waits for a machine where
        its cycles can be measured.
      - The Teddy dispatch was a real gap (a `blr` with a frame where Clang has `br`): fixed,
        see Done.
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
    - ~~pair AVX2, +20 %, with 13 % fewer instructions~~: each block was loaded twice. Fixed
      (see Done): at 4 KiB the asm now gives 460 cycles, against 467 for GCC and 490 for Clang;
    - memmem SSE2, +22 %;
    - 14b: rapidjson, `pretty` of `citm_catalog` against SSE4.2. Rebuilt with the current
      header and measured in 3 rounds (`harness/rapidjson-e2e/counters.sh`, best of 7):
      cycles +0.0, +0.8 and +1.5 %, instructions +0.07, +0.02 and +0.01 %, mispredictions
      −15 % (6.2 k against 7.2 k per op). With the raw frontend events: DSB µops +3 %, MITE
      µops −6 %, switch penalty −11 %. The remaining difference is within this row's spread
      (±1 %). Without a cause that the counters can see, it stays open with that spread recorded.

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

      In the 64-aligned layout no function body changed, so each of its rows is position by
      construction. The count_parity control shows the trade: at +48 bytes, the SSE2 path gets
      HEAD's cycles back, but the AVX2 path loses its −29 % (81.3 → 116.5 at 64 B).

      The last two rows are not all position: of their +3.5-3.8 cycles, the length check
      itself costs 0.4-0.9 (the guard control against the NOP control). That part is a code
      cost, and it was accepted as a trade-off (see Done).

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

      In the glibc binary every pad of the *call* is slow and MITE does not explain it. Moving
      the *callee* does (NOPs at the start of `nvol_memchr_entry_avx2`, Arch, best of 9):

      | pad | 0 | 2 | 4 | 6 | 8 | 10 | 12 | 14 | 16 | 20 | 24 | 28 |
      |---|---|---|---|---|---|---|---|---|---|---|---|---|
      | cycles | 18.0 | 16.6 | 16.7 | 16.9 | 17.4 | 18.0 | 19.0 | 19.5 | 19.2 | 18.4 | 19.9 | 21.2 |
      | MITE µops | 0.8 | 0 | 0 | 0 | 0 | 0 | 0.9 | 0 | 20.0 | 24.0 | 28.4 | 31.9 |
      | switch penalty | 0.1 | 0.1 | 0 | 0 | 0 | 0 | 2.8 | 0 | 0.1 | 0 | 0.9 | 0 |

      (The NOPs run at the entry, so the DSB µops grow with the pad.) Pads 2-6 reach the
      musl binary's fast state (16.8), and from 16 bytes up the callee falls out of the DSB.
      So the µop cache explains the effect in both binaries; in glibc the window that decides
      is the callee's. The fix for the generator would be to place the hot
      dispatch code so that no 32-byte window overflows the DSB (on Skylake, at most 3 ways
      of 6 µops per window). That needs the µop counts per window that
      `encode.py` can give.

    - **The loop rows are not the frontend.** `harness/asm/frontend.sh` (4 raw events per
      run, asm against GCC and Clang): in memrchr 4 KiB AVX2, byteset 4-64 KiB AVX2, pair
      4-64 KiB AVX2 and memmem 4-64 KiB SSE2, the three builds take every µop from the DSB
      (MITE ≈ 0). With the port events (`EV=cycles,r01a1,...`, `uops_dispatched_port`, and
      `r010e` for `uops_issued.any`), at 4 KiB:
      - **pair AVX2:** the asm sends twice the µops to the load ports (265 + 266 on ports 2
        and 3, against 137 + 138) and issues 2011 µops against 1769. The loop reads each
        block twice, `vpand ymm8, ymm6, [rsi + r8]` and `vmovdqu ymm9, [rsi + r8]`, and
        `NVOL_K3` twice per iteration from memory. Fixed by sharing foldable loads (see Done);
      - **memmem SSE2:** ports and issued µops are the same in the three builds (2428, 2434
        and 2415), and the cycles are not: asm 797-810, Clang 794-798, GCC 660-665. So the
        asm is on par with Clang, and GCC is the outlier. Not mispredictions (1.0 per call in
        all three), not BACLEARS, not the frontend. Both GCC and NVOL take two branches per
        16-byte iteration (`je` over the verification, then the latch). GCC issues both loads
        before the compares, and uses `tzcnt` where NVOL uses `bsf`. Neither explains it.
        The asm hand-edited with both loads first gives 812-823 cycles (unchanged). `tzcnt`
        in every `first_offset` has no measurable gain against a position control (below).
        What makes GCC faster is still open. Narrowed further (2026-11-21):
        - the same instructions (2916 against 2918 per call), the same branches (527: 256
          iterations × 2, so the bench finds no candidate) and the same mispredictions (1.0
          against 1.2), at 841 against 710 cycles: 3.3 cycles per 16-byte iteration against 2.8;
        - not placement alone: NOPs at the function's start (0-28 bytes) give 809-1070 cycles,
          never GCC's 685-710;
        - an idea to beat both: move the candidate loop (`while m != 0`, a loop nested in the
          scan) out of line as a cold region, so that each iteration takes one branch instead
          of two (`je` over the verification, then the latch). Tried in `Lower`'s `While`; it
          broke the allocation of a large function (a 5032-byte frame, refused: no stack
          probes). The new `jmp` back from the region adds blocks that `_regions`/`_split` handle
          badly. Reverted; it needs that fixed first;
      - **`tzcnt` for `bsf`, tried and rejected (2026-11-21).** `bsf` waits on its
        destination's old value, a false dependency that `tzcnt` (`rep bsf`) does not have.
        Emitted in all 627 `first_offset`s, it seemed to give memmem 4 KiB AVX2 −29 % and 256 B
        SSE2 −7 %. The control is the old code generated with a CS prefix (`0x2e`, ignored)
        before each `bsf`: the same length as `tzcnt`, so the same layout (the same function
        addresses), with `bsf`'s execution. Against it, 113 of 120 rows are within ±3 %, the
        other 7 within ±5 % and in functions whose hot path has no `tzcnt`. Every gain was the
        layout: memmem 4 KiB AVX2 559.5 before, 386.8 in the control, 396.6 with `tzcnt`.
      - **memmem 4 KiB AVX2 swings 30 % on position alone** (that control: 559.5 → 386.8
        cycles with every function after the first `bsf` shifted by a few bytes), and the
        committed layout is the slow one.
        - The cause: in the slow layout the packed-pair loop (`9a70-9a8b`) straddles a 32-
          and 64-byte boundary (`vpmovmskb` at line+63, `test; je` in the next line). It issues
          ~117 more µops per call (1418 against 1301, for the same 1258-1261 instructions),
          about one per iteration, with the same mispredictions (0.97 against 0.84), machine
          clears (≈1 per call in both) and MITE (≈0). In the fast layout the loop (`9b20-9b3b`)
          sits inside one 32-byte window.
        - **Tried and rejected: a layout rule.** Functions 32-aligned (so that offsets map to
          windows), plus a short loop (≤ 32 bytes) that would straddle a window moved 16 bytes
          further, still 16-aligned. A control with the functions 32-aligned and the rule off
          splits the two effects:
          - the 32 alignment alone: 13 rows better than −3 %, 33 worse than +3 %. memmem
            4 KiB AVX2 556.9 → 391.1, but `count_parity` 16 B SSE2 +29 % and `byteset_not`
            64 B AVX2 +19 %;
          - the rule on top of it: 17 better, 13 worse; memmem 391.1 → 403.4 (it is not
            what fixes memmem).

          (A first version, the least padding that fits rather than keeping 16-alignment,
          moved every loop head and made byteset AVX2 4 KiB +12.7 %.) Both are draws in the
          same position lottery, not a rule that holds. memmem's loop placement stays open.
        - **Adopted: functions 64-aligned, for stability (2026-11-22).** The aim is not a
          better draw but that a draw stays put: an edit to one function no longer moves any
          other. `encode.FUNC_ALIGN = 64`, in all three x86 printers (the sections 64-aligned
          too). Loop heads keep HEAD's rule, and padding counts from the function's start, so
          every function body is byte-identical to HEAD's; only the start addresses change.
          It grows `.text` 170624 → 176432 bytes (+3.4 %). Measured with 13 ops × 5 sizes ×
          2 paths, best of 5, against an identical copy of HEAD as the noise control:
          - **Before:** HEAD with 7 dead bytes after its first `ret` moved memmem SSE2
            64 KiB +33 % (12433 → 16584), 4 KiB +31 %, 256 B +20 %, count 4 KiB AVX2 +26 %,
            memchr 4 KiB AVX2 −22 %.
          - **After:** an edit absorbed by the padding moves nothing (no other function
            changes address). One that crosses a line shifts every later function by 64.
            That moves no row beyond the noise control, except memchr 4 KiB AVX2
            (108 → 135, with 108 → 116 in the control itself) and first_non_ascii 4 KiB AVX2
            (same spread in the control).
          - **The one-time redraw** against HEAD: geomean −1.09 % (noise −0.12 %), 19 rows
            better than −5 %, 11 worse than +5 %. Better: memchr2 16 B −17/−23 %,
            first_non_ascii 4 KiB SSE2 −17 %, memmem 4 KiB AVX2 −10 %, count_parity
            64-256 B AVX2 −29 %. The worse rows are in the position-loss log above.
          - **Rejected: every loop head 32-aligned as well.** A loop's window would depend
            only on its own function's code after it. But rotated loops are entered by
            falling through, so each entry runs up to 31 NOP bytes, and `.text` grows 8.2 %.
            Geomean −0.64 %, 16 rows worse than +5 %, against 11 for the 64-aligned functions
            alone.
      - **memrchr AVX2:** 5 rounds (best of 7 each): asm 129-147, GCC 123-134, Clang
        110-118. The asm is about +20 % against Clang with fewer instructions (465 against
        495), everything from the DSB. It is a fixed cost per call, not the loop. At 64 KiB the
        asm and Clang are even (2773-2908 against 2776-2887), and at 4 KiB about 28 cycles
        are left. Issuing the four loads off the old pointer before the `lea`, in Clang's
        order, changes nothing (149-150 against 147-153, 3 rounds), so the address chain is
        ruled out. **The cause is the loop exit's misprediction, and the shape decides it.**
        The asm and GCC mispredict once per call (1.0, taking the best run of 7), and Clang
        does not (0.0). Rewritten in Clang's shape (exit test at the top: `cmp; jb` out, the
        body, `test; je` back, `jmp` to the match), the asm also gives 0.01. Three variants
        (3 rounds, cycles):

        | memrchr AVX2 | 128 B | 256 B | 1 KiB | 4 KiB | 64 KiB |
        |---|---|---|---|---|---|
        | NVOL's shape | 16.7-17.1 | 20.1-21.4 | 36.4-39.0 | 146-152 | 2825-2828 |
        | Clang's, near jumps | 24.8 | 27.5-28.4 | 44.0 | 121-131 | 2808-2857 |
        | Clang's, short jumps, no repeated entry check | 20.0-20.4 | 23.2-23.4 | 37.2-38.1 | 125-129 | 2832-2916 |
        | Clang | 18.0-18.2 | 21.0-21.3 | 38.9-40.0 | 117-128 | 2480-2826 |

        No shape wins everywhere. Clang's removes the misprediction once the loop runs ~31
        times (4 KiB: −14 %), but costs ~3 cycles when it runs once or twice (128 B: +18 %).
        Clang itself also loses to NVOL there. Under the rule (a worse row rejects), the swap
        is not taken. Tried next (maintainer's go-ahead): both loops in the kernel IR, chosen by
        length (Clang's shape from 24 iterations of the unrolled loop, x86 only). Rejected:
        - the IR is shared, so the C gets both loops too, and **Clang compiles it worse:
          memrchr 4 KiB AVX2 +22.5 %** (123.7 → 151.5). GCC is within noise;
        - the asm gains where expected (4 KiB AVX2 −12.3 %, SSE2 −8…−9 % from 1.5 KiB, 64 KiB
          −1…−2 %). But on AVX2, 24 iterations (3 KiB) still loses (+8.5 %), and the gain
          starts at 32. SSE2 gains from 16. So the threshold is not one count of iterations;
        - rows with the same instruction count swing ±6…18 % either way (16 B and 64 B), which
          is layout. Rows near the threshold cannot be judged without a position control.
        Done as an asm-only loop attribute, `While.exit_first` (see Done), accepted with a
        recorded trade-off: −12…−18 % at 4 KiB AVX2, and about 3 % real cost at 256-512 B AVX2.
        SSE2 gains nothing from the shape. Measured since (2026-11-21):
        - **SSE2 has no gap up to 4 KiB:** 1 KiB asm 84.0, GCC 81.7, Clang 85.7; 4 KiB asm
          284.6, GCC 285.3, Clang 302.5. The gap is at 16-64 KiB, in both paths: 64 KiB SSE2
          asm 5607-5710 against GCC 4796-4925, and 16 KiB AVX2 504-525 against 445-468, with
          the same instructions (19512 against 19520 at 64 KiB);
        - not memory: the same L1 replacements (1029 per call = 64 KiB / 64), L2 demand reads
          (768 against 782) and L2 prefetches (1231 against 1125). The asm has 3× the
          RS-full stalls (`resource_stalls.rs`, r04a2: 1044 against 365 cycles per call in
          SSE2, 1096 against 823 in AVX2); store buffer and ROB are not full;
        - ruled out by hand-edited asm (3 rounds): the four loads in descending order (5572-
          5651, unchanged), and GCC's operand form (`movdqa xN, xV; pcmpeqb xN, [m]`, the
          load folded: 6062-6101 and 1364 RS-full stalls, worse). Still open.

    The JCC erratum, alignment (16, 32 or 64) and the order of the loads are already ruled
    out. The asm serves as the control: it is the same kernel without a compiler.
26. ~~1.4: "one memchr per line"~~: measured with counters (see Done).
27. 1.2a: Teddy over short haystacks. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)):
    - ~~at 16 B, NVOL is 2× to 40× slower than aho-corasick~~: `nvol_teddy_slow` is now
      aho-corasick's Rabin-Karp (see Done). At 8-18 B it is on par in cycles (−20…+7 %), and
      it is equal or below in instructions in all 24 rows;
    - from 64 B up, NVOL is ahead or on par: +6.9…+27.2 % at 64 B (the −6…+27 % of
      `tedbench` was wall-clock noise), −5.0…+11.6 % from 256 B to 64 KiB. In instructions,
      NVOL is below aho in every row from 64 B up, so the Teddy gap in item 17 is the asm's
      and not the Rust's.
28. 1.6: memchr with 1-15 bytes. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)): NVOL has 4 fewer instructions
    per call (both scan byte by byte, 5 instructions per byte), but from 3 B up it takes 1-2.5
    more cycles: +4…+19 %, the same in 3 rounds.
    - **Cause: the JCC erratum, in LLVM's layout of this binary.** The byte loop is the same
      as the crate's (`cmp %dil,(%rax); je; inc; cmp; jne`). But in `nvol_memchr_entry_avx2`
      the loop exit `jmp` sits at 0x1c07d-0x1c082 and crosses a 32-byte boundary, so that
      window is not cached in the DSB: 2-5 MITE µops and 0.5-1.1 cycles of switch penalty per
      call, against 0 for the crate. There are no mispredictions. Built with `RUSTFLAGS="-C
      llvm-args=-x86-branches-within-32B-boundaries"`, MITE drops to 0, and NVOL comes out
      ahead at 3, 8, 12 and 15 B (11.0, 16.0, 20.1 and 23.1 cycles, against 11.5, 16.6, 20.6
      and 23.6).
    - Still open: 5 B gives 17.1 against 14.9 in both builds, with no MITE and no
      mispredictions. It is part of a curve that is not monotonic in the size (JCC-safe build,
      best of 7): NVOL 16.0 / 17.1 / 14.1 cycles at 4 / 5 / 6 B, against the crate's 12.5 / 14.9
      / 14.5, with fewer µops issued (45 / 48 / 51 against 48 / 51 / 54), no recovery cycles
      and no resource stalls. A byte loop that gets faster with one more byte is not its
      work; the bench's call or the layout. The counters available do not split it further.
    - The layout belongs to the user's build (a rustc flag), not to the generated code: it
      goes to the toolchain log (item 21). Port memchr's SWAR for the path without SIMD.
29. 1.5: `first_non_ascii` against `bstr`. Measured with counters ([report](benchmarks/2026-11-21-rust-counters.md)): −20 % cycles at
    256 B, −43 % from 4 KiB, on par at 64 B, but **+2 cycles at 16 B** (11.0 against 9.0, 3
    rounds), to be closed.
    - **Cause: the call.** bstr compiles `first_non_ascii_byte` with static SSE2 and inlines it
      into the caller (the binary has no symbol for it). NVOL goes through its runtime
      dispatch: an indirect call through `nvol_first_non_ascii_raw::FN`, then `vzeroupper` in
      the AVX2 entry. The work is the same (one 16-byte load and `pmovmskb`). NVOL issues 37
      µops against 34, with no MITE and no mispredictions. A fix would be an inline SSE2 path
      in the wrappers for haystacks shorter than 32 B, before the dispatch. Done for Rust,
      accepted with a trade-off (see Done).
    - The semantics of "none": bstr's public `find_non_ascii_byte` returns `None` when every
      byte is ASCII, the same as NVOL. Only its internal `ascii::first_non_ascii_byte` returns
      `len`. The wrappers therefore already match the public API; the maintainer decides
      whether anything should change.
30. ~~1.10: ripgrep end to end on Linux~~ (see Done). ~~The whole-file Teddy residual~~
    (`Moriarty|Lestrade|Mycroft|Hudson`: +0.54…+0.65 % cycles over the process, 3 rounds, with
    2.8 % fewer instructions) is not in the SIMD. A `perf record` over 5 runs per build (a
    single run attributed the page faults differently in each binary) gives aho-corasick's
    `SlimAVX2<4>::find` 80.05 % of 923.6 M sampled cycles (≈ 739 M), against NVOL's
    `nvol_teddy_slim_avx2_4` at 89.95 % of 806.7 M (≈ 726 M): **NVOL's kernel takes ≈ 1.8 %
    fewer cycles**. The process-level difference is elsewhere in rg
    ([report](benchmarks/2026-11-21-ripgrep-linux.md)).
31. ~~1.7a and 2.1c under QEMU~~: `bench`, `membench` and `tedbench` ran emulated ([report](benchmarks/2026-11-21-a64-qemu.md)).
    What remains:
    - `Finder::find 3B frequent "the"` is −5.0…−7.8 % at every size, a match at the start:
      the call and the prefilter setup;
    - Odin on aarch64 needs an `odin` that targets `linux_arm64` in WSL;
    - real hardware (1.7a, 2.12).
32. **AU21.** ugrep `-c zq` (2-byte memmem). Measured with counters
    (`harness/ugrep-e2e/counters.sh`, [report](benchmarks/2026-11-21-rust-counters.md)): the whole process gives +0.40, +0.80 and
    +0.68 % cycles in 3 rounds, with 0.2 % fewer instructions. The other queries are within
    ±0.65 %. The +2.8 % of the wall clock was mostly noise.
    - Per function (`perf record`, 5 runs of `-c zq` over 64 MiB): the process is dominated by
      the kernel's copy in `read()` (`rep_movs_alternative`, 34-37 %). The search itself is
      stock's `simd_advance_chars_avx2<2>`, 16.3 % of 42.7 M cycles (≈ 7.0 M), against
      NVOL's `nvol_memmem_find_entry_avx2`, 20.7 % of 39.4 M (≈ 8.2 M). That is about +17 % in
      the kernel for a 2-byte needle, diluted to +0.6 % over the process. ugrep's `chars<2>`
      compares both bytes at fixed offsets. NVOL's packed pair takes the rare-byte offsets
      from the Finder and verifies each candidate.
    - **The 2-byte kernel (see Done):** in NVOL's own asm the 2-byte path is now 14-21 %
      faster (SSE2), but it is asm-only. In C, GCC (which builds ugrep's header) already
      schedules the general loop well, and next to it Clang compiled both worse. So ugrep's
      own row is unchanged by it, and the ugrep residual stays open: GCC's C of the general
      kernel against ugrep's inline loop.

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

- **Rare regions out of line, after allocation (item 17).** Inside a loop, an arm that loops
  or returns itself (a search kernel's candidate check) was laid out in place, so the
  common path jumped over it to the latch every iteration. `regalloc.layout_rare` now moves
  each `jcc L; region; L:` out of line after the allocation: the jcc inverted to `R`, and
  `R: region; jmp L` after the function's ret. The loop runs its common path straight into
  the latch. Only the order changes. Done in the lowering, before allocation, the jump back
  had split the allocator's regions (the memmem attempt above); after it, it is plain
  layout. x86 91 regions moved, aarch64 96. `.text` 176688 → 177136. 104 rows, best of 5,
  HEAD / new / HEAD again: geomean −0.71 % (noise −0.04 %), 13 rows better than −5 %:

  | row | cycles HEAD / new / HEAD again |
  |---|---|
  | memmem 4 KiB SSE2 | 810.20 / **670.15** / 817.90 (GCC 672.5: the SSE2 gap of item 25) |
  | pair 16 B / 64 B / 256 B / 4 KiB SSE2 | −10 % / −12 % / −10 % / −12.5 % |
  | byteset 4 KiB AVX2 | 317.68 / 287.65 / 323.51 |
  | memchr3 4 KiB AVX2 | 288.51 / 246.09 / 287.79 |
  | first_non_ascii 64 B AVX2 | 16.81 / 13.99 / 16.85 |

  Three rows worse than +5 %, checked with the control (HEAD and new regenerated with every
  loop head 32-aligned): count 64 B AVX2 is noise (15.16 / 15.14, control 11.75 / 11.51);
  first_non_ascii 16 B AVX2 is placement (control 15.99 / 15.98; logged); memmem 4 KiB AVX2
  is **a trade-off**, 378-393 → 423-428 in both layouts. In this bench the AVX2 packed-pair
  loop finds a candidate often, so the region is not rare there, and out of line it costs
  two taken jumps per candidate. Accepted against the SSE2 row's −17 % and the rest.
  Teddy does not move (a predicted taken jump costs little in its loop).

- **Return tails copied into the jumps to them (item 17).** Every exit of a kernel jumped to
  one shared return (`jmp L1` … `L1: vzeroupper; ret`), where GCC and Clang return in
  place. `regalloc.dup_returns` (after the frame and the cold layout, both targets) replaces
  each `jmp L` to a short return tail (at most 4 instructions ending in `ret`, no label or
  jump among them) by a copy of the tail. It drops the tail when nothing reaches it any more.
  The CFI and the Win64 unwinder already handle an epilogue followed by more code. Returns:
  x86 214 → 1164, aarch64 123 → 558. `.text` 177072 → 176688 bytes (the jumps were as
  long as the copies, and the dead tails go). Over 104 rows (13 operations × 16 B-4 KiB ×
  2 paths, best of 5, HEAD / new / HEAD again): geomean −1.56 % (noise −0.25 %), 17 rows
  better than −5 %, 6 worse than +5 %. The control regenerates HEAD and new with every loop
  head 32-aligned, the same rule for both:
  - **placement:** memchr 16 B SSE2, count 64 B SSE2, first_non_ascii 4 KiB (equal or
    better in the control), logged in the position-loss table; memrchr 4 KiB AVX2 moves by
    ±8 between runs;
  - **trade-off:** memrchr 64 B AVX2, +1.5-2 cycles in both layouts with the same µops
    (56.1). In HEAD's layout the copied `vzeroupper; ret` brings 4 µops through legacy decode
    (MITE); with aligned loops that goes, but the cycles do not come back. No cause found
    with the counters WSL gives. Accepted: one row against a net gain.

- **count: no SAD setup below one block (item 17).** `count_sad` cleared its 64-bit
  accumulator and ended with its horizontal sum (`vmovq` / `vpextrq` / two adds) on every
  call, even when no 4×V block fits (below 64 B in SSE2, 128 B in AVX2), where the block loop
  never runs. Clang drops that by value range; the asm cannot. Now `len < LOOP` goes straight
  to the vector loop and the overlapping tail and returns. The block path is HEAD's, with
  one compare in front. In the IR, so every backend gets it (GCC 64 B AVX2: 66 → 56
  instructions). Counters, best of 7 to 9, HEAD / new / HEAD again:

  | count | instructions | cycles |
  |---|---|---|
  | 16 B AVX2 | 47.99 → 39.94 | 12.16 / 10.72 / 12.21 |
  | 16 B SSE2 | 53.96 → 44.01 | 12.82 / 10.80 / 12.15 |
  | 32 B SSE2 | 67.93 → 58.03 | 17.50 / 14.54 / 16.86 |
  | 64 B AVX2 | 55.97 → 47.01 | 13.40 / 16.82 / 13.74 |
  | 128 B AVX2 | 68.91 → 71.00 | 16.95 / 17.83 / 17.17 |

  The 64 B AVX2 row is slower with 9 instructions fewer, and its µops all come from the DSB
  in both versions (MITE ≈ 0). The position control (the same code with every loop head
  32-aligned) gives 11.02, so it is placement inside the function: logged in the
  position-loss table. From 128 B on, the block path is +2 instructions (the compare), and
  the cycles stay inside HEAD's own spread (15.8-17.4 at 128 B between identical copies).
  An earlier form (the block, its accumulator and its sum under `if len >= LOOP`) gave
  the same small-size gains, but repeated the test at the loop's entry.

- **A tail call through a function table (item 17).** Teddy's dispatch,
  `return CallTable(...)`, was a call: `blr x9` on aarch64 and `call r8` on x86, with a
  frame kept only for that call (`stp x29, x30` / `sub rsp, 8`) and a return trip back
  through `nvol_teddy_find_raw`. `tail_call` now takes a `CallTable` too. The epilogue runs,
  then `br` / `jmp` to the target, kept in a register that is never callee-saved: on
  aarch64 the first argument register the call leaves free (`br x4`, Clang's code); on x86
  `rax`, computed into it directly (`movsxd rax, ...; add rax, r9; jmp rax`). The function is
  frameless now on all three ABIs, so Win64 has no epilogue for its unwinder to recognize.
  Counters, best of 7, HEAD / new / HEAD again:

  | teddy | instructions | cycles |
  |---|---|---|
  | 16 B AVX2 | 415.09 → 412.95 | 99.02 / 97.94 / 99.14 |
  | 64 B AVX2 | 141.00 → 138.06 | 37.12 / 35.99 / 37.05 |
  | 64 B SSSE3 | 233.00 → 230.04 | 57.00 / 55.86 / 56.90 |
  | 256 B AVX2 | 302.63 → 299.79 | 95.80 / 94.31 / 97.20 |

  −3 instructions and about −1.1 cycles per call on every row.

- **A select instead of a branch for `return c ? x : y` (item 17).** The public index
  wrappers end in `if p == NULL: return NONE; return p - start`. Lower now recognizes an `If`
  whose only statement returns, followed by a `Return`, outside loops, with cheap values
  (constants, variables, a +/- or pointer difference of two) and a plain comparison. It
  computes both values and asks the target for a select:
  - aarch64: `csel`, or `csinv d, y, xzr, cc` when x is all ones: `sub x9, x0, x19;
    cmp x0, #0; csinv x0, x9, xzr, ne`, Clang's code exactly;
  - x86: `cmovCC`. When x is a non-zero constant, GCC's order: y aside, the compare, then the
    constant straight into the result (a `mov`, which keeps the flags), then `cmovne` from y.
    `nvol_memchr` is now GCC's code exactly.

  7 wrappers on each target (memchr family, `first_non_ascii`). aarch64 static count,
  together with the extension change before it: every wrapper 11 instructions against
  Clang's 12; total 8029 → 8008 (+12.5 % → +12.2 % against Clang). x86 counters (best of 7,
  HEAD / new / HEAD again): one more instruction per call (both values computed), and about
  one cycle less on most rows, e.g. memchr 16 B AVX2 17.01 / 15.77 / 17.07, memchr 0 B
  10.77 / 9.90 / 10.91, first_non_ascii 16 B SSE2 17.56 / 16.05 / 17.47.
  **Trade-off:** memchr2 and memchr3 64 B AVX2 are about one cycle worse (15.4-16.0 → 16.2-16.8,
  17.6-17.8 → 18.8-19.2, three rounds). The control moves the wrapper 0, 16, 32 and 48 bytes
  into its line, and the rows do not change (16.4-16.9, 18.6-19.3). So it is the code: the
  `cmov` puts the select's latency on the result, which a predicted branch hides. Accepted:
  the iterators call the raw functions, which have no select, so only a single call pays it.

- **A narrow parameter that is only passed on is not zero-extended (item 17).** Every value
  lives zero-extended, so a function extends its u8/bool/u32 parameters at entry (the ABI
  leaves the upper bits undefined). The public wrappers (`nvol_memchr` …) only hand their
  needles to the raw function, whose own entry extends them again: `and w0, w0, #255` on
  aarch64, `movzx edi, dil` on x86, twice per call. `Lower.forwarded_only` now skips the
  extension when every read of the parameter is an argument of a called (not inlined)
  function, at a parameter of the same type. Removed: 14 in the System V library and the
  matching ones in Win64 and aarch64. The ABI test (garbage in the upper bits through the
  trampoline) catches a seeded `forwarded_only = True`: 9 failures. Counters (best of 7,
  WSL; HEAD, then a second copy of HEAD as the noise control):

  | row | instructions | cycles (HEAD / new / HEAD again) |
  |---|---|---|
  | memchr 16 B AVX2 | 47.98 → 46.99 | 17.03 / 17.48 / 16.46 |
  | memchr2 16 B AVX2 | 56.08 → 54.00 | 16.54 / 15.80 / 15.95 |
  | memchr3 16 B AVX2 | 62.97 → 59.95 | 19.14 / 18.97 / 18.19 |
  | memchr3 64 B AVX2 | 78.04 → 74.91 | 19.03 / 17.93 / 18.12 |

  One instruction per needle and call; the cycles stay inside the noise control's spread on
  every row (24 rows: memchr, memrchr, memchr2/3, count, first_non_ascii at 16 and 64 B).

- **aarch64: the frame in the first save's writeback (item 17).** `a64.frame` opened with
  `sub sp, sp, #N` + `stp x29, x30, [sp]` and closed with `ldp` + `add sp`. Now, when the
  first save sits at `[sp]` and N fits (the pair form reaches -512..504, the single `str`
  -256..255), the save is pre-indexed and its load post-indexed, as Clang does:
  `stp x29, x30, [sp, #-32]!` … `ldp x29, x30, [sp], #32`. The CFI steps are unchanged. A
  pre/post-indexed access stays `str`/`ldr` (the unscaled `stur` has no writeback; GNU as
  refused it). 22 of the 23 frames fold. Static count against Clang -O2 (`a64_static.py`, same
  Clang output): 8051 → 8029 (+12.8 % → +12.5 %); every wrapper −2 (`nvol_memchr` 15 → 13,
  Clang 12; `nvol_first_non_ascii` 14 → 12, equal to Clang), memmem −5, Teddy −3.

- **NASM's ELF output without local-label symbols (item 17).** NASM writes every label into
  the ELF symbol table, and has no option to keep one out (`equ` and `..@` labels are written
  too). A `.L36` after `L.nvol_x:` became the local symbol `L.nvol_x.L36`, and so did the
  `.cfi_end` end labels: 6134 local symbols. perf charged a function's samples to them:
  memmem 4 KiB AVX2 showed 59 % in `L.nvol_memmem_find_entry_avx2.L105` and 20 % in `.L108`.
  Now, in ELF only, the printer writes a jump target as the function's alias plus its offset
  (`jae strict short L.nvol_memchr_sse2 + 227`), from encode's own lengths. The label stays
  as a comment. The function's `size` and the FDE's range are numbers from `code_size`.
  NASM resolves the expression in its own section, so the object is the same: `.text`,
  `.eh_frame`, `.rodata` and `.data` byte-identical, 540 relocations before and after, and
  6134 → 275 local symbols (the `L.name` aliases, the CIE, the tables). perf now gives
  82.8 % to `nvol_memmem_find_entry_avx2`. COFF (Win64) is unchanged.

- **`first_non_ascii` inline for 16-31 bytes in Rust (item 29), accepted with a trade-off.**
  The Rust wrapper (`nvol/backends/rust.py`) checks 16-31 bytes on x86_64 with inline SSE2
  (the baseline): the first and the last 16 bytes, `movemask` each. That skips the dispatch's
  indirect call and the AVX2 entry's `vzeroupper`, which bstr's inlined static SSE2 does not
  pay. Other sizes and architectures go through the dispatch as before. Cycles (`nacount`,
  best of 7, 3 rounds):

  | size | 8 B | 16 B | 20-31 B | 32 B | 64 B | 256 B | 4 KiB |
  |---|---|---|---|---|---|---|---|
  | before | 17.0-17.4 | 11.0 | 12.0 | 12.0-12.2 | 12.7-13.1 | 18.0-19.1 | 123 |
  | after | 16.7-16.9 | **4.0** | **4.0** | 14.0-14.1 | 14.0 | 21.9-22.4 | 100 |
  | bstr | 19.0 | 9.0 | 19.8-32.9 | 10.0 | 12.6 | 22.9-23.0 | — |

  **The trade-off:** 16-31 B −65 %, against 32 B +17 % (40 → 48 instructions, so at least
  partly code) and 64-256 B +8…+20 %. At 64 B the instructions are unchanged, so that row is
  position (Rust's LTO lays out the whole binary anew). 4 KiB moves the other way (−19 %)
  with the same code. Two other forms were measured and are worse: the short path as a
  `#[cold]` function (no gain: 11 cycles at 16-31 B) and as an `#[inline(never)]` function
  (9 cycles, but +25…+33 % from 32 B). The maintainer accepted the inline one.

- **A 2-byte memmem in the asm (item 32).** With a 2-byte needle, the packed pair's two
  offsets are 0 and 1, so every candidate is a match. `nvol_memmem_pp2_<isa>` (`packed_pair(...,
  exact2=True)`) returns the first candidate with no verification. A return inside the scan
  is lowered as a cold exit, so each 16- or 32-byte chunk takes one branch instead of two
  (`je` over the verification loop, then the latch), as ugrep's `chars<2>` does. The entry
  routes `nlen == 2` to it.
  - **Asm-only:** `Function.asm_only` and `If.asm_only` (`nvol/ir.py`), dropped by
    `ir.for_languages` in the C, C++, Rust, Odin and Python backends, so their generated code
    does not change. In C, GCC already gets the general loop close (memmem2 4 KiB SSE2 664),
    and inlined next to it Clang compiled both worse (the 8-byte needle +5…+19 %, 1240 → 1368
    instructions). Called out of line, GCC paid the call at 64 B (+13…+16 %). The interpreter
    runs the asm-only `if`, so `nvol check` tests `pp2` against the oracle.
  - `bench.c` gains `memmem2` (needle "zq", ugrep's `-c zq`).
  - Cycles (asm, 3 rounds). memmem with the 8-byte needle, whose kernel did not change, is the
    position control in the same binary:

    | asm | memmem2 before → after | memmem 8 B before → after |
    |---|---|---|
    | 256 B SSE2 | 73.9-74.4 → 63.5-63.9 (−14 %) | 73.9-74.0 → 73.0-73.5 |
    | 4 KiB SSE2 | 811.7-820.1 → 641.2-647.4 (−21 %) | 811.8-816.9 → 812.6-819.3 |
    | 64 KiB SSE2 | 12385-12479 → 10054-10147 (−18 %) | 12382-12520 → 12353-12474 |
    | 4 KiB AVX2 | 558.4-562.9 → 395.3-404.8 | 560.9-562.3 → 429.9-437.0 |
    | 64 KiB AVX2 | 8154-8383 → 6812-6828 | 8214-8526 → 7122-7251 |

    In SSE2 the gain is all code. In AVX2 the new layout also helps the 8-byte needle (−22 %),
    so about 8 % of memmem2's −28 % is code. No row is worse. The asm now beats GCC's C on the
    2-byte needle (4 KiB SSE2 641-647 against 664).
  - Red check: returning one byte too far gives 12/480 oracle mismatches per architecture and
    12 failed asm tests.

- **memrchr's long loop, exit-first (item 25), accepted with a trade-off.** At 4 KiB AVX2 the
  asm was ~28 cycles behind Clang. The cause is a misprediction at the exit of the unrolled
  reverse loop (~31 iterations): NVOL's rotated shape (`test; jne` to the match, `cmp; jae`
  back) mispredicts it once per call, and Clang's (`cmp; jb` out at the top, `test; je` back)
  does not.
  - `While.exit_first` (`nvol/ir.py`) is a guard that only NVOL's lowering honors. The C,
    Rust and Odin backends and the interpreter ignore it, and their generated code does not
    change. When the guard holds and the body ends in an `if c: ... return`, `Lower` first
    runs a copy in Clang's shape: the exit test at the top, the body, the back edge on `not
    c`, and the match as the fallthrough. Then the rotated loop finishes. The copy is a cold
    region (after the ret, its branch inverted), so a short call falls through.
  - The reverse memchr kernels set the guard on x86 at 32 iterations for AVX2 (4 KiB) and 16
    for SSE2 (1 KiB).
  - Cycles (asm, 3 rounds): memrchr 4 KiB AVX2 148-157 → 125-136 (−12…−18 %; Clang
    117-128). 8 KiB −1…+5 %, 64 KiB −1…+2 %. SSE2 unchanged (±3 %).
  - **The trade-off:** 256 B AVX2 20.0-21.0 → 23.7-24.9 and 512 B 25.6-25.9 → 29.2-29.5.
    Two controls split it: a 10-byte NOP at the guard's place (pure shift) gives +0.9…+1.6
    cycles, and the guard's two instructions +0.4…+0.9 more (≈ 3 %, the real cost of the
    length check). The rest is the copy's own shift of what follows it. Below 128 B the
    check is never reached. The maintainer accepted it: ~15 % on long reverse searches for
    ~3 % on 256-512 B ones.

- **Teddy over short haystacks: the Rabin-Karp (item 27, former 1.2a).** Below Teddy's minimum
  length, `nvol_teddy_slow` tried every pattern at every position. It is now aho-corasick
  1.1.5's packed Rabin-Karp (`packed/rabinkarp.rs`):
  - `nvol_teddy_init` hashes each pattern's first `minlen` bytes (h = 2h + byte, wrapping),
    and sets its bit in one of 64 buckets (`OFF_RK`, one u64 mask each). It also stores the
    hash (`OFF_RKH`). `STATE_BYTES` goes from 1424 to 2448;
  - the search rolls the hash, scans its bucket from the lowest pattern id (insertion order,
    so leftmost-first), and compares the stored hash before the bytes, as aho-corasick does.

  Red check: comparing against `h + 1` gives 38/544 mismatches on x86 and 27/224 on aarch64
  in `nvol check`. Counters (Rust, `tedcount`, cycles per call, before → after, aho):

  | case | 8 B | 16 B | 18 B |
  |---|---|---|---|
  | 1 (8 × 3) | 217 → 47 (59) | 520 → 89 (111) | 27 → 26 (30) |
  | 4 (32 × 5) | 687 → 39 (39) | 2068 → 78 (77) | 2414 → 88 (88) |
  | 5 (40 × 3) | 1113 → 54 (67) | 2613 → 100 (123) | 50 → 50 (69) |
  | 6 (64 × 6) | 935 → 47 (44) | 3351 → 84 (81) | 3953 → 101 (109) |

  The asm (`bench teddy`, both paths) goes from 307 to 55 cycles at 8 B and from 767 to
  100 at 16 B. Instructions are equal or below aho in all 24 rows.

- **One load per vector in the asm (item 25, pair AVX2).** The port counters showed the
  pair loop putting twice the µops on the load ports. Two causes, one defect:
  - `Lower.gen` left foldable vector loads out of its per-statement CSE, on the idea that
    repeating them is free. But each folded operand is still a load µop: `vpcmpeqb ymm1,
    ymm15, [r14 + r12]` against three needles is three loads. Now they are shared, and a load
    with one use still folds (`_fold_loads`);
  - `_member` (`nvol/kernels/byteset.py`) used the loaded chunk in two statements (the low
    and high nibble lookups), so every backend saw two loads. It now binds the chunk with
    a `let` (the C, C++, Rust and Odin get one `loadu` instead of two).

  Static count on x86 System V: 41844 → 42021 instructions, and 8150 → 7610 with a memory
  operand. Only the pair AVX2 functions change. Counters (asm, best of 5): pair AVX2 at 64 B
  −3.9 %, 256 B −8.1 %, 4 KiB −16.4 % (550 → 460 cycles; GCC 467, Clang 490), 64 KiB −3.4 %.
  Path 0 of `pair` in `bench` is the scalar kernel and does not change. GCC and Clang give the same
  instructions in byteset, byteset_not and pair, and cycles within ±2 %. Every other asm row
  is unchanged code at unchanged addresses. Its swings, for example memchr 4 KiB AVX2 at
  +8.5, −1.1 and −9.6 % in three repeats, are that row's noise.

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
