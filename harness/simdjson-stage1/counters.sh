#!/bin/sh
# Hardware counters per stage-1 pass (perf stat): `stage1bench count IMPL N FILE` minus the
# same with N = 0 (loading, first pass), divided by N; the best of R repeats per counter.
#
#   sh counters.sh [impls] [files]
#   defaults: "simdjson nvol", every file in $W/corpus
#   env: W (default ${E2E:-$HOME/e2e}/stage1), N (default 2000), R (default 15)
W=${W:-${E2E:-$HOME/e2e}/stage1}
N=${N:-2000}
R=${R:-15}
IMPLS=${1:-"simdjson nvol"}
FILES=${2:-$(cd "$W/corpus" && ls *.json | sed 's/\.json$//')}

run() {
    perf stat -x, -e instructions,cycles,branch-misses "$W/stage1bench" count "$1" "$2" "$W/corpus/$3.json" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"], v["branch-misses"]}'
}

best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} NR == 1 || $3 < c {c = $3} END {print a, b, c}'
}

printf '%-14s %-9s %8s %12s %12s %10s %9s\n' file impl bytes instr/pass cycles/pass brmiss B/cycle
for f in $FILES; do
    bytes=$(wc -c < "$W/corpus/$f.json")
    for m in $IMPLS; do
        set -- $(best "$m" "$N" "$f") $(best "$m" 0 "$f")
        awk -v f="$f" -v m="$m" -v n="$N" -v by="$bytes" -v i1="$1" -v c1="$2" -v m1="$3" -v i0="$4" -v c0="$5" -v m0="$6" \
            'BEGIN {c = (c1 - c0) / n; printf "%-14s %-9s %8d %12.0f %12.0f %10.1f %9.2f\n", f, m, by, (i1 - i0) / n, c, (m1 - m0) / n, by / c}'
    done
done
