#!/bin/sh
# Odin vs NVOL-Rust vs the reference crates under hardware counters (perf stat), per xbench
# case and size: (count with N calls - count with 0 calls) / N, best of R repeats.
# Cycles vary < 1 % between runs; wall clock on this machine varies +-5-10 %.
#
#   bash xcount-build.sh                      (Windows or Linux host)
#   sh xcounters.sh [cases] [sizes]           (Linux; WSL: apk add perf)
# defaults: memchr_iter_nl; 4096 65536 1048576
BIN=${BIN:-$(dirname "$0")/xcount/xcount}
R=${R:-3}
CASES=${1:-memchr_iter_nl}
SIZES=${2:-"4096 65536 1048576"}

run() {
    perf stat -x, -e instructions,cycles "$BIN" "$@" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}
best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}
# per call: instructions and cycles of N calls minus the N = 0 run
per() {
    set -- $(best "$1" "$2" "$3" "$4") $(best "$1" "$2" "$3" 0) "$4"
    awk -v i=$1 -v c=$2 -v i0=$3 -v c0=$4 -v n=$5 'BEGIN {printf "%.0f %.0f", (i - i0) / n, (c - c0) / n}'
}

printf '%-16s %8s %9s %9s %9s %9s %9s %9s %9s\n' case size odin-ins nvol-ins ref-ins odin-cyc nvol-cyc ref-cyc odin/nvol
for c in $CASES; do
    for s in $SIZES; do
        n=$((268435456 / s))      # ~256 MiB scanned per run
        set -- $(per odin $c $s $n) $(per nvol $c $s $n) $(per ref $c $s $n)
        # odin/nvol as in xbench: positive = Odin faster
        awk -v c=$c -v s=$s -v oi=$1 -v oc=$2 -v li=$3 -v lc=$4 -v ri=$5 -v rc=$6 'BEGIN {
            printf "%-16s %8s %9s %9s %9s %9s %9s %9s %+8.1f%%\n", c, s, oi, li, ri, oc, lc, rc, (lc / oc - 1) * 100 }'
    done
done
