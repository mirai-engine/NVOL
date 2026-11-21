#!/bin/sh
# Frontend counters per call for NVOL's asm against the C library built by GCC and Clang
# (-O2): cycles, uops from the uop cache (DSB), uops from the legacy decoder (MITE) and the
# DSB-to-MITE switch penalty, as (count with N calls - count with 0 calls) / N, taken from the
# repeat with the fewest cycles (best of R). Four events, so perf does not multiplex them.
#
#   sh harness/asm/frontend.sh "OP SIZE PATH" ...
#   e.g. sh harness/asm/frontend.sh "memrchr 4096 1" "pair 4096 1"
#   env: W (work dir, default ${E2E:-$HOME/e2e}/asmbench), R (default 7), BUILDS (asm gcc clang),
#        EV (four events, cycles first; the ports, e.g.: cycles,r01a1,r02a1,r04a1)
#
# The events are raw Skylake/Kaby Lake codes (r0879 idq.dsb_uops, r0479 idq.mite_uops,
# r02ab dsb2mite_switches.penalty_cycles), because Alpine's perf has no names for them; WSL's
# kernel counts them. Needs perf, gcc, clang and nasm. Run from Linux or WSL.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
W=${W:-${E2E:-$HOME/e2e}/asmbench}
R=${R:-7}
BUILDS=${BUILDS:-"asm gcc clang"}
EV=${EV:-cycles,r0879,r0479,r02ab}
mkdir -p "$W"
B="$ROOT/harness/asm/bench.c"
gcc -O2 -DNVOL_TESTING -I"$ROOT/generated/c" -o "$W/bench-gcc" "$B" "$ROOT/generated/c/nvol.c"
clang -O2 -DNVOL_TESTING -I"$ROOT/generated/c" -o "$W/bench-clang" "$B" "$ROOT/generated/c/nvol.c"
nasm -f elf64 "$ROOT/generated/nasm/nvol_x86_64_sysv.asm" -o "$W/kernels.o"
gcc -O2 -DNVOL_TESTING -I"$ROOT/generated/nasm" -o "$W/bench-asm" "$B" "$ROOT/generated/nasm/nvol.c" "$W/kernels.o"

counts() { perf stat -x, -e $EV "$@" 2>&1 >/dev/null | awk -F, '{printf "%s ", $1}'; }

printf '%-16s %7s %4s %-6s %s\n' op size path build "$(echo "$EV" | tr , ' ')"
for row in "$@"; do
    set -- $row
    op=$1; size=$2; path=$3
    n=$((33554432 / (size + 64)))
    for b in $BUILDS; do
        best=""
        i=0
        while [ $i -lt "$R" ]; do
            a=$(counts "$W/bench-$b" "$op" "$size" "$n" "$path")
            z=$(counts "$W/bench-$b" "$op" "$size" 0 "$path")
            v=$(echo "$a|$z" | awk -v n="$n" '{split($0, p, "|"); k = split(p[1], x, " "); split(p[2], y, " ");
                for (j = 1; j <= k; j++) printf "%.1f ", (x[j] - y[j]) / n}')
            if [ -z "$best" ] || awk -v a="${v%% *}" -v c="${best%% *}" 'BEGIN {exit !(a < c)}'; then best=$v; fi
            i=$((i + 1))
        done
        set -- $best
        printf '%-16s %7d %4d %-6s %9s %9s %9s %9s\n' "$op" "$size" "$path" "$b" "$1" "$2" "$3" "$4"
    done
done
