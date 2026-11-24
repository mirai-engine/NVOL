#!/bin/sh
# Copyright The Mirai Authors. SPDX-License-Identifier: MIT OR Apache-2.0
#
# Hardware counters per 32-byte step (perf stat): `stringsbench count IMPL N FILE` minus the
# same with N = 0, divided by N * steps; the best of R repeats per counter.
#
#   sh strings.sh [impls] [files]
#   defaults: every IMPL, every file in $W/corpus
#   env: W (default ${E2E:-$HOME/e2e}/stage2), N (default 2000), R (default 15)
W=${W:-${E2E:-$HOME/e2e}/stage2}
N=${N:-2000}
R=${R:-15}
IMPLS=${1:-"nvol-avx2 simdjson-haswell nvol-sse2@westmere simdjson-westmere"}
FILES=${2:-$(cd "$W/corpus" && ls *.json | sed 's/\.json$//')}

run() {
    perf stat -x, -e instructions,cycles "$W/stringsbench" count "$1" "$2" "$W/corpus/$3.json" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}

best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}

printf '%-14s %-19s %10s %11s\n' file impl instr/step cycles/step
for f in $FILES; do
    steps=$(( $(wc -c < "$W/corpus/$f.json") / 32 ))
    for m in $IMPLS; do
        set -- $(best "$m" "$N" "$f") $(best "$m" 0 "$f")
        awk -v f="$f" -v m="$m" -v n="$N" -v s="$steps" -v i1="$1" -v c1="$2" -v i0="$3" -v c0="$4" \
            'BEGIN {k = n * s; printf "%-14s %-19s %10.2f %11.3f\n", f, m, (i1 - i0) / k, (c1 - c0) / k}'
    done
done
