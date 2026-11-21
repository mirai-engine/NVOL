# The NVOL assembler against GCC, Clang and MSVC (item 15)

Date: 2026-11-20. Machine: i5-7400, WSL2 Alpine (musl), GCC 15.2 and Clang 22.1 with `-O2`, NASM
3.01. Measurement: `harness/asm/counters.sh` (`perf stat`, the count of N calls minus the count of
zero calls, divided by N). Instruction counts are exact. Cycles are the best of 3 runs, and in WSL
they vary from run to run (the same memrchr AVX2 row at 4 KiB gave 125 and 141 cycles). Here they
give direction and do not decide.

Each call walks the whole buffer without finding anything. The test hook forces the CPU path.
"Before" is the asm at the start of the item. "asm" is the asm of this commit. The two C columns
are NVOL's C, compiled. The gap is against the better of the two.

## What changed in the backend

- **Load folding:** a vector load used only once, by the next operation, becomes that
  operation's memory operand (`vpcmpeqb ymm1, ymm0, [rdx]`), as GCC and Clang emit. In SSE, only
  aligned loads fold.
- **`While` condition invariants:** `end - 128` is computed once, before the loop.
- **Pointer alignment arithmetic:** `p + (V - (addr(p) & (V-1)))` lowers to `(p & -V) + V`, and
  `p - (addr(p) & m)` lowers to `p & ~m`.
- **`popcnt` without the instruction** (SSE2/SSSE3): a 256-byte table, as in libgcc (7
  instructions for a 16-bit mask, against 16 for SWAR).
- **32-bit comparisons** for values of 32 bits or less (one byte shorter).
- **Alignment:** functions to 16. Loop heads to 16 if it costs up to 10 bytes, otherwise to 8,
  as GCC does. NVOL writes the NOPs itself, so the three syntaxes still produce the same bytes.
- **Out-of-line loop exits:** a kernel's `if ... return` is lowered in place. After register
  allocation, it moves before the loop head with the branch inverted. The loop is left with a
  single taken branch per iteration.
- **Sized symbols in ELF**, so that `perf report` and callgrind attribute the samples.

## Result (instructions and cycles per call)

| operation | bytes | path | before | asm | GCC | Clang | asm vs best C | asm cycles | best C cycles |
|---|---|---|---|---|---|---|---|---|---|
| byteset | 64 | SSE2/SSSE3 | 162 | 154 | 121 | 134 | +27.3 % | 38 | 29 |
| byteset | 64 | AVX2 | 93 | 89 | 72 | 70 | +27.1 % | 23 | 17 |
| byteset | 4096 | SSE2/SSSE3 | 4952 | 4439 | 4405 | 4670 | +0.8 % | 1026 | 1017 |
| byteset | 4096 | AVX2 | 1605 | 1350 | 1332 | 1456 | +1.3 % | 366 | 321 |
| byteset_not | 64 | SSE2/SSSE3 | 133 | 129 | 127 | 135 | +1.6 % | 32 | 32 |
| byteset_not | 64 | AVX2 | 67 | 65 | 62 | 63 | +4.8 % | 25 | 16 |
| byteset_not | 4096 | SSE2/SSSE3 | 4920 | 4665 | 4664 | 4924 | +0.0 % | 1074 | 1068 |
| byteset_not | 4096 | AVX2 | 1579 | 1451 | 1448 | 1450 | +0.2 % | 384 | 379 |
| count | 64 | SSE2/SSSE3 | 79 | 79 | 92 | 67 | +17.9 % | 23 | 17 |
| count | 64 | AVX2 | 67 | 63 | 66 | 45 | +40.0 % | 27 | 17 |
| count | 4096 | SSE2/SSSE3 | 1105 | 1104 | 1121 | 1029 | +7.4 % | 266 | 262 |
| count | 4096 | AVX2 | 579 | 449 | 449 | 407 | +10.3 % | 134 | 102 |
| count_parity | 64 | SSE2/SSSE3 | 222 | 190 | 223 | 203 | -6.4 % | 68 | 56 |
| count_parity | 64 | AVX2 | 254 | 251 | 113 | 115 | +122.1 % | 93 | 29 |
| count_parity | 4096 | SSE2/SSSE3 | 5516 | 3151 | 7034 | 2790 | +12.9 % | 790 | 961 |
| count_parity | 4096 | AVX2 | 1021 | 859 | 727 | 720 | +19.2 % | 330 | 218 |
| first_non_ascii | 64 | SSE2/SSSE3 | 77 | 71 | 64 | 64 | +10.9 % | 28 | 26 |
| first_non_ascii | 64 | AVX2 | 59 | 56 | 50 | 51 | +12.0 % | 22 | 17 |
| first_non_ascii | 4096 | SSE2/SSSE3 | 1087 | 1014 | 1011 | 1140 | +0.3 % | 254 | 251 |
| first_non_ascii | 4096 | AVX2 | 515 | 478 | 473 | 541 | +1.0 % | 128 | 114 |
| memchr | 64 | SSE2/SSSE3 | 87 | 81 | 76 | 80 | +6.6 % | 28 | 21 |
| memchr | 64 | AVX2 | 66 | 61 | 55 | 57 | +10.9 % | 22 | 18 |
| memchr | 4096 | SSE2/SSSE3 | 1347 | 1279 | 1273 | 1411 | +0.5 % | 304 | 281 |
| memchr | 4096 | AVX2 | 646 | 482 | 476 | 548 | +1.2 % | 142 | 141 |
| memchr2 | 64 | SSE2/SSSE3 | 100 | 95 | 89 | 92 | +6.7 % | 29 | 22 |
| memchr2 | 64 | AVX2 | 80 | 76 | 69 | 68 | +11.6 % | 22 | 17 |
| memchr2 | 4096 | SSE2/SSSE3 | 2493 | 2364 | 2357 | 2360 | +0.3 % | 596 | 540 |
| memchr2 | 4096 | AVX2 | 1091 | 1020 | 1014 | 1014 | +0.6 % | 247 | 218 |
| memchr3 | 64 | SSE2/SSSE3 | 118 | 113 | 106 | 110 | +6.6 % | 28 | 26 |
| memchr3 | 64 | AVX2 | 91 | 85 | 78 | 75 | +13.3 % | 26 | 19 |
| memchr3 | 4096 | SSE2/SSSE3 | 3268 | 3136 | 3131 | 3134 | +0.2 % | 774 | 739 |
| memchr3 | 4096 | AVX2 | 1350 | 1282 | 1274 | 1272 | +0.8 % | 366 | 291 |
| memmem | 64 | SSE2/SSSE3 | 181 | 181 | 134 | 130 | +39.2 % | 48 | 31 |
| memmem | 64 | AVX2 | 175 | 171 | 113 | 107 | +59.8 % | 49 | 29 |
| memmem | 4096 | SSE2/SSSE3 | 3207 | 3205 | 2907 | 2903 | +10.4 % | 826 | 680 |
| memmem | 4096 | AVX2 | 1686 | 1433 | 1247 | 1241 | +15.4 % | 460 | 391 |
| memrchr | 64 | SSE2/SSSE3 | 78 | 75 | 70 | 69 | +8.7 % | 20 | 18 |
| memrchr | 64 | AVX2 | 73 | 67 | 61 | 58 | +15.3 % | 24 | 16 |
| memrchr | 4096 | SSE2/SSSE3 | 1338 | 1272 | 1268 | 1328 | +0.3 % | 272 | 290 |
| memrchr | 4096 | AVX2 | 636 | 473 | 468 | 496 | +1.1 % | 141 | 88 |
| pair | 64 | SSE2/SSSE3 | 1224 | 1224 | 1038 | 1036 | +18.2 % | 350 | 247 |
| pair | 64 | AVX2 | 104 | 96 | 93 | 87 | +10.3 % | 26 | 23 |
| pair | 4096 | SSE2/SSSE3 | 77842 | 77841 | 65557 | 65557 | +18.7 % | 20587 | 15540 |
| pair | 4096 | AVX2 | 2246 | 1734 | 1983 | 2105 | -12.6 % | 554 | 462 |
| teddy | 64 | SSE2/SSSE3 | 317 | 312 | 225 | 214 | +45.8 % | 83 | 55 |
| teddy | 64 | AVX2 | 200 | 199 | 133 | 128 | +55.5 % | 60 | 34 |
| teddy | 4096 | SSE2/SSSE3 | 13172 | 12663 | 9803 | 9285 | +36.4 % | 3376 | 2299 |
| teddy | 4096 | AVX2 | 5240 | 4988 | 3410 | 3278 | +52.2 % | 1827 | 1438 |

## Reading

- **At 4 KiB, memchr, memrchr, memchr2/3, first_non_ascii and byteset (find and find_not) are
  within 2 % of the best C in instructions.** Before, they were between +6 and +39 %.
- **`pair` AVX2 executes 12.6 % fewer instructions than the C.**
- **`count_parity` SSE2 at 4 KiB:** 3151 instructions against 2790 for Clang. Clang vectorizes
  the count, a transformation of the compiler's own. In cycles the asm is 18 % lower.
- **Open gaps:**
  - `count_parity` and `count` on AVX2;
  - Teddy (+36…55 %);
  - memmem (+10…60 %);
  - `pair` on SSSE3 (+19 %);
  - small sizes (64 B: +7…40 %), where each function's entry and exit weigh most.

  They stay in item 15.

## Part two

### What changed in the backend

- **Inverted loops:** the condition is tested at entry and at the bottom, with no label in the
  middle. The entry `jmp` goes away, and the body's arithmetic leaves the flags for the test:
  `lea r, [r+k]` + `cmp r, 0` + `ja` become `add r, k` + `jne`.
- **Dead code** after an unconditional jump is removed. Loop exits go to the end of the
  function.
- **Spills:** a `mov` from or to a spilled register reads or writes the slot without scratch.
  Only the scratch registers that are needed are reserved.
- **Tail calls:** `return f(...)` with an out-of-line `f` becomes the epilogue plus `jmp f`. A
  dispatched function is called through its pointer (`call [rel ptr]`), as the inlined C wrapper
  does.
- **Common subexpressions** within a statement (repeated `load8(p+i)`), except vector loads that
  fold as a memory operand.
- **Branches on flags:** `(x & (1 << n)) != 0` becomes `bt` + `jc`. A boolean compared with a
  constant, or stored in a single-use `let`, branches on the flags of its expression
  (`vptest` + `je`).
- **128-byte loop step:** `sub r, -128` (8-bit immediate) instead of `lea r, [r+128]`. It brought
  memchr AVX2 at 4 KiB from +35 to +15 % by clock against MSVC. Loop size matters for the
  micro-op cache.
- **Tried and dropped:**
  - a spill weight by loop depth: it kept Teddy's cold verification loops in registers and
    spilled memmem's hot values;
  - the JCC erratum mitigation (`-mbranches-within-32B-boundaries`) and aligning loop heads to
    32 or 64: no consistent improvement.

### Instructions per call, all sizes (WSL, against GCC and Clang)

"before" is the asm at the start of item 15. Median gap against the best C by size: 16 B
+15.0 %, 64 B +8.8 %, 256 B +4.6 %, 4 KiB +0.8 %, 64 KiB +0.1 %. At the start, all 120 rows were
worse. Now 31 are at parity (±1 %) and 12 are below.

| operation | bytes | path | before | asm | GCC | Clang | asm vs best C |
|---|---|---|---|---|---|---|---|
| byteset | 16 | SSE2/SSSE3 | 105 | 81 | 70 | 80 | +15.7 % |
| byteset | 16 | AVX2 | 81 | 63 | 60 | 59 | +6.8 % |
| byteset | 64 | SSE2/SSSE3 | 162 | 132 | 121 | 134 | +9.0 % |
| byteset | 64 | AVX2 | 93 | 74 | 72 | 70 | +5.7 % |
| byteset | 256 | SSE2/SSSE3 | 390 | 336 | 325 | 350 | +3.4 % |
| byteset | 256 | AVX2 | 165 | 134 | 132 | 136 | +1.4 % |
| byteset | 4096 | SSE2/SSSE3 | 4952 | 4417 | 4406 | 4671 | +0.3 % |
| byteset | 4096 | AVX2 | 1605 | 1335 | 1330 | 1456 | +0.4 % |
| byteset | 65536 | SSE2/SSSE3 | 77908 | 69702 | 69691 | 73799 | +0.0 % |
| byteset | 65536 | AVX2 | 24634 | 20530 | 20536 | 22564 | -0.0 % |
| byteset_not | 16 | SSE2/SSSE3 | 76 | 76 | 73 | 78 | +4.1 % |
| byteset_not | 16 | AVX2 | 55 | 54 | 50 | 53 | +8.0 % |
| byteset_not | 64 | SSE2/SSSE3 | 133 | 130 | 127 | 135 | +2.4 % |
| byteset_not | 64 | AVX2 | 67 | 65 | 62 | 63 | +4.7 % |
| byteset_not | 256 | SSE2/SSSE3 | 361 | 346 | 343 | 363 | +0.9 % |
| byteset_not | 256 | AVX2 | 139 | 131 | 128 | 129 | +2.2 % |
| byteset_not | 4096 | SSE2/SSSE3 | 4920 | 4667 | 4665 | 4924 | +0.1 % |
| byteset_not | 4096 | AVX2 | 1579 | 1450 | 1448 | 1449 | +0.1 % |
| byteset_not | 65536 | SSE2/SSSE3 | 77878 | 73788 | 73757 | 77906 | +0.0 % |
| byteset_not | 65536 | AVX2 | 24616 | 22552 | 22570 | 22572 | -0.1 % |
| count | 16 | SSE2/SSSE3 | 71 | 56 | 90 | 59 | -5.1 % |
| count | 16 | AVX2 | 57 | 50 | 47 | 34 | +47.1 % |
| count | 64 | SSE2/SSSE3 | 79 | 72 | 92 | 67 | +7.3 % |
| count | 64 | AVX2 | 67 | 58 | 66 | 45 | +29.1 % |
| count | 256 | SSE2/SSSE3 | 127 | 117 | 140 | 112 | +4.6 % |
| count | 256 | AVX2 | 97 | 81 | 91 | 76 | +6.4 % |
| count | 4096 | SSE2/SSSE3 | 1105 | 1035 | 1122 | 1026 | +0.9 % |
| count | 4096 | AVX2 | 579 | 413 | 451 | 406 | +1.6 % |
| count | 65536 | SSE2/SSSE3 | 16753 | 15692 | 16769 | 15656 | +0.2 % |
| count | 65536 | AVX2 | 8382 | 5827 | 6376 | 5799 | +0.5 % |
| count_parity | 16 | SSE2/SSSE3 | 145 | 138 | 126 | 127 | +9.5 % |
| count_parity | 16 | AVX2 | 149 | 143 | 153 | 80 | +78.8 % |
| count_parity | 64 | SSE2/SSSE3 | 222 | 184 | 223 | 203 | -9.4 % |
| count_parity | 64 | AVX2 | 254 | 246 | 113 | 115 | +117.8 % |
| count_parity | 256 | SSE2/SSSE3 | 474 | 326 | 553 | 331 | -1.5 % |
| count_parity | 256 | AVX2 | 301 | 283 | 155 | 152 | +86.2 % |
| count_parity | 4096 | SSE2/SSSE3 | 5516 | 3148 | 7035 | 2792 | +12.7 % |
| count_parity | 4096 | AVX2 | 1021 | 854 | 726 | 723 | +18.0 % |
| count_parity | 65536 | SSE2/SSSE3 | 86193 | 48289 | 110749 | 42184 | +14.5 % |
| count_parity | 65536 | AVX2 | 12522 | 9952 | 9841 | 9845 | +1.1 % |
| first_non_ascii | 16 | SSE2/SSSE3 | 48 | 44 | 41 | 40 | +10.0 % |
| first_non_ascii | 16 | AVX2 | 52 | 49 | 41 | 41 | +19.5 % |
| first_non_ascii | 64 | SSE2/SSSE3 | 77 | 69 | 64 | 64 | +7.8 % |
| first_non_ascii | 64 | AVX2 | 59 | 55 | 50 | 51 | +10.0 % |
| first_non_ascii | 256 | SSE2/SSSE3 | 125 | 115 | 110 | 122 | +4.5 % |
| first_non_ascii | 256 | AVX2 | 94 | 86 | 82 | 90 | +4.9 % |
| first_non_ascii | 4096 | SSE2/SSSE3 | 1087 | 1013 | 1009 | 1143 | +0.4 % |
| first_non_ascii | 4096 | AVX2 | 515 | 475 | 470 | 541 | +1.1 % |
| first_non_ascii | 65536 | SSE2/SSSE3 | 16458 | 15417 | 15408 | 17490 | +0.1 % |
| first_non_ascii | 65536 | AVX2 | 7233 | 6727 | 6677 | 7765 | +0.8 % |
| memchr | 16 | SSE2/SSSE3 | 55 | 51 | 49 | 50 | +4.1 % |
| memchr | 16 | AVX2 | 58 | 54 | 46 | 49 | +17.4 % |
| memchr | 64 | SSE2/SSSE3 | 87 | 79 | 76 | 80 | +3.9 % |
| memchr | 64 | AVX2 | 66 | 59 | 55 | 57 | +7.3 % |
| memchr | 256 | SSE2/SSSE3 | 147 | 138 | 132 | 150 | +4.6 % |
| memchr | 256 | AVX2 | 107 | 90 | 85 | 98 | +6.0 % |
| memchr | 4096 | SSE2/SSSE3 | 1347 | 1277 | 1272 | 1411 | +0.4 % |
| memchr | 4096 | AVX2 | 646 | 481 | 476 | 549 | +1.1 % |
| memchr | 65536 | SSE2/SSSE3 | 20540 | 19522 | 19487 | 21578 | +0.2 % |
| memchr | 65536 | AVX2 | 9305 | 6690 | 6708 | 7734 | -0.3 % |
| memchr2 | 16 | SSE2/SSSE3 | 64 | 60 | 58 | 58 | +3.4 % |
| memchr2 | 16 | AVX2 | 65 | 62 | 53 | 56 | +17.0 % |
| memchr2 | 64 | SSE2/SSSE3 | 100 | 94 | 89 | 92 | +5.6 % |
| memchr2 | 64 | AVX2 | 80 | 74 | 69 | 68 | +8.8 % |
| memchr2 | 256 | SSE2/SSSE3 | 214 | 202 | 197 | 200 | +2.5 % |
| memchr2 | 256 | AVX2 | 128 | 119 | 113 | 114 | +5.3 % |
| memchr2 | 4096 | SSE2/SSSE3 | 2493 | 2362 | 2359 | 2360 | +0.1 % |
| memchr2 | 4096 | AVX2 | 1091 | 1020 | 1014 | 1014 | +0.6 % |
| memchr2 | 65536 | SSE2/SSSE3 | 38974 | 36925 | 36935 | 36933 | -0.0 % |
| memchr2 | 65536 | AVX2 | 16436 | 15410 | 15401 | 15428 | +0.1 % |
| memchr3 | 16 | SSE2/SSSE3 | 73 | 69 | 66 | 67 | +4.5 % |
| memchr3 | 16 | AVX2 | 74 | 69 | 60 | 61 | +15.0 % |
| memchr3 | 64 | SSE2/SSSE3 | 118 | 112 | 106 | 110 | +5.7 % |
| memchr3 | 64 | AVX2 | 91 | 83 | 78 | 75 | +10.7 % |
| memchr3 | 256 | SSE2/SSSE3 | 268 | 256 | 250 | 254 | +2.4 % |
| memchr3 | 256 | AVX2 | 151 | 141 | 134 | 133 | +6.0 % |
| memchr3 | 4096 | SSE2/SSSE3 | 3268 | 3136 | 3130 | 3135 | +0.2 % |
| memchr3 | 4096 | AVX2 | 1350 | 1283 | 1274 | 1274 | +0.8 % |
| memchr3 | 65536 | SSE2/SSSE3 | 51261 | 49223 | 49220 | 49216 | +0.0 % |
| memchr3 | 65536 | AVX2 | 20568 | 19533 | 19493 | 19497 | +0.2 % |
| memmem | 16 | SSE2/SSSE3 | 362 | 336 | 203 | 200 | +68.1 % |
| memmem | 16 | AVX2 | 370 | 343 | 206 | 201 | +70.7 % |
| memmem | 64 | SSE2/SSSE3 | 181 | 168 | 134 | 130 | +29.1 % |
| memmem | 64 | AVX2 | 175 | 157 | 113 | 107 | +46.7 % |
| memmem | 256 | SSE2/SSSE3 | 325 | 300 | 266 | 262 | +14.5 % |
| memmem | 256 | AVX2 | 247 | 211 | 167 | 161 | +31.0 % |
| memmem | 4096 | SSE2/SSSE3 | 3207 | 2941 | 2908 | 2903 | +1.3 % |
| memmem | 4096 | AVX2 | 1686 | 1290 | 1247 | 1240 | +4.0 % |
| memmem | 65536 | SSE2/SSSE3 | 49285 | 45185 | 45140 | 45151 | +0.1 % |
| memmem | 65536 | AVX2 | 24711 | 18590 | 18505 | 18515 | +0.5 % |
| memrchr | 16 | SSE2/SSSE3 | 62 | 59 | 55 | 53 | +11.3 % |
| memrchr | 16 | AVX2 | 66 | 61 | 51 | 49 | +24.5 % |
| memrchr | 64 | SSE2/SSSE3 | 78 | 73 | 70 | 69 | +5.8 % |
| memrchr | 64 | AVX2 | 73 | 66 | 61 | 58 | +13.8 % |
| memrchr | 256 | SSE2/SSSE3 | 138 | 130 | 127 | 129 | +2.3 % |
| memrchr | 256 | AVX2 | 96 | 81 | 77 | 76 | +6.3 % |
| memrchr | 4096 | SSE2/SSSE3 | 1338 | 1271 | 1268 | 1329 | +0.3 % |
| memrchr | 4096 | AVX2 | 636 | 472 | 466 | 496 | +1.1 % |
| memrchr | 65536 | SSE2/SSSE3 | 20550 | 19508 | 19508 | 20530 | -0.0 % |
| memrchr | 65536 | AVX2 | 9284 | 6702 | 6725 | 7196 | -0.3 % |
| pair | 16 | SSE2/SSSE3 | 312 | 252 | 270 | 268 | -6.0 % |
| pair | 16 | AVX2 | 351 | 276 | 279 | 280 | -1.1 % |
| pair | 64 | SSE2/SSSE3 | 1224 | 972 | 1038 | 1036 | -6.2 % |
| pair | 64 | AVX2 | 104 | 95 | 93 | 87 | +9.2 % |
| pair | 256 | SSE2/SSSE3 | 4873 | 3853 | 4110 | 4108 | -6.2 % |
| pair | 256 | AVX2 | 206 | 173 | 183 | 183 | -5.3 % |
| pair | 4096 | SSE2/SSSE3 | 77842 | 61460 | 65557 | 65556 | -6.2 % |
| pair | 4096 | AVX2 | 2246 | 1733 | 1983 | 2104 | -12.6 % |
| pair | 65536 | SSE2/SSSE3 | 1245344 | 983164 | 1048711 | 1048730 | -6.3 % |
| pair | 65536 | AVX2 | 34900 | 26692 | 30763 | 32841 | -13.2 % |
| teddy | 16 | SSE2/SSSE3 | 3523 | 3461 | 2230 | 2402 | +55.2 % |
| teddy | 16 | AVX2 | 3523 | 3461 | 2230 | 2402 | +55.2 % |
| teddy | 64 | SSE2/SSSE3 | 317 | 270 | 225 | 214 | +26.2 % |
| teddy | 64 | AVX2 | 200 | 171 | 133 | 128 | +33.6 % |
| teddy | 256 | SSE2/SSSE3 | 929 | 774 | 681 | 646 | +19.8 % |
| teddy | 256 | AVX2 | 440 | 357 | 289 | 278 | +28.4 % |
| teddy | 4096 | SSE2/SSSE3 | 13172 | 10855 | 9802 | 9287 | +16.9 % |
| teddy | 4096 | AVX2 | 5240 | 4077 | 3411 | 3278 | +24.4 % |
| teddy | 65536 | SSE2/SSSE3 | 209030 | 172138 | 155726 | 147548 | +16.7 % |
| teddy | 65536 | AVX2 | 82055 | 63601 | 53313 | 51278 | +24.0 % |

### Time per call against MSVC (Windows, `harness/asm/wtime.py`)

Windows does not give counters to a user program. The measurement is by clock: the best of 9
runs, in nanoseconds. The columns are the asm (NASM, with the glue compiled by `cl`), NVOL's C
compiled by MSVC (`cl /O2`), and the same C compiled by MinGW GCC (`-O2`). The time moves several
points from run to run.

| operation | bytes | path | asm ns | MSVC ns | GCC ns | asm vs MSVC |
|---|---|---|---|---|---|---|
| memchr | 64 | AVX2 | 5.77 | 5.47 | 5.61 | +5.5% |
| memchr | 64 | SSE2/SSSE3 | 6.38 | 5.31 | 6.56 | +20.2% |
| memchr | 4096 | AVX2 | 39.98 | 34.87 | 34.13 | +14.7% |
| memchr | 4096 | SSE2/SSSE3 | 84.62 | 86.90 | 85.22 | -2.6% |
| memrchr | 64 | AVX2 | 5.77 | 6.68 | 8.51 | -13.6% |
| memrchr | 64 | SSE2/SSSE3 | 5.77 | 5.47 | 6.08 | +5.5% |
| memrchr | 4096 | AVX2 | 45.24 | 38.69 | 35.07 | +16.9% |
| memrchr | 4096 | SSE2/SSSE3 | 92.06 | 91.82 | 87.50 | +0.3% |
| memchr2 | 64 | AVX2 | 5.77 | 6.38 | 5.77 | -9.6% |
| memchr2 | 64 | SSE2/SSSE3 | 6.84 | 6.38 | 7.60 | +7.2% |
| memchr2 | 4096 | AVX2 | 68.40 | 74.90 | 66.07 | -8.7% |
| memchr2 | 4096 | SSE2/SSSE3 | 165.62 | 165.82 | 167.76 | -0.1% |
| memchr3 | 64 | AVX2 | 7.30 | 7.60 | 7.95 | -3.9% |
| memchr3 | 64 | SSE2/SSSE3 | 8.84 | 8.41 | 10.03 | +5.1% |
| memchr3 | 4096 | AVX2 | 89.78 | 99.70 | 89.09 | -9.9% |
| memchr3 | 4096 | SSE2/SSSE3 | 224.95 | 224.60 | 226.24 | +0.2% |
| count | 64 | AVX2 | 5.17 | 7.36 | 5.31 | -29.8% |
| count | 64 | SSE2/SSSE3 | 4.86 | 5.01 | 7.08 | -3.0% |
| count | 4096 | AVX2 | 38.84 | 38.19 | 32.94 | +1.7% |
| count | 4096 | SSE2/SSSE3 | 79.56 | 77.63 | 82.44 | +2.5% |
| count_parity | 64 | AVX2 | 33.37 | 23.75 | 18.83 | +40.5% |
| count_parity | 64 | SSE2/SSSE3 | 20.44 | 17.07 | 19.08 | +19.7% |
| count_parity | 4096 | AVX2 | 83.53 | 227.08 | 78.17 | -63.2% |
| count_parity | 4096 | SSE2/SSSE3 | 237.00 | 417.66 | 640.13 | -43.3% |
| first_non_ascii | 64 | AVX2 | 4.86 | 4.25 | 4.86 | +14.4% |
| first_non_ascii | 64 | SSE2/SSSE3 | 6.69 | 6.23 | 7.29 | +7.4% |
| first_non_ascii | 4096 | AVX2 | 43.55 | 42.86 | 44.39 | +1.6% |
| first_non_ascii | 4096 | SSE2/SSSE3 | 88.69 | 88.14 | 89.63 | +0.6% |
| memmem | 64 | AVX2 | 14.50 | 10.94 | 9.42 | +32.5% |
| memmem | 64 | SSE2/SSSE3 | 15.41 | 11.22 | 10.94 | +37.3% |
| memmem | 4096 | AVX2 | 125.84 | 133.04 | 127.83 | -5.4% |
| memmem | 4096 | SSE2/SSSE3 | 247.37 | 247.67 | 244.64 | -0.1% |
| byteset | 64 | AVX2 | 6.92 | 5.90 | 7.17 | +17.3% |
| byteset | 64 | SSE2/SSSE3 | 10.33 | 9.50 | 10.63 | +8.7% |
| byteset | 4096 | AVX2 | 97.97 | 115.53 | 107.99 | -15.2% |
| byteset | 4096 | SSE2/SSSE3 | 304.41 | 304.22 | 304.71 | +0.1% |
| byteset_not | 64 | AVX2 | 5.90 | 6.08 | 6.99 | -3.0% |
| byteset_not | 64 | SSE2/SSSE3 | 10.63 | 10.33 | 10.63 | +2.9% |
| byteset_not | 4096 | AVX2 | 127.78 | 117.56 | 115.92 | +8.7% |
| byteset_not | 4096 | SSE2/SSSE3 | 323.76 | 323.61 | 323.71 | +0.0% |
| pair | 64 | AVX2 | 10.33 | 10.03 | 8.51 | +3.0% |
| pair | 64 | SSE2/SSSE3 | 77.06 | 93.23 | 85.19 | -17.3% |
| pair | 4096 | AVX2 | 163.99 | 138.29 | 162.25 | +18.6% |
| pair | 4096 | SSE2/SSSE3 | 4896.48 | 5606.30 | 4902.53 | -12.7% |
| teddy | 64 | AVX2 | 18.50 | 14.17 | 13.55 | +30.6% |
| teddy | 64 | SSE2/SSSE3 | 26.98 | 18.97 | 19.49 | +42.2% |
| teddy | 4096 | AVX2 | 487.75 | 467.81 | 432.94 | +4.3% |
| teddy | 4096 | SSE2/SSSE3 | 878.92 | 707.54 | 708.33 | +24.2% |

### Reading and next steps

- **At 4 KiB and 64 KiB the instructions are at parity with the best C** (median +0.8 % and
  +0.1 %). `pair` is 6-13 % below, and memchr2/3 are at parity.
- **Against MSVC by clock:**
  - equal or better in most 4 KiB rows;
  - `count_parity` −43 to −63 %: MSVC does not vectorize that count, and Clang and GCC do;
  - `pair` SSSE3 −13 %;
  - memchr and memrchr AVX2 at 4 KiB are +15 to +17 %, with the same instructions as the C.
    The cause is the frontend (loop size and placement). WSL exposes no counters to see it.
- **What remains open:**
  - **Small sizes (16-64 B):** the entry and exit of large functions (the ones that dispatch
    between inlined kernels) pay for spills of parameters and callee-saved registers. The
    allocator needs live-range splitting, because its intervals have a single segment. This
    weighs most on memmem (+29/+47 % at 64 B) and Teddy (+17/+24 % at 4 KiB). There the loop
    pointer stays on the stack because the cold verification loops compete for registers. An
    `unlikely` mark in the IR would give the allocator frequencies (and give the C
    `__builtin_expect`).
  - **`count_parity` on AVX2:** GCC and Clang vectorize the kernel's scalar head and tail loop.
    That is a compiler transformation. The kernel can be improved for all backends (head and
    tail with one load and a mask, as in `count_sad`).
  - **Cycles:** the rows with the same instructions and more cycles (memchr and memrchr AVX2)
    call for measuring on an idle machine and with more counters (native Linux).

## Part three: idle machine (2026-11-21)

The machine had no other use: 11 % load, only the Claude session. The processor is an Intel
Core i5-7400 (Kaby Lake, 4 cores without HT). In WSL the cycles are the best of 15 repetitions.
In Windows, by clock, the time is the best of 9 runs. WSL has no `taskset`, so the core was not
pinned.

### Cycles and instructions per call at 4 KiB (WSL, against the better of GCC and Clang)

Measured before the count reassociation, described below.

| operation | path | asm instr | best C instr | gap | asm cycles | best C cycles | gap |
|---|---|---|---|---|---|---|---|
| byteset | SSE2/SSSE3 | 4407 | 4406 | +0.0 % | 1005 | 1002 | +0.3 % |
| byteset | AVX2 | 1331 | 1332 | -0.1 % | 380 | 322 | +18.3 % |
| byteset_not | SSE2/SSSE3 | 4661 | 4664 | -0.1 % | 1075 | 1065 | +0.9 % |
| byteset_not | AVX2 | 1445 | 1448 | -0.2 % | 382 | 375 | +1.8 % |
| count | SSE2/SSSE3 | 1034 | 1027 | +0.6 % | 260 | 260 | -0.2 % |
| count | AVX2 | 410 | 407 | +0.9 % | 143 | 106 | +34.7 % |
| count_parity | SSE2/SSSE3 | 3145 | 2792 | +12.7 % | 801 | 945 | -15.2 % |
| count_parity | AVX2 | 851 | 722 | +18.0 % | 279 | 218 | +27.7 % |
| first_non_ascii | SSE2/SSSE3 | 1011 | 1009 | +0.2 % | 252 | 260 | -2.8 % |
| first_non_ascii | AVX2 | 472 | 472 | +0.1 % | 116 | 116 | -0.3 % |
| memchr | SSE2/SSSE3 | 1274 | 1273 | +0.1 % | 285 | 284 | +0.3 % |
| memchr | AVX2 | 478 | 475 | +0.5 % | 114 | 114 | +0.8 % |
| memchr2 | SSE2/SSSE3 | 2357 | 2357 | -0.0 % | 546 | 548 | -0.3 % |
| memchr2 | AVX2 | 1017 | 1013 | +0.4 % | 225 | 219 | +2.7 % |
| memchr3 | SSE2/SSSE3 | 3132 | 3130 | +0.1 % | 742 | 741 | +0.2 % |
| memchr3 | AVX2 | 1277 | 1273 | +0.3 % | 291 | 290 | +0.2 % |
| memmem | SSE2/SSSE3 | 2917 | 2903 | +0.5 % | 815 | 670 | +21.7 % |
| memmem | AVX2 | 1262 | 1242 | +1.6 % | 390 | 385 | +1.3 % |
| memrchr | SSE2/SSSE3 | 1265 | 1267 | -0.1 % | 287 | 285 | +0.8 % |
| memrchr | AVX2 | 468 | 467 | +0.0 % | 150 | 122 | +23.4 % |
| pair | SSE2/SSSE3 | 61459 | 65556 | -6.3 % | 16435 | 15470 | +6.2 % |
| pair | AVX2 | 1724 | 1983 | -13.1 % | 554 | 461 | +20.1 % |
| teddy | SSE2/SSSE3 | 9811 | 9287 | +5.6 % | 2370 | 2289 | +3.5 % |
| teddy | AVX2 | 3544 | 3278 | +8.1 % | 1444 | 1435 | +0.6 % |

### Time per call against MSVC (Windows)

| operation | bytes | path | asm ns | MSVC ns | GCC ns | asm vs MSVC |
|---|---|---|---|---|---|---|
| memchr | 64 | AVX2 | 4.30 | 5.31 | 5.60 | -19.0% |
| memchr | 64 | SSE2/SSSE3 | 6.78 | 5.31 | 6.19 | +27.7% |
| memchr | 4096 | AVX2 | 39.63 | 34.92 | 32.69 | +13.5% |
| memchr | 4096 | SSE2/SSSE3 | 84.33 | 84.38 | 85.22 | -0.1% |
| memrchr | 64 | AVX2 | 5.60 | 6.49 | 8.00 | -13.7% |
| memrchr | 64 | SSE2/SSSE3 | 5.60 | 5.31 | 5.90 | +5.5% |
| memrchr | 4096 | AVX2 | 43.60 | 36.95 | 34.08 | +18.0% |
| memrchr | 4096 | SSE2/SSSE3 | 84.38 | 89.04 | 84.92 | -5.2% |
| memchr2 | 64 | AVX2 | 5.01 | 6.19 | 5.60 | -19.1% |
| memchr2 | 64 | SSE2/SSSE3 | 6.49 | 6.19 | 7.37 | +4.8% |
| memchr2 | 4096 | AVX2 | 65.77 | 71.83 | 64.14 | -8.4% |
| memchr2 | 4096 | SSE2/SSSE3 | 160.76 | 160.96 | 162.20 | -0.1% |
| memchr3 | 64 | AVX2 | 6.50 | 7.42 | 7.37 | -12.4% |
| memchr3 | 64 | SSE2/SSSE3 | 8.26 | 8.16 | 9.73 | +1.2% |
| memchr3 | 4096 | AVX2 | 86.51 | 96.78 | 86.46 | -10.6% |
| memchr3 | 4096 | SSE2/SSSE3 | 218.25 | 218.06 | 219.64 | +0.1% |
| count | 64 | AVX2 | 4.13 | 7.14 | 5.31 | -42.2% |
| count | 64 | SSE2/SSSE3 | 4.77 | 5.01 | 7.08 | -4.8% |
| count | 4096 | AVX2 | 37.85 | 39.19 | 32.49 | -3.4% |
| count | 4096 | SSE2/SSSE3 | 70.78 | 75.35 | 82.44 | -6.1% |
| count_parity | 64 | AVX2 | 31.87 | 23.62 | 18.68 | +34.9% |
| count_parity | 64 | SSE2/SSSE3 | 20.00 | 16.77 | 18.48 | +19.3% |
| count_parity | 4096 | AVX2 | 80.85 | 220.24 | 75.94 | -63.3% |
| count_parity | 4096 | SSE2/SSSE3 | 230.01 | 405.36 | 604.41 | -43.3% |
| first_non_ascii | 64 | AVX2 | 4.72 | 4.13 | 4.72 | +14.3% |
| first_non_ascii | 64 | SSE2/SSSE3 | 5.90 | 5.90 | 7.08 | +0.0% |
| first_non_ascii | 4096 | AVX2 | 34.38 | 41.62 | 44.54 | -17.4% |
| first_non_ascii | 4096 | SSE2/SSSE3 | 85.42 | 85.57 | 87.05 | -0.2% |
| memmem | 64 | AVX2 | 10.99 | 10.63 | 9.42 | +3.4% |
| memmem | 64 | SSE2/SSSE3 | 11.94 | 11.24 | 10.96 | +6.2% |
| memmem | 4096 | AVX2 | 122.47 | 132.64 | 127.48 | -7.7% |
| memmem | 4096 | SSE2/SSSE3 | 206.05 | 248.66 | 246.63 | -17.1% |
| byteset | 64 | AVX2 | 5.77 | 6.13 | 7.90 | -5.9% |
| byteset | 64 | SSE2/SSSE3 | 10.04 | 9.89 | 10.66 | +1.5% |
| byteset | 4096 | AVX2 | 99.60 | 99.21 | 108.83 | +0.4% |
| byteset | 4096 | SSE2/SSSE3 | 305.01 | 305.26 | 305.01 | -0.1% |
| byteset_not | 64 | AVX2 | 5.47 | 6.08 | 6.99 | -10.0% |
| byteset_not | 64 | SSE2/SSSE3 | 10.03 | 10.33 | 10.96 | -2.9% |
| byteset_not | 4096 | AVX2 | 110.42 | 114.38 | 116.57 | -3.5% |
| byteset_not | 4096 | SSE2/SSSE3 | 313.54 | 314.98 | 314.88 | -0.5% |
| pair | 64 | AVX2 | 8.55 | 10.59 | 8.26 | -19.3% |
| pair | 64 | SSE2/SSSE3 | 73.91 | 87.66 | 80.21 | -15.7% |
| pair | 4096 | AVX2 | 162.35 | 138.49 | 162.10 | +17.2% |
| pair | 4096 | SSE2/SSSE3 | 4693.85 | 5282.64 | 4700.20 | -11.1% |
| teddy | 64 | AVX2 | 14.78 | 15.11 | 13.27 | -2.2% |
| teddy | 64 | SSE2/SSSE3 | 18.74 | 19.47 | 20.77 | -3.7% |
| teddy | 4096 | AVX2 | 429.07 | 447.37 | 424.40 | -4.1% |
| teddy | 4096 | SSE2/SSSE3 | 687.20 | 673.41 | 693.50 | +2.0% |

### Reading

- **memchr AVX2 at 4 KiB is at parity in cycles** (+0.8 %). The +15 % seen by clock was noise
  from the busy machine. In Windows it jumps between 32 and 40 ns from one link to the next (it
  depends on placement). In the best case it is below MSVC.
- **memrchr AVX2 at 4 KiB is +23 % in cycles with the same instructions**, and +15/+25 %
  against MSVC in Windows in every run. The loop takes 3.7 cycles per iteration against 3.3 for
  Clang. Measurement ruled out:
  - the JCC erratum: no branch in the loop crosses or ends on a 32-byte boundary;
  - head alignment: 16, 32 and 64 give the same result;
  - load order: reading from the old pointer and updating it afterwards changes nothing at
    4 KiB and lowers 64 KiB by 4 %.

  Isolating what remains needs frontend counters, which WSL does not expose (native Linux has
  `perf stat` with `idq.*` and `uops_issued`).
- **count AVX2:** the kernel subtracts four times in a row on the same accumulator. GCC
  reassociates it into two pairs (`acc -= a + b`), and NVOL's asm now does the same in lowering.
  It drops from 140 to 135 cycles at 4 KiB (GCC takes 110). Giving the kernel two accumulators
  for all backends was tried and dropped, because it makes the GCC-compiled C worse: SSE2 goes
  from 264 to 307 cycles.
- **Loop head alignment, with cycles and the idle machine:** no alignment wins in every row.
  32 bytes lowers byteset AVX2 by 13 % and count_parity AVX2 by 8 %, but raises memmem AVX2 by
  10 %. It stays at 16 (with up to 10 bytes of padding), as in GCC.
- **Same instructions, more cycles, not yet explained:** byteset AVX2 (+18 %; with alignment
  32 it drops to +4 %), pair AVX2 (+20 %, with 13 % fewer instructions) and memmem SSE2
  (+22 %). It is the same case as memrchr: counters are needed.
