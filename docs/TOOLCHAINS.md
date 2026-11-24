# Toolchain log

Problems that NVOL found in a compiler, an assembler, a C library or a build tool: what
happens, the version, how small a case shows it, what NVOL does about it, and whether it was
reported upstream. NVOL works around each one in the generator or the harness, so none of them
blocks a build. They are recorded because the workaround is a cost, and it can be removed once
the tool is fixed.

Filing an issue upstream is the maintainer's decision. "Not reported" below means nobody filed
it yet, not that it should not be. On 2026-11-22 every Odin, LLVM and NASM row was checked
against the projects' current sources (Odin `9b8e03e4`, llvm-project `12269c838`, NASM
`3.02-50`), their documentation and their trackers. A row is filed only when it reproduces
there and is not a documented decision; the last column says which, and why.

| # | tool | version | problem | NVOL's workaround | reported |
|---|---|---|---|---|---|
| T1 | LLVM (Odin's backend) | 20 | the `or` of compare masks folded to `i1`: a `vpsllw $7` per iteration | the `mask_or` primitive (0x00/0xFF lanes), lowered by Odin as `simd_max` | no: not reproduced on 2026-11-22 (the same kernel without `mask_or`, Odin on LLVM 20 and 22, and the reduced IR on LLVM 22.1.8 and 23.1.5 all emit `vpor` with no `vpsllw`); nothing to file |
| T2 | LLVM (Odin's backend) | 20 | LSR keeps two induction variables over an aligned pointer tied to `start` | the Odin backend builds the aligned pointer as `(addr(p) \| (V-1)) + 1` | no: LLVM is right given Odin's IR (no `inbounds`), so it belongs to T3, not to LLVM |
| T3 | Odin | dev-2026-10 | GEPs without `inbounds`, `icmp sle` on pointers, no `@(align)`, no way to pass LLVM options | the T2 rewrite; nothing for the rest | **yes, declined**: the signed comparisons, [odin-lang/Odin#8551](https://github.com/odin-lang/Odin/issues/8551), fix in [#8552](https://github.com/odin-lang/Odin/pull/8552), both closed unmerged on 2026-11-24 as not a bug (see T3's notes); `inbounds` not filed (Odin allows arbitrary pointer arithmetic, so it may be by design) |
| T4 | GCC | Alpine's, at the time of PENDING's rapidjson items | the inlining budget: four inline comparisons in rapidjson's per-character loop push other code out of line | the kernel call kept, the third byte checked before calling | no (a heuristic, not a bug) |
| T5 | GCC | Alpine's, at the time of PENDING's rapidjson items | an opaque call forces callee-saved registers on every path of the caller | glue written so the call is the only thing on its path | no (by design) |
| T6 | musl / glibc | fortify headers | `__builtin_memcpy` in a `load64` keeps the overlap check of the fortified `memcpy` (ugrep: 1379 → 805 lines of assembly once avoided) | the C prelude loads through `__builtin_memcpy` with no fortified wrapper in reach | no (a property of fortify) |
| T7 | MSVC | Visual Studio 18 | C4141 (`inline` repeated) with `/WX` on `NVOL_FORCE_INLINE inline` | `NVOL_INLINE` for kernels with an ISA attribute | no (a warning, correct) |
| T8 | NASM | 2.16.01 | with `-Ox` (the default) a `near` jump without `strict` can come out short | the printer writes `strict near` / `strict short` from `encode.relax` | fixed upstream: NASM 3.02's changelog makes `JMP NEAR` the same as `JMP STRICT NEAR` |
| T9 | NASM | 2.16.01 (not on 3.01, 3.02) | two jumps that depend on each other stay long (`e9`, `0f 8x`) when the short form fits | `encode.relax` decides every size; the printer writes it | no; not reproduced on current versions (below) |
| T10 | NASM | 2.16.01 | `align` in a data section pads with `0x90` | `align N, db 0` | no: documented (the manual: the default fill of `ALIGN` is `NOP`) |
| T11 | NASM, ml64 | 2.16.01; Visual Studio 18 | no way to ask for the 2-byte-VEX store form of `vmovdqa`/`vmovdqu` ymm8-15 → ymm0-7 (GNU as picks it) | the printers write those bytes with `db` | **yes**, for `{vex2}`, which the manual says encodes "if possible" with a 2-byte VEX: [netwide-assembler/nasm#349](https://github.com/netwide-assembler/nasm/issues/349), fix in [#350](https://github.com/netwide-assembler/nasm/pull/350); ml64 is closed source, not filed |
| T12 | GNU as (COFF) | 2.42 | `.drectve` written as data (`0xc0300040`), not `IMAGE_SCN_LNK_INFO`: `link.exe` ignores its `-export:` entries | `harness/asm/build_msvc.py` reads the section and passes a `.def` | no |
| T13 | ml64 | Visual Studio 18 | `_TEXT` is predefined 16-aligned and a redeclaration cannot change it (A2015) | the code goes in `_NVOLT`, aliased to `.text`, `ALIGN(64)` | no (by design) |
| T14 | NASM | 3.01 | every label goes into the ELF symbol table (`.L36` becomes `L.fn.L36`; `equ` and `..@` too), and perf charges samples to them | jump targets written as `L.fn + offset` from encode's lengths | no: by design (`elf_deflabel` keeps every label but NASM's `..` specials, and lets `..@` through on purpose; dropping them would change what debuggers and profilers show) |

## Details

### T3. Odin: pointer comparisons and `inbounds`

Odin emits `icmp sle ptr` for `a <= b` between multi-pointers (a signed comparison, `jle` in
the asm), and every pointer step (`p[n:]`, `&p[n]`, `intrinsics.ptr_offset`) as a
`getelementptr` without `inbounds`. Without `inbounds`, LLVM cannot assume that the pointer
stays inside one object, which is what let LSR tie an aligned pointer to `start` (T2). The
signed comparison does not matter in user space on x86-64 and aarch64 (addresses below 2^63);
in kernel space it would. Odin also has no `@(align)` for procedures, and no way to pass an LLVM
option such as `-x86-branches-within-32B-boundaries` (the JCC erratum mitigation).

**Filed on 2026-11-22** as [odin-lang/Odin#8551](https://github.com/odin-lang/Odin/issues/8551)
(the signed comparisons only), with the fix in
[#8552](https://github.com/odin-lang/Odin/pull/8552): `lb_emit_comp` takes the unsigned
predicates for pointer, multi-pointer and procedure types, and `tests/internal` gains a test
that fails before it (4/4) and passes after (285/285 of the suite). On master `9b8e03e4`, two
addresses on either side of 2^63 compare wrong as `[^]u8`, `^u8` and `rawptr` and right as
`uintptr`. The first draft, which also named `inbounds`:

> **Multi-pointer comparisons are signed, and pointer arithmetic lacks `inbounds`.**
> `a <= b` on `[^]u8` lowers to `icmp sle`; `p[n:]` lowers to `getelementptr i8, ptr %p,
> i64 %n` without `inbounds`. Both lose information LLVM uses: loop strength reduction kept a
> second induction variable in a byte-search loop because it could not prove the aligned
> pointer stays in the same object. C (`clang -O2`) emits `icmp ule` and `getelementptr
> inbounds` for the same loop. Version: dev-2026-10 (84bc3fc), LLVM 20.

**Outcome, 2026-11-24.** The maintainer closed #8552 unmerged and #8551 with GitHub's default
"completed" reason, which only means closed, not fixed. His one comment: pointers are signed on
AMD64, so #8551 is not a bug, and the reason `uintptr` exists and not `intptr` is more complex
than that. He gave no more detail.

What the report got right and what it got wrong:

- Right: the facts. Odin lowers `<` on pointers to `icmp slt/sle/...`, the same comparison on
  `uintptr` is unsigned, `clang -O2` emits `icmp ule` for the C loop, and the test reproduced
  it on master.
- Wrong: calling it a bug. On AMD64 a canonical address is sign-extended from bit 47, so the
  upper half (kernel space) reads as negative, and ordering pointers as signed integers is a
  coherent model. It is a design choice, not an accident. The audit rule asked to check for a
  deliberate design before filing. Finding nothing in Odin's documentation was taken as
  enough, and it was not.
- Neither false data nor a careless reviewer: a disagreement about semantics, filed as a defect
  with a ready fix.

NVOL keeps no workaround: user-space addresses stay below 2^63 on x86-64 and aarch64.

**Policy from 2026-11-24 (the user's decision).** No new issue or pull request to another
project until NVOL is mature. Findings are documented here and in the project that found them,
with their evidence, and reported later, when NVOL's maturity backs them. A finding about
semantics (what a construct should mean, not what it does) is to be raised first as a
question to the project, never as a fix. The open NASM reports (T11: #349 and #350, still open
on 2026-11-24, no comments) stay as they are.

### T9. NASM: two jumps that depend on each other stay long

The case: the entry of a rotated loop, a `jmp` forward to the test and a `jb` back to the
head, with displacements from 0x7c to 0x7f. Each jump's size depends on the other's, and NASM
leaves both long, with `-Ox` and with `-O9999`. MASM and GNU as shrink both. NASM does shrink a
lone jump with the same displacement. NVOL is not affected: `encode.relax` computes every
size and the printer writes `strict short` or `strict near`.

**On current NASM it does not reproduce (2026-11-22).** Two shapes were swept on NASM 3.01
(Alpine, the one `verify.sh` uses) and 3.02 (Arch), with `-Ox`:

- the rotated-loop entry: `jmp test` over K bytes, then `cmp` and `jb` back to the head.
  Both jumps are short up to the last K where `jb`'s −128 still fits, and `jb` alone is long
  after it;
- each jump inside the other's span (`C: 2 nops; A: jmp B; K nops; jb C; 2 nops; B: ret`). Both
  are short up to K = 122, the last K where both short forms are consistent, and both are long
  from K = 123, where `jb` needs 129 bytes back.

So whatever 2.16.01 did, 3.01 does not, for these shapes. The sweep, to rerun on a new NASM:

```sh
for K in 118 119 120 121 122 123 124; do
    printf 'bits 64
C:
 times 2 nop
A:
 jmp B
 times %d nop
 jb C
 times 2 nop
B:
 ret
' $K > b.asm
    nasm -Ox -f bin b.asm -o b.bin && echo "K=$K $(stat -c %s b.bin) bytes (both short: $((K + 9)))"
done
```

NVOL keeps deciding the sizes itself: the printers must give the same bytes as `encode`'s
lengths (jump targets are offsets from those lengths, T14), whatever the assembler would pick.
