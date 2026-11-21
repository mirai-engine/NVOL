# ripgrep end to end on NVOL — i5-7400 (AVX2), Windows 10

ripgrep `178cc02` (15.x), rustc 1.99.1. Each test builds ripgrep twice: once with the usual crates
and once with patched copies of memchr 2.8.3 and aho-corasick 1.1.5, in which all the SIMD that
ripgrep reaches calls `generated/rust/nvol.rs`. The ripgrep clone is not modified: the patch goes in
through `--config patch.crates-io...`.

```
RIPGREP=../ripgrep TEST=1 bash harness/ripgrep-e2e/setup.sh   # copies, patches, builds and runs the tests
python harness/ripgrep-e2e/bench.py --rounds 25
```

## What is replaced

| Patch | Entry point | Used in ripgrep by |
|---|---|---|
| `memchr-2.8.3.patch` | `memchr_raw`, `memrchr_raw`, `memchr2/3_raw`, `memrchr2/3_raw`, `count_raw` (`src/memchr.rs`) | `memchr`, `memrchr`, `memchr_iter`, `Memchr2/3`, `count`: grep-searcher (lines, `-n`, context), regex-automata 1-3 byte prefilters, bstr |
| | `memmem::Finder` (default configuration) and `memmem::find` | regex-automata single-literal prefilter, bstr |
| `aho-corasick-1.1.5.patch` | `packed::Searcher::find_in` (LeftmostFirst, default configuration) | regex-automata Teddy prefilter and the `AhoCorasick` one |

`memmem::FinderRev` (PENDING 1.1b) stays out; ripgrep does not use it in search. If a caller asks
for another configuration, such as `Prefilter::None`, a custom *ranker*, LeftmostLongest or a
forced algorithm, the original code runs. Two new tests inside the patched crates check that the
default configuration really goes through NVOL.

## Correctness

| Suite | Original | With NVOL |
|---|---|---|
| memchr 2.8.3 (`cargo test --lib`) | — | 142 + 1 new, all pass |
| aho-corasick 1.1.5 (`cargo test --lib`) | — | 163 + 1 new, all pass |
| ripgrep, whole workspace (`cargo test --workspace`) | 1199 pass, 2 fail | 1199 pass, **the same 2** fail |

The two failures (`misc::symlink_nofollow`, `regression::r1389_bad_symlinks_no_biscuit`) are
Windows symlink failures, and they fail the same way with the original crates.

`bench.py` also compares the output of the two rg builds on 14 queries over two 256 MiB corpora:
synthetic English text and concatenated Rust sources from the local registry. **All 14 outputs are
byte-for-byte identical**, up to 261 MB of output for the alternation.

## Performance

Median of 25 interleaved batches, with the order randomly alternated. "Paired" is the median of the
NVOL/original ratios of each batch; a negative value means NVOL was faster.

| corpus | query | exercises | original (ms) | NVOL (ms) | median | paired |
|---|---|---|---|---|---|---|
| en | `Sherlock Holmes` | memmem | 295.6 | 284.7 | −3.7 % | −4.8 % |
| en | `Moriarty` (absent) | memmem | 284.7 | 328.3 | +15.3 % | −0.5 % |
| en | `-F Z` (absent) | memchr | 290.8 | 291.6 | +0.3 % | −3.2 % |
| en | `Sherlock\|Holmes\|Watson\|Irene\|Adler` | Teddy | 731.5 | 765.9 | +4.7 % | −0.8 % |
| en | 4 absent names | Teddy | 308.0 | 326.4 | +6.0 % | +2.6 % |
| en | `-i 'sherlock holmes'` | Teddy | 521.4 | 574.8 | +10.2 % | +7.0 % |
| en | `-c Watson` | count | 376.2 | 374.8 | −0.4 % | +12.2 % |
| en | `-n 'Adler Irene'` | count | 344.4 | 300.8 | −12.7 % | −4.5 % |
| en | `[A-Z]olmes Street` | memmem | 282.1 | 326.4 | +15.7 % | +22.8 % |
| code | `fn main` | memmem | 304.6 | 296.1 | −2.8 % | +9.9 % |
| code | `unsafe fn\|extern "C"\|#[inline(always)]` | Teddy | 348.1 | 318.5 | −8.5 % | +4.3 % |
| code | `-w unsafe` | memmem | 303.8 | 292.7 | −3.6 % | −1.0 % |
| code | `-F zqzqzqzq` (absent) | memmem | 300.5 | 272.2 | −9.4 % | +6.4 % |
| code | `-c e` | memchr per line | 631.4 | 640.1 | +1.4 % | −3.5 % |

In an earlier 15-batch run the signs came out flipped. For example, `[A-Z]olmes Street` gave
−0.5 % and −8.6 %, and the absent alternation −14.5 %. **There is no consistent difference: at the
process level the two builds cannot be told apart.** The bench can measure; the SIMD simply
carries almost no weight in the total time:

- rg with 256 MiB in cache runs at 1-2 GB/s (`--no-mmap` 1.8, mmap 1.0-1.2). Starting the process
  alone costs ~80 ms.
- memchr, memmem and Teddy run at 6-90 GB/s
  ([xbench](2026-11-18-odin-xbench-i5-7400-windows.md)). So they scan 256 MiB in 3-40 ms, between
  ~1 % and ~10 % of the total.
- The rest is reading the file, copying it into the buffer, splitting lines and writing the output,
  all identical in both builds. At that ratio, neither the NVOL gains (count 3.5×, memmem +8-60 %)
  nor a regression of a few percent shows through the ±15 % per-process noise on Windows.

**Conclusion:** NVOL also replaces ripgrep's SIMD as a complete program: the same test suite, the
same output and the same performance within noise. Seeing the SIMD contribution in total time
needs a machine where I/O weighs less (Linux, cached files, many files in parallel) or per-function
counters (PENDING 1.10).
