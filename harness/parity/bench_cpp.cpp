// C++ side of the parity benchmark (harness/parity): bench.c itself, compiled as C++ against
// the C++ backend (generated/cpp/nvol.hpp, namespace nvol::raw). The C++ backend has no test
// hook, so PATH is ignored: the CPU's own path (AVX2 on x86_64), as for Rust.
#include "nvol.hpp"
using namespace nvol::raw;
#define NVOL_BENCH_NO_C_HEADER
#define NVOL_BENCH_NO_TEST_HOOK
#include "../asm/bench.c"
