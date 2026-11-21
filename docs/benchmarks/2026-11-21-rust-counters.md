# Rust and ugrep under hardware counters (2026-11-21)

i5-7400 (Kaby Lake), WSL (Alpine), `perf stat`, idle machine. Each row is (count with N
calls − count with 0 calls) / N, best of 5 (11 for ugrep). The borderline rows were run in 3
rounds, and their spread is given. NVOL is `generated/rust/nvol.rs` built into
`harness/rust` (`x86_64-unknown-linux-musl`, LTO fat), against the crates that ripgrep 15
locks: memchr 2.8.3 and aho-corasick 1.1.5. bstr is 1.13.0.

The deltas are NVOL against the reference: negative means NVOL uses fewer. In the Teddy table,
`delta` keeps `tedbench`'s sign: positive means NVOL is faster.

## One memchr per line (item 26, `linecount`)

The LineStep pattern of grep-searcher: one `memchr(b'\n')` per line over 1 MiB, 50 passes.
The values are per pass.

| line | NVOL instr | memchr instr | NVOL cycles | memchr cycles | Δ instr | Δ cycles (3 rounds) |
|---|---|---|---|---|---|---|
| 8 | 4 064 410 | 4 457 200 | 2 633 254 | 2 905 058 | −8.8 % | −9.4 % |
| 20 | 1 625 928 | 1 783 448 | 1 068 541 | 1 201 402 | −8.8 % | −11.1 % |
| 40 | 1 389 424 | 1 520 881 | 570 664 | 628 609 | −8.6 % | −9.2 % |
| 64 | 934 086 | 1 033 031 | 395 527 | 411 812 | −9.6 % | −4.0…−4.5 % |
| 100 | 629 313 | 681 680 | 276 586 | 282 634 | −7.7 % | −2.1…−2.5 % |
| 200 | 367 555 | 398 657 | 191 540 | 201 286 | −7.8 % | −4.8 % |
| 1000 | 158 496 | 163 712 | 79 618 | 76 141 | −3.2 % | +4.6, −3.2, −4.0 % |

NVOL is ahead at every line length. The one positive row (1000, round 1) did not repeat.

## memchr at 1-15 bytes (item 28, `smallcount`)

No match, so every call scans to the end; 20 M calls.

| size | NVOL instr | memchr instr | NVOL cycles | memchr cycles | Δ cycles (3 rounds) |
|---|---|---|---|---|---|
| 1 | 37 | 41 | 9.0 | 9.5 | −5.3 % |
| 2 | 42 | 46 | 10.1 | 10.5 | −3.9 % |
| 3 | 47 | 51 | 12.0 | 11.5 | +4.2…+4.4 % |
| 4 | 52 | 56 | 13.6 | 12.5 | +8.4 % |
| 5 | 57 | 61 | 17.0-17.1 | 14.3-14.8 | +15.5…+19.5 % |
| 6 | 62 | 66 | 16.1 | 14.5 | +10.5 % |
| 7 | 67 | 71 | 16.7 | 15.5 | +7.7 % |
| 8 | 72 | 76 | 18.0-18.2 | 16.5-16.6 | +8.6…+9.8 % |
| 9 | 77 | 81 | 19.7 | 17.6 | +11.9 % |
| 10 | 82 | 86 | 20.3 | 18.5 | +9.3 % |
| 11 | 87 | 91 | 21.1 | 19.6 | +7.7 % |
| 12 | 92 | 96 | 22.2-22.5 | 20.5-20.6 | +8.3…+9.4 % |
| 13 | 97 | 101 | 23.1 | 21.6 | +6.7 % |
| 14 | 102 | 106 | 23.3 | 22.6 | +3.2 % |
| 15 | 107 | 111 | 25.6-25.9 | 23.6 | +8.7…+9.6 % |

Both scan byte by byte below 16 B: 5 instructions per byte in each, and NVOL has 4 fewer per
call. From 3 B up, NVOL takes 1-2.5 more cycles per call with fewer instructions. The cause
is not in the instruction count (item 28).

## `first_non_ascii` against bstr (item 29, `nacount`)

All-ASCII haystack, so every call scans to the end.

| size | NVOL instr | bstr instr | NVOL cycles | bstr cycles | Δ cycles (3 rounds) |
|---|---|---|---|---|---|
| 16 | 38 | 39 | 11.0 | 9.0 | +22.2…+22.4 % |
| 64 | 52 | 61 | 12.6-13.0 | 12.7 | −0.4…+2.8 % |
| 256 | 83 | 108 | 18.5 | 23.1 | −20.2 % |
| 4096 | 473 | 1008 | 124.1 | 218.3 | −43.1 % |
| 65536 | 6714 | 15409 | 2362.1 | 4202.5 | −43.8 % |

bstr's public `ByteSlice::find_non_ascii_byte` returns `None` when every byte is ASCII, the
same as NVOL's `first_non_ascii`. Only its internal `ascii::first_non_ascii_byte` returns `len`.

## Teddy against aho-corasick (items 27 and 17, `tedcount`)

The 8 `tedbench` cases: 0 = 2 patterns × 6 B, 1 = 8 × 3, 2 = 16 × 1, 3 = 16 × 2, 4 = 32 × 5,
5 = 40 × 3, 6 = 64 × 6, 7 = `find_iter` with 3 names. Each call is `find`, or `find_iter`
in case 7.

| case | size | NVOL instr | aho instr | NVOL cycles | aho cycles | delta |
|---|---|---|---|---|---|---|
| 0 | 16 | 611 | 289 | 128 | 66 | −48.3 % |
| 1 | 16 | 2528 | 480 | 519 | 110 | −78.7 % |
| 2 | 16 | 82 | 92 | 21 | 23 | +11.4 % |
| 3 | 16 | 4745 | 341 | 953 | 76 | −92.0 % |
| 4 | 16 | 8223 | 352 | 2067 | 77 | −96.3 % |
| 5 | 16 | 11937 | 540 | 2610 | 123 | −95.3 % |
| 6 | 16 | 14934 | 369 | 3348 | 84 | −97.5 % |
| 7 | 16 | 820 | 308 | 173 | 68 | −60.7 % |
| 0-7 | 64 | 86-460 | 100-533 | 23-145 | 26-161 | +6.9…+27.2 % |
| 0-7 | 256 | 152-2017 | 172-2117 | 48-679 | 46-722 | −3.9…+8.0 % |
| 0-7 | 4096 | | | | | −5.0…+9.1 % |
| 0-7 | 65536 | | | | | −0.2…+11.6 % |

At 16 B NVOL is 2× to 40× slower in every case with more than one byte per pattern. The
slow path `nvol_teddy_slow` tries each pattern at each position. Below its minimum length,
aho-corasick runs its multi-pattern Rabin-Karp. From 64 B up, NVOL is ahead or on par
(case 2 at 4 KiB: −5.0 %). In instructions, NVOL is below aho in every row from 64 B up.

## ugrep (item 32, AU21, `harness/ugrep-e2e/counters.sh`)

The whole process over `en64.txt` (64 MiB), stock ugrep 7.8.5 against the same commit with
`ugrep-7.8.5.patch`, best of 11, interleaved.

| query | Δ instr | Δ cycles (3 rounds) |
|---|---|---|
| `-c zq` | −0.20…−0.22 % | +0.40, +0.80, +0.68 % |
| `-c Wat` | −0.18…+0.12 % | +0.44, +0.12, −0.05 % |
| `-c Watson` | +0.17 % | +0.65 % |
| `Sherlock Holmes` | +0.07 % | −0.02 % |
| `Moriarty` | −0.15 % | +0.05 % |

The +2.8 % that AU21 recorded by wall clock is +0.4…+0.8 % in cycles, with 0.2 % fewer
instructions.
