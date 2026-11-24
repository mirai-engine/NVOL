#!/bin/sh
# Copyright The Mirai Authors. SPDX-License-Identifier: MIT OR Apache-2.0
#
# Hardware counters per call (perf stat): `digitsbench count IMPL MODE N` minus the same with
# N = 0, divided by N * 4096 calls; the best of R repeats per counter.
#
#   sh counters.sh [impls] [modes]
#   defaults: every IMPL, "tput chain"
#   env: W (default ${E2E:-$HOME/e2e}/stage2), N (default 20000), R (default 15)
W=${W:-${E2E:-$HOME/e2e}/stage2}
N=${N:-20000}
R=${R:-15}
IMPLS=${1:-"nvol-sse42@haswell simdjson-haswell nvol-sse42@westmere simdjson-westmere nvol-swar simdjson-fallback nvol-is simdjson-is"}
MODES=${2:-"tput chain"}

run() {
    perf stat -x, -e instructions,cycles "$W/digitsbench" count "$1" "$2" "$3" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}

best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}

printf '%-21s %-6s %10s %11s\n' impl mode instr/call cycles/call
for mode in $MODES; do
    for m in $IMPLS; do
        set -- $(best "$m" "$mode" "$N") $(best "$m" "$mode" 0)
        awk -v m="$m" -v mode="$mode" -v n="$N" -v i1="$1" -v c1="$2" -v i0="$3" -v c0="$4" \
            'BEGIN {k = n * 4096; printf "%-21s %-6s %10.2f %11.3f\n", m, mode, (i1 - i0) / k, (c1 - c0) / k}'
    done
done
