# memmem: NVOL vs memchr 2.8.3 (memchr::memmem) — i5-7400 (AVX2), Windows 10, rustc 1.99.1

> Superseded by [2026-11-18-audit](2026-11-18-audit-i5-7400-windows.md): this measurement did not
> interleave NVOL and the reference, and the bias reached ±25 % in short rows.

`cd harness/rust && cargo run --release --bin membench`: A/B, best of 7 batches. Single-pass
construction (`nvol_memmem_init`). Haystacks < 4 KiB in ns per call, the rest in GB/s. Synthetic
English text; the "present" needles appear only near the end. The 1 MiB "frequent the" and
"Sherlock" rows measure latency to the first match, not throughput.

| case                                 |   hay B |          nvol |        memchr |   delta |
|--------------------------------------|---------|---------------|---------------|---------|
| Finder::find 2B absent "zq"          |      64 |      9.3 ns   |     10.7 ns   |  +14.2% |
| Finder::find 8B present "Sherlock"   |      64 |     11.0 ns   |     12.9 ns   |  +18.2% |
| Finder::find 8B absent "Moriarty"    |      64 |      9.4 ns   |     10.9 ns   |  +16.6% |
| Finder::find 16B absent              |      64 |      9.4 ns   |     11.1 ns   |  +18.8% |
| Finder::find 32B absent              |      64 |      9.4 ns   |     11.2 ns   |  +18.4% |
| Finder::find 40B absent (two-way)    |      64 |     11.1 ns   |     12.6 ns   |  +14.1% |
| Finder::find 64B present (two-way)   |      64 |     11.1 ns   |     12.4 ns   |  +11.6% |
| Finder::find 3B frequent "the"       |      64 |      8.4 ns   |     10.3 ns   |  +23.4% |
| find_iter count "the "               |      64 |     30.1 ns   |     41.4 ns   |  +37.4% |
| one-shot find (build+search) 8B      |      64 |     31.8 ns   |     47.6 ns   |  +49.5% |
|--------------------------------------|---------|---------------|---------------|---------|
| Finder::find 2B absent "zq"          |    4096 |    34.56 GB/s |    34.69 GB/s |   -0.4% |
| Finder::find 8B present "Sherlock"   |    4096 |    21.20 GB/s |    15.23 GB/s |  +39.2% |
| Finder::find 8B absent "Moriarty"    |    4096 |    34.22 GB/s |    22.14 GB/s |  +54.6% |
| Finder::find 16B absent              |    4096 |    33.74 GB/s |    22.11 GB/s |  +52.6% |
| Finder::find 32B absent              |    4096 |    34.16 GB/s |    22.08 GB/s |  +54.7% |
| Finder::find 40B absent (two-way)    |    4096 |    32.43 GB/s |    31.29 GB/s |   +3.6% |
| Finder::find 64B present (two-way)   |    4096 |    32.10 GB/s |    32.68 GB/s |   -1.8% |
| Finder::find 3B frequent "the"       |    4096 |   486.30 GB/s |   420.48 GB/s |  +15.7% |
| find_iter count "the "               |    4096 |     7.02 GB/s |     5.15 GB/s |  +36.2% |
| one-shot find (build+search) 8B      |    4096 |    28.44 GB/s |    18.85 GB/s |  +50.8% |
|--------------------------------------|---------|---------------|---------------|---------|
| Finder::find 2B absent "zq"          |   65536 |    28.38 GB/s |    30.79 GB/s |   -7.8% |
| Finder::find 8B present "Sherlock"   |   65536 |    17.94 GB/s |    14.86 GB/s |  +20.7% |
| Finder::find 8B absent "Moriarty"    |   65536 |    28.31 GB/s |    23.45 GB/s |  +20.7% |
| Finder::find 16B absent              |   65536 |    27.78 GB/s |    23.17 GB/s |  +19.9% |
| Finder::find 32B absent              |   65536 |    33.19 GB/s |    23.12 GB/s |  +43.5% |
| Finder::find 40B absent (two-way)    |   65536 |    29.47 GB/s |    29.05 GB/s |   +1.4% |
| Finder::find 64B present (two-way)   |   65536 |    28.89 GB/s |    27.93 GB/s |   +3.4% |
| Finder::find 3B frequent "the"       |   65536 |  7456.54 GB/s |  6452.78 GB/s |  +15.6% |
| find_iter count "the "               |   65536 |     3.09 GB/s |     3.31 GB/s |   -6.6% |
| one-shot find (build+search) 8B      |   65536 |    26.96 GB/s |    22.68 GB/s |  +18.9% |
|--------------------------------------|---------|---------------|---------------|---------|
| Finder::find 2B absent "zq"          | 1048576 |    28.64 GB/s |    29.53 GB/s |   -3.0% |
| Finder::find 8B present "Sherlock"   | 1048576 |     5.90 GB/s |     5.74 GB/s |   +2.7% |
| Finder::find 8B absent "Moriarty"    | 1048576 |    29.61 GB/s |    23.67 GB/s |  +25.1% |
| Finder::find 16B absent              | 1048576 |    29.22 GB/s |    23.34 GB/s |  +25.2% |
| Finder::find 32B absent              | 1048576 |    25.85 GB/s |    22.86 GB/s |  +13.1% |
| Finder::find 40B absent (two-way)    | 1048576 |    27.66 GB/s |    28.32 GB/s |   -2.3% |
| Finder::find 64B present (two-way)   | 1048576 |    26.55 GB/s |    26.31 GB/s |   +0.9% |
| Finder::find 3B frequent "the"       | 1048576 | 111848.11 GB/s | 111848.11 GB/s |   +0.0% |
| find_iter count "the "               | 1048576 |     2.02 GB/s |     1.93 GB/s |   +4.7% |
| one-shot find (build+search) 8B      | 1048576 |    29.25 GB/s |    23.06 GB/s |  +26.9% |
|--------------------------------------|---------|---------------|---------------|---------|
| Finder::new 8B (construction only)   |       0 |     21.1 ns   |     33.2 ns   |  +57.2% |
| Finder::new 40B (construction only)  |       0 |    218.9 ns   |    217.6 ns   |   -0.6% |
| pathological a^1M b, needle 41B      | 1048577 |    29.30 GB/s |    29.17 GB/s |   +0.5% |
| pathological a^1M b, needle 60B      | 1048577 |    29.55 GB/s |    30.05 GB/s |   -1.7% |
