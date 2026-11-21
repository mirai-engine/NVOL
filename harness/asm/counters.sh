#!/bin/sh
# NVOL's own assembly against the C library compiled by GCC and Clang (-O2), with hardware
# counters per call (perf stat): for each operation, size, CPU path and build, the counts of
# `bench OP SIZE N PATH` minus those of `bench OP SIZE 0 PATH` (setup), divided by N.
# Instructions are exact; cycles are the best of R repeats.
#
#   sh harness/asm/counters.sh [ops] [sizes] [paths]
#   defaults: every operation of bench.c; 16 64 256 4096 65536; paths "1 0" (AVX2, SSE2/SSSE3)
#   env: W (work dir, default ${E2E:-$HOME/e2e}/asmbench), R (default 5), BUILDS (asm gcc clang)
#
# Needs perf, gcc, clang and nasm (Alpine: apk add perf clang nasm). Run from Linux or WSL.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
W=${W:-${E2E:-$HOME/e2e}/asmbench}
R=${R:-5}
BUILDS=${BUILDS:-"asm gcc clang"}
OPS=${1:-"memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem byteset byteset_not pair teddy"}
SIZES=${2:-"16 64 256 4096 65536"}
PATHS=${3:-"1 0"}
mkdir -p "$W"
B="$ROOT/harness/asm/bench.c"
gcc -O2 -DNVOL_TESTING -I"$ROOT/generated/c" -o "$W/bench-gcc" "$B" "$ROOT/generated/c/nvol.c"
clang -O2 -DNVOL_TESTING -I"$ROOT/generated/c" -o "$W/bench-clang" "$B" "$ROOT/generated/c/nvol.c"
nasm -f elf64 "$ROOT/generated/nasm/nvol_x86_64_sysv.asm" -o "$W/kernels.o"
gcc -O2 -DNVOL_TESTING -I"$ROOT/generated/nasm" -o "$W/bench-asm" "$B" "$ROOT/generated/nasm/nvol.c" "$W/kernels.o"

run() {     # build op size n path -> "instructions cycles"
    perf stat -x, -e instructions,cycles "$W/bench-$1" "$2" "$3" "$4" "$5" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}
best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}

printf '%-16s %7s %4s %-6s %12s %12s\n' op size path build instr/call cycles/call
for op in $OPS; do
    for size in $SIZES; do
        n=$((33554432 / (size + 64)))
        for path in $PATHS; do
            for b in $BUILDS; do
                set -- $(best "$b" "$op" "$size" "$n" "$path") $(best "$b" "$op" "$size" 0 "$path")
                awk -v op="$op" -v s="$size" -v p="$path" -v b="$b" -v n="$n" -v i1="$1" -v c1="$2" -v i0="$3" -v c0="$4" \
                    'BEGIN {printf "%-16s %7d %4d %-6s %12.1f %12.1f\n", op, s, p, b, (i1 - i0) / n, (c1 - c0) / n}'
            done
        done
    done
done
