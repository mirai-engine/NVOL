# ripgrep end to end on Linux (2026-11-21)

i5-7400, WSL2 with Arch Linux (glibc, rustc 1.100.0, perf 7.3.5), idle machine. ripgrep
178cc02 (15.x) is built twice from the same checkout with `harness/ripgrep-e2e/setup.sh`:
- stock, with memchr 2.8.3 and aho-corasick 1.1.5;
- NVOL, with the same crates patched to call `generated/rust/nvol.rs`.

The builds and corpora live on the Linux filesystem (`W=~/rg-work`). The corpora are
`bench.py`'s: `en` is 256 MiB of synthetic English, and `code` is 41 MiB of Rust sources
from the cargo registry. All 14 queries print byte-identical output on both builds.

## Hardware counters (`counters.sh`, whole process, best of 7, interleaved)

Negative means NVOL uses fewer. The three queries within ±0.7 % were run in 3 rounds.

```
query                                       stock-instr     nvol-instr      stock-cyc       nvol-cyc  d-instr    d-cyc
Sherlock Holmes                               180975186      130921750       99650294       97377846  -27.66%   -2.28%
Moriarty                                      131352797       81091993       66059677       63865675  -38.26%   -3.32%
-F Z                                           36958644       36963096       58212402       57649886   +0.01%   -0.97%
Sherlock|Holmes|Watson|Irene|Adler           1438094264     1402277300      805542222      796667733   -2.49%   -1.10%
Moriarty|Lestrade|Mycroft|Hudson              299480024      291106436      148657195      149485138   -2.80%   +0.56%
-i sherlock holmes                           1250731376     1238319920      610601720      611070871   -0.99%   +0.08%
-c Watson                                     611150309      558548580      299398090      297353757   -8.61%   -0.68%
-n Adler Irene                                276202134      181249666      152811977      119664429  -34.38%  -21.69%
[A-Z]olmes Street                             223224536      174219914      119688310      118649195  -21.95%   -0.87%
fn main                                        29993431       22128600       22795932       22340845  -26.22%   -2.00%
unsafe fn|extern "C"|#[inline(always)]         60282507       58745997       38002586       38049618   -2.55%   +0.12%
-w unsafe                                      53507850       45029839       32081039       31318412  -15.84%   -2.38%
-F zqzqzqzq                                    29280221       21259829       21572760       21126150  -27.39%   -2.07%
-c e                                          270979630      259733770      114371651      112078270   -4.15%   -2.01%
```

Repeats of the three borderline rows (cycles, rounds 1-3):
- `Moriarty|Lestrade|Mycroft|Hudson`: +0.56, +0.65 and +0.54 %;
- `-i sherlock holmes`: +0.08, +0.24 and +0.31 %;
- code alternation: +0.12, −0.05 and −0.02 %.

The whole-file Teddy search (`alternation-absent`) is therefore +0.6 % in cycles, with 2.8 %
fewer instructions. Every other query is on par or better in cycles. `-n` (line numbers,
NVOL's `count`) gives −21.7 %, and the memmem queries give −2.0…−3.3 % with 16-38 % fewer
instructions.

## Share of the SIMD kernels (`perf record`, one thread, one file)

The share of each binary's cycles spent in the search kernels (memchr, memmem, Teddy and
count symbols; `nvol_*` in the NVOL build):

```
query (cycles in SIMD kernels)                stock       nvol
Sherlock Holmes                               68.1%      74.0%
Moriarty                                      82.9%      87.7%
-F Z                                          82.8%      89.0%
Moriarty|Lestrade|Mycroft|Hudson              92.4%      95.1%
-c Watson                                     63.2%      62.0%
-c e                                          38.0%      37.9%
```

On this machine the kernels account for 38-95 % of rg's cycles over a cached file. On
Windows, a ±15 % per-process noise hid that share (1.10). The share is close in both builds,
so NVOL's kernels take about the same fraction of a run that is itself a little shorter.

## Wall clock (`bench.py --rounds 7`, ms)

```
ms, median (min) of 7 interleaved rounds; nvol vs stock < 0 = NVOL faster; 'paired' = median of the per-round nvol/stock ratios
corpus query                exercises                              stock            nvol   median      min   paired
en     literal              memmem                          30.1 ( 29.8)    29.7 ( 29.3)    -1.3%    -1.5%    -2.1%
en     literal-absent       memmem, whole file              20.4 ( 20.2)    20.0 ( 19.3)    -2.0%    -4.2%    -4.2%
en     byte-absent          memchr, whole file              17.7 ( 17.6)    17.8 ( 17.5)    +0.3%    -0.3%    +0.1%
en     alternation          Teddy                          242.6 (236.9)   238.9 (233.4)    -1.5%    -1.5%    -0.9%
en     alternation-absent   Teddy, whole file               44.8 ( 44.1)    44.9 ( 44.4)    +0.3%    +0.7%    +0.4%
en     literal-i            Teddy (case variants)          182.9 (178.5)   184.1 (178.5)    +0.7%    +0.0%    +0.1%
en     count-lines          memchr count (lines)            91.2 ( 88.2)    90.4 ( 87.9)    -0.9%    -0.3%    +0.2%
en     line-numbers         memchr count (-n)               46.5 ( 45.4)    36.4 ( 35.7)   -21.7%   -21.5%   -20.8%
en     inner-literal        memmem (inner literal)          36.6 ( 36.4)    35.7 ( 35.3)    -2.5%    -3.1%    -2.8%
code   literal              memmem                           7.2 (  7.2)     7.3 (  6.9)    +1.2%    -3.9%    -0.3%
code   alternation          Teddy                           11.9 ( 11.6)    11.8 ( 11.7)    -1.5%    +0.9%    -0.7%
code   word                 memmem + word boundary          10.1 (  9.7)     9.9 (  9.7)    -1.6%    -0.8%    -0.5%
code   absent               memmem, whole file               7.0 (  6.9)     6.7 (  6.6)    -4.3%    -3.9%    -3.9%
code   count-e              memchr per line                 35.0 ( 34.1)    33.9 ( 33.3)    -3.1%    -2.6%    -2.6%
```
