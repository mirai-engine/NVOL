# Third-party notices

NVOL is licensed under either of [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your
option. It does not vendor third-party source files. Its kernels are rewrites, in NVOL's IR, of
the algorithms of the projects below, which keep their own licenses. Each entry says where the
derived work lives.

## Adapted code

- **memchr 2.8.3** (Unlicense or MIT; © 2015 Andrew Gallant),
  <https://github.com/BurntSushi/memchr>. NVOL uses it under the Unlicense.
  - `nvol/kernels/memchr.py`: `memchr`, `memrchr` and their 2- and 3-needle variants.
  - `nvol/kernels/count.py`: `count_raw`.
  - `nvol/kernels/memmem.py`: packed pair, Two-Way and Rabin-Karp.
  - `nvol/kernels/rank.py`: the byte frequency table, copied as is, which parity requires.
  - The generated iterators and `Finder` glue.
- **aho-corasick 1.1.5** (Unlicense or MIT; © 2015 Andrew Gallant),
  <https://github.com/BurntSushi/aho-corasick>. NVOL uses it under the Unlicense.
  - `nvol/kernels/teddy.py`: the Teddy packed searcher, with its Slim and Fat variants.
  - The generated Teddy glue.
  - aho-corasick's Teddy follows the algorithm of Intel's Hyperscan.
- **bstr 1.13** (MIT or Apache-2.0; © 2018-2019 Andrew Gallant),
  <https://github.com/BurntSushi/bstr>. `nvol/kernels/ascii.py`: the `first_non_ascii` scan.

## Patches to third-party projects

These patches build each project on top of NVOL, for the end-to-end tests. They contain lines
of the projects they patch, under those projects' licenses; nothing else of those projects is
in this repository.

- **memchr 2.8.3 and aho-corasick 1.1.5** (Unlicense or MIT; © 2015 Andrew Gallant), for
  **ripgrep** (Unlicense or MIT; © 2015 Andrew Gallant), <https://github.com/BurntSushi/ripgrep>.
  `harness/ripgrep-e2e/memchr-2.8.3.patch` and `harness/ripgrep-e2e/aho-corasick-1.1.5.patch`.
- **ugrep 7.8.5** (BSD-3-Clause; © 2023 Robert van Engelen, Genivia Inc.),
  <https://github.com/Genivia/ugrep>. `harness/ugrep-e2e/ugrep-7.8.5.patch`.
- **RapidJSON `297637b`** (MIT; © 2015 THL A29 Limited, a Tencent company, and Milo Yip),
  <https://github.com/Tencent/rapidjson>. `harness/rapidjson-e2e/rapidjson-297637b.patch`.

## Development tools

These are used to build, test or measure NVOL. None is distributed with it.

- **The compilers and assemblers** of the generated code:
  - GCC, Clang/LLVM, MSVC, rustc and Odin;
  - NASM, MASM (`ml64`) and GNU as.
- **ripgrep, ugrep and RapidJSON** are fetched and built by the end-to-end harnesses, with
  their own test suites.
- **simdjson** (Apache-2.0 or MIT), <https://github.com/simdjson/simdjson>, is not used by
  NVOL. The RapidJSON harness reads the `jsonexamples` and `tests` of a simdjson checkout that
  it is given, as a test corpus.
- **perf, valgrind and QEMU**, under WSL, for the measurements and the aarch64 tests.

## License texts

The license texts of each project are in its repository. NVOL takes memchr and aho-corasick
under the Unlicense, which asks for no notice; their copyright lines are kept here anyway. The
MIT terms of bstr and RapidJSON and the BSD-3-Clause terms of ugrep require that their copyright
notices be kept with copies of their code, which the entries above do.
