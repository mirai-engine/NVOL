# Odin with counters: `memchr_iter` and the rest of `xbench` (i5-7400, WSL2 Alpine)

Closes PENDING 2.1a. Measured with hardware counters instead of the clock. The same code varies
±5-10 % by clock between runs, and cycles vary less than 1 %.

## Method

`harness/odin/xcount` is the counters twin of `xbench`. It runs one case of one implementation
N times:

```
xcount odin|nvol|ref CASE SIZE N
```

- Implementations: `nvol` = NVOL-Rust; `ref` = crates `memchr` 2.8.3 and `aho-corasick` 1.1.5.
  Byteset has no reference crate, so in those cases `ref` runs NVOL-Rust again.
- Same text as `xbench`: words with a `\n` every ~80 bytes.
- It is compiled for `linux_amd64` and linked statically against the `harness/rust-ffi`
  staticlib (target `x86_64-unknown-linux-musl`) with `rust-lld`.

`xcounters.sh` measures it with `perf stat`. It subtracts the run with N = 0 and takes the
minimum of R repetitions.

```sh
bash harness/odin/xcount-build.sh                 # Windows o Linux
cd harness/odin && wsl -d Alpine -- sh xcounters.sh "memchr_iter_nl count_nl" "4096 65536"
```

## Cause of the gap

The A/B by clock gave Odin −4…−9 % on `memchr_iter`. With counters, at 4 KiB Odin executed 5 %
more instructions and 7.6 % more uops (2651 against 2528 instructions per pass) and was 7.8 %
slower. The difference was in the setup of each call to the AVX2 kernel, about 5 uops. With one
call every ~80 bytes, that is what showed.

That setup came from the IR's `trip` hint. `trip` existed to work around a problem in the LLVM 20
LSR: with the loop driven by pointer comparison, it kept two induction variables. But the
counter-driven loop forces computing the counter. On top of that, LLVM rebased `cur` on `start`:
`mov ecx,eax; and ecx,0x1f; sub rax,rcx; add rax,0x40`, while Rust does `and`/`add` only once.

The common root is the Odin compiler. It emits pointer arithmetic as `getelementptr i8`
**without `inbounds`**: no form (`p[n:]`, `&p[n]`, `intrinsics.ptr_offset`) adds it. Rust does
emit `getelementptr inbounds`. Without `inbounds`, SCEV ties the aligned pointer
`start + (32 - (start & 31))` to `start`, and the LSR rebases the loop on `start`. That is why the
two variables appeared, or the rebase when `trip` was present.

## Fix

In Odin, the backend builds the aligned pointer from the address:
`p + (V - (addr(p) & (V-1)))` becomes `(addr(p) | (V-1)) + 1`, and `p - (addr(p) & m)` becomes
`addr(p) &~ m`. The pointer is then a new base, as in Rust and C, and the loop keeps a single
induction variable without needing the counter. `trip` was removed from the IR, the interpreter
and the kernels.

Tried and dropped:

| Variant | Result |
|---|---|
| No `trip`, with the aligned pointer as a GEP | The two variables return: 14 instead of 11 uops per 128 B iteration; `memchr_absent` with 1 MiB, +15 % instructions. |
| Unsigned comparison (`uintptr(a) <= uintptr(b)`) | Changes nothing. |
| Loop with an integer index from a fixed base | Worse: indexed addressing. |
| `trip` with the aligned pointer from the address | LLVM computes the final value of `cur` in closed form and uses indexed addressing: 2697 instructions. |
| `-o:aggressive`, `-microarch:skylake` | Change nothing. |

Along the way a generator bug turned up. The `foreign _` blocks for LLVM intrinsics lacked
`default_calling_convention = "none"`, unlike the ones in `core:simd/x86`. On `linux_amd64` the
SysV ABI passes 256-bit vectors through memory, and LLVM rejected the module ("Intrinsic has
incorrect argument type"). So the generated Odin did not compile for Linux x86-64. On Windows it
worked by accident. The Odin tests now also run on `linux_amd64` (WSL), and they pass on the AVX2
and SSE2/SSSE3 paths.

## Result

`xcounters.sh` with R = 3, per call. The last column is the difference in cycles. Positive means
Odin is faster.

| case | size | Odin instr | NVOL-Rust instr | ref instr | Odin cycles | NVOL-Rust cycles | ref cycles | Odin/NVOL |
|---|---|---|---|---|---|---|---|---|
| memchr_iter_nl | 4 KiB | 2388 | 2528 | 2840 | 1147 | 1120 | 1156 | −2.4 % |
| memchr_iter_nl | 64 KiB | 36686 | 38810 | 43472 | 20114 | 20523 | 19969 | +2.0 % |
| memchr_iter_nl | 1 MiB | 571436 | 604472 | 675942 | 628471 | 651351 | 625330 | +3.6 % |
| memchr_absent | 4 KiB | 480 | 488 | 494 | 111 | 122 | 141 | +9.9 % |
| memchr_absent | 64 KiB | 6719 | 6735 | 6740 | 2542 | 2536 | 2587 | −0.2 % |
| memchr_absent | 1 MiB | 106965 | 106726 | 106190 | 62919 | 61620 | 62710 | −2.1 % |
| memchr3_absent | 4 KiB | 1281 | 1291 | 1293 | 294 | 294 | 294 | +0.0 % |
| memchr3_absent | 64 KiB | 19528 | 19550 | 19538 | 5972 | 5996 | 6217 | +0.4 % |
| memchr3_absent | 1 MiB | 311509 | 311559 | 311401 | 120774 | 115006 | 114349 | −4.8 % |
| count_nl | 4 KiB | 410 | 420 | 1177 | 149 | 138 | 556 | −7.4 % |
| count_nl | 64 KiB | 5811 | 5833 | 15582 | 2471 | 2543 | 8444 | +2.9 % |
| count_nl | 1 MiB | 92119 | 92326 | 246096 | 63047 | 65220 | 140752 | +3.4 % |
| memmem_absent | 4 KiB | 1238 | 1249 | 2148 | 412 | 411 | 629 | −0.2 % |
| memmem_absent | 64 KiB | 18526 | 18543 | 32875 | 7120 | 7417 | 9343 | +4.2 % |
| memmem_absent | 1 MiB | 295084 | 295260 | 524515 | 125741 | 130329 | 152620 | +3.6 % |
| teddy16_absent | 4 KiB | 4422 | 4434 | 4582 | 2140 | 2142 | 2145 | +0.1 % |
| teddy16_absent | 64 KiB | 69803 | 69825 | 71898 | 34019 | 33950 | 34020 | −0.2 % |
| teddy16_absent | 1 MiB | 1114556 | 1114673 | 1147225 | 544132 | 541450 | 539136 | −0.5 % |
| byteset_absent | 4 KiB | 1869 | 1867 | — | 449 | 447 | — | −0.4 % |
| byteset_absent | 64 KiB | 28757 | 28759 | — | 9099 | 9080 | — | −0.2 % |
| byteset_absent | 1 MiB | 459144 | 459152 | — | 158280 | 155048 | — | −2.0 % |
| byteset_iter_range | 4 KiB | 5027 | 5183 | — | 1584 | 1656 | — | +4.5 % |
| byteset_iter_range | 64 KiB | 77798 | 80264 | — | 41414 | 41300 | — | −0.3 % |
| byteset_iter_range | 1 MiB | 1217352 | 1255006 | — | 812657 | 794497 | — | −2.2 % |

Odin executes as many instructions as NVOL-Rust, or fewer, in every case. Differences of two
instructions out of ~1900 do not count. `memchr_iter` went from 2651 to 2388 instructions at
4 KiB and now executes fewer than Rust.

The remaining cycle differences are not code differences:

- **At 4 KiB, code placement dominates.** The Rust code is identical in every measured binary,
  and still `memchr_absent` varied between 113 and 146 cycles depending on where it landed at
  link time.
- **At 1 MiB, memory dominates.** The sign changes between runs.
- **`count_nl` at 4 KiB, −7 % in every build.** Odin has one more misprediction per call: the
  exit of the inner loop, which runs 32 identical iterations in both languages. The sign changes
  with size: +12.7 % at 3000 B, −6.8 % at 4096 B, +8.2 % at 6000 B and −7.6 % at 16 KiB. It is
  a predictor effect for certain iteration counts, not a code effect. Reversing the link order
  does not change it. At odd sizes, Odin's `popcnt` beats Rust's SWAR, which is compiled without
  that extension.

## Windows, by clock

`xbench` on Windows still gives `memchr_iter` at 64 KiB between −11 % and −13 %. With the same
code, `xbench` compiled for Linux and run in WSL gives +2.2 %. On Windows, the Odin and Rust AVX2
kernels are identical instruction by instruction (both save `xmm6`, which is callee-saved in
Win64), and so is the iterator closure. Measurement order has no effect: reversed, it gives
−11.8 %. A copy of `xbench` that runs only `memchr_iter` gives −2.2 % to −4.3 % with `-o:speed`,
ahead of the crate (10.28 against 10.09 GB/s), and −11 % with `-o:speed -microarch:skylake`.

The cause is code placement in that binary. In the `.exe`, the `cmp`+`jle` pair of the 128 B loop
ends exactly on a 32-byte boundary, the Skylake/Kaby Lake JCC erratum case. But on Linux, shifting
the code by 16, 32 and 48 bytes (with a padding object at link time) leaves that pair in the same
position, and the result does not change. So the cause is another placement detail, not that
instruction. Odin does not allow aligning functions or passing LLVM options to pin it down
(PENDING 2.1b).
