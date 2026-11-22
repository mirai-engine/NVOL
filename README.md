# NVOL — Native Vector Operations Library

A generator (in Python) of verified libraries of low-level SIMD operations
for **C, C++, Rust, Python and Odin**, on x86_64 and aarch64, and in **its own assembly**
(x86-64: NASM and GNU as on Win64 and System V, MASM on Win64; aarch64: GNU as, AAPCS64, NEON.
NVOL lowers the IR to instructions, with no compiler in between).

The in-house assembly is the internal reference (mature since 2026-11-22, [criterion](docs/PENDING.md)):
each language is compared against it and every difference is explained. This separates NVOL's
problems from those of each compiler or toolchain ([design](docs/DESIGN.md#principles)).

First goal: replace all the SIMD that [ripgrep](https://github.com/BurntSushi/ripgrep) uses, with
identical results and equal or better performance. This is the base for
[grepper](https://github.com/5hif7y/grepper), the 1:1 port of ripgrep to Odin.
See [docs/RIPGREP_SIMD_INVENTORY.md](docs/RIPGREP_SIMD_INVENTORY.md).

## Usage

Before each commit, run the full verification (generator; C with GCC, Clang and MSVC; C++,
Rust, Odin and assembly; native, aarch64 under QEMU and System V in WSL). It fails if any step
fails:

```sh
bash harness/verify.sh                       # SKIP="rust-a64 odin-a64" to skip steps on request
bash harness/verify.sh --changed             # only the steps the uncommitted changes reach
```

```sh
python -m nvol list                          # ISAs, kernels and primitives
python -m nvol check --cases 50              # kernels (IR) vs oracle, with UB checking
python -m nvol gen --out generated           # generates C, C++, Rust, Python, Odin (x86_64 + aarch64), NASM, MASM (x86_64) and GNU as (x86_64 + aarch64)
python -m nvol gen --lang rust --arch x86_64 --isa sse2,avx2 --kernels memchr,count
python -m pytest -q tests                    # generated C and asm through the forced paths (base, AVX2, SSE4.2) + generated/ up to date
nasm -f win64 generated/nasm/nvol_x86_64_win64.asm -o k.obj && gcc -shared -DNVOL_BUILD_DLL -o nvol.dll generated/nasm/nvol.c k.obj
nasm -f elf64 generated/nasm/nvol_x86_64_sysv.asm -o k.o && cc -shared -fPIC -o libnvol.so generated/nasm/nvol.c k.o
ml64 -c -Fo k.obj generated/masm/nvol_x86_64_win64.asm && cl /LD /DNVOL_BUILD_DLL generated/masm/nvol.c k.obj
as --64 -o k.o generated/gas/nvol_x86_64_sysv.s && cc -shared -fPIC -o libnvol.so generated/gas/nvol.c k.o
aarch64-linux-gnu-as -o k.o generated/gas/nvol_aarch64_aapcs64.s && aarch64-linux-gnu-gcc -shared -fPIC -o libnvol.so generated/gas/nvol.c k.o
wsl -d Alpine -- sh harness/asm/run-a64.sh   # aarch64 asm: GNU as = Clang bytes, smoke, test_asm + native suites under QEMU
cd harness/rust && cargo test --release      # generated Rust vs naive and vs the memchr crate
cd harness/rust && cargo run --release --bin bench
odin run harness/odin/tests -collection:nvol=generated -o:speed   # Odin: AVX2, base (SSE2/SSSE3) and SSE4.2
ODIN=odin QEMU="wsl -d Alpine -- qemu-aarch64" bash harness/odin/run-aarch64.sh
cargo build --release --manifest-path harness/rust-ffi/Cargo.toml   # Odin vs Rust in the same process:
odin run harness/odin/xbench -collection:nvol=generated -o:speed
RIPGREP=../ripgrep TEST=1 bash harness/ripgrep-e2e/setup.sh   # ripgrep built on NVOL + its tests
python harness/ripgrep-e2e/bench.py                             # identical output + timings vs original ripgrep
QEMU="wsl -d Alpine -- qemu-aarch64" bash harness/run-aarch64.sh   # NEON parity under QEMU
wsl -d Alpine -- sh harness/cpp/setup-aarch64-sysroot.sh          # once: aarch64 sysroot for the cpp-a64 and asm-a64 steps
```

Requirements: Python ≥ 3.9 (no dependencies to generate), `pytest` + GCC/Clang
for the tests, stable Rust and Odin for their harnesses; for aarch64, QEMU (WSL works) and Clang;
for the assembly, NASM and `ml64` (MSVC, which the `asm` step also uses; in WSL:
`apk add nasm gcc g++ musl-dev python3 py3-pytest`).

## Operations

x86_64: SSE2 / SSSE3 / SSE4.2 / AVX2 with runtime dispatch. aarch64: NEON.
Each generated file contains both architectures (`#if` / `cfg(target_arch)`).

| API | Parity with | Status |
|---|---|---|
| `memchr`, `memchr2`, `memchr3` | `memchr` 2.8.3 | ✅ tested + parity |
| `memrchr`, `memrchr2`, `memrchr3` | `memchr` 2.8.3 | ✅ tested + parity |
| `count` (SAD strategy) | `memchr_iter().count()` | ✅ tested, **2-3.5× faster** |
| `count_parity` | `memchr` `count_raw` | ✅ tested + parity |
| `first_non_ascii` | `bstr` 1.13 | ✅ tested |
| `memchr_iter`, `memrchr_iter` (1-3 needles, double-ended) | `memchr::Memchr{,2,3}` | ✅ tested + parity; `.count()` 2-4× faster, `memrchr_iter` ~+10 % |
| `memmem` (`Finder`, `find`, `find_iter`: packed pair SSE2/AVX2 + Two-Way + Rabin-Karp) | `memchr::memmem` | ✅ tested + parity; packed pair +13…+63 % |
| Teddy (`packed::Searcher`: Slim SSSE3/AVX2, Fat AVX2, masks 1-4) | `aho-corasick` 1.1.5 | ✅ tested + parity; construction 7-23× faster |
| Byte sets: first byte in a set or outside it (`find_not`), and a pair of sets at two distances (with a candidate mask); a single `pshufb` when the members have distinct low nibbles | ugrep 7.8.5 (`pinN_one`, `pinN_pma`); rapidjson | ✅ tested; equal to or better than ugrep's `cmpeq` chains for N = 2-16 (up to 2.9×) |
| **Full ripgrep** built on NVOL (memchr + aho-corasick patched) | ripgrep 15 (`178cc02`) | ✅ same test suite, identical output, equal performance within noise |
| **ugrep** built on NVOL (`memchr`, counting, literals and byte sets; `nvol.hpp`) | ugrep 7.8.5 | ✅ same suite, identical output; literals −14…−26 %, large sets −35 % end to end; no query slower beyond noise |
| **rapidjson** built on NVOL (whitespace skipping and end-of-string search; `nvol.hpp`) | rapidjson `297637b` | ✅ 2067 identical results (535 files, fuzz included) across the 5 builds; `Parse` and `ParseInsitu` up to −25 %, writing up to −22 %; equal to or better than SSE2 and SSE4.2 (NVOL built the same way) in every operation, measured with counters on an idle machine |
| All of the above on NEON (aarch64) | `memchr` / `aho-corasick` on aarch64 | ✅ tested (QEMU); performance not measured on ARM hardware |

## Performance (i5-7400, AVX2, GB/s; details in [docs/benchmarks](docs/benchmarks/))

| op | 32 B | 64 KiB | 1 MiB |
|---|---|---|---|
| memchr (absent) | 8.04 vs 6.90 | 86.0 vs 87.8 | 54.9 vs 54.7 |
| memrchr (absent) | 7.49 vs 6.09 | 77.2 vs 75.7 | 52.6 vs 52.1 |
| memchr2 (absent) | 7.47 vs 6.17 | 53.8 vs 53.4 | 42.1 vs 41.4 |
| memchr3 (absent) | 5.81 vs 4.74 | 39.6 vs 38.8 | 33.3 vs 33.0 |
| count `\n` | 6.55 vs 2.54 | **89.2 vs 26.0** | **55.4 vs 25.0** |

(NVOL generated in Rust vs the `memchr` crate, interleaved A/B; `memmem` and Teddy in
[docs/benchmarks](docs/benchmarks/2026-11-18-audit-i5-7400-windows.md).) Dispatch is ifunc-style,
like `memchr`: a function pointer resolved once. Everything still missing is in
[docs/PENDING.md](docs/PENDING.md).

## How it works

Kernels are written in Python on a minimal IR. A strict reference interpreter (the semantics of
each primitive in Python) executes the same tree, and the backends print it. Details in
[docs/DESIGN.md](docs/DESIGN.md).

```
nvol/primitives.py  semantics + contract of each primitive
nvol/isa.py         lowering primitive → intrinsic (SSE2, SSSE3, SSE4.2, AVX2, NEON) per language
nvol/ir.py          IR + Builder
nvol/kernels/       memchr.py, count.py, ascii.py, memmem.py, rank.py, teddy.py, byteset.py, dispatch.py (ifunc)
nvol/interp.py      reference interpreter (detects UB)
nvol/oracle.py      executable specification of each API
nvol/backends/      c.py (C and C++), rust.py, python.py, odin.py, iters.py (memchr_iter), finder.py (memmem), packed.py (Teddy)
nvol/asm/           lower.py (IR → instructions, with inlining; shared by the targets), x86.py (x86-64 instruction selection, spill code, frame), a64.py (A64 and NEON selection, AAPCS64 frame, branch ranges), regalloc.py (intervals with holes, spill splitting, jump cleanup), abi.py (Win64, System V, AAPCS64), encode.py (x86 lengths, jump sizes), nasm.py, masm.py, gas.py (AT&T), gas_a64.py (aarch64), samebytes.py (every syntax and assembler gives the same code)
generated/          current generator output
archive/            implementations no longer used or rejected after measuring, kept readable (nothing builds them)
harness/verify.sh   full verification (see Usage)
harness/c           the whole pytest suite with Clang (run-clang.sh); compile_commands.py for indexing generated/
harness/parity      each language build against NVOL's assembly, with counters (docs/PARITY.md)
harness/rust        parity tests + benchmark vs memchr
harness/cpp         smoke test of the C++ header (native and aarch64 under QEMU)
harness/asm         smoke of generated/{nasm,masm,gas} (gcc and MSVC on Win64; System V in WSL with the whole asm pytest; aarch64 under QEMU: run-a64.sh), counters, a64_static.py
harness/odin        tests, benchmarks and counters of the Odin backend
harness/ripgrep-e2e patches to memchr/aho-corasick to build ripgrep on NVOL
harness/ugrep-e2e   patch to ugrep (C++) on nvol.hpp; build, bench and set microbench in WSL
harness/rapidjson-e2e patch to rapidjson on nvol.hpp; 5 builds (no SIMD, SSE2, SSE4.2, NVOL, NVOL with -msse4.2), fuzz check, bench and counters in WSL
```

## Roadmap

1. ~~Inventory of ripgrep's SIMD~~ · ~~IR + interpreter + C/C++/Rust/Python backends~~ ·
   ~~memchr/memrchr 1-3, count, first_non_ascii~~
2. ~~Cached ifunc dispatch~~ · ~~`memchr_iter`~~ · ~~raw signature `(start, end) -> *const u8`~~
3. ~~`memmem`~~ (packed pair, Two-Way, Rabin-Karp, rarity table; Shift-Or is not on ripgrep's
   path).
4. ~~Teddy~~ (Slim128/Slim256/Fat256 × masks 1-4; SSSE3 ISA; function table in the IR).
5. ~~NEON (aarch64)~~ (parity tested under QEMU; still to be measured on ARM hardware).
6. ~~Odin backend~~ · ~~ripgrep end to end on NVOL~~ → **`grepper` starts**.
7. Formal verification of primitives (bitvectors/SMT).
8. Assembly as internal reference: ~~x86-64 in NASM, Win64 and System V~~ · ~~MASM~~ ·
   ~~GNU as~~ · ~~same bytes in all three~~ · ~~review~~ · ~~optimization against
   GCC/Clang/MSVC~~ (median instruction count +0.2 % at 4 KiB, +5.1 % at 16 B) · ~~aarch64~~
   (tested under QEMU; +12.2 % static instructions against Clang -O2) · ~~maturity~~ (every
   row equal or better in instructions than the best of GCC and Clang, or a listed deviation
   with its cause).
9. ~~Parity of each language against the assembly, with each difference explained~~
   ([parity log](docs/PARITY.md), [toolchain log](docs/TOOLCHAINS.md); `harness/parity`).
   Every change accepted with a worse row is in the [trade-off log](docs/TRADEOFFS.md).
   Open: the rows with the same instructions and more cycles, with item 25
   ([report](docs/benchmarks/2026-11-22-cycle-gaps.md)).
10. simdjson: block-to-mask kernels (64-bit masks, `prefix_xor`, UTF-8, digits), Phase 6 of
    [PENDING](docs/PENDING.md).

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option,
including the generated code. Third-party material and its notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Contributions follow
[CONTRIBUTING.md](CONTRIBUTING.md).
