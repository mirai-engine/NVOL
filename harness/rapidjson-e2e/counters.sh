#!/bin/sh
# Hardware counters per operation (perf stat), for differences too small for wall-clock
# timing on a shared machine. For each build, file and operation: `jsonbench count OP N`
# minus `count OP 0` (loading, first parse), divided by N; the best of R repeats for cycles.
#
#   sh counters.sh [builds] [files] [ops]
#   defaults: "sse2 nvol", citm_catalog twitter text_lines, every operation
#   env: W (default ${E2E:-$HOME/e2e}/rapidjson), N (default 200), R (default 5)
#
# Needs perf (Alpine: apk add perf); WSL2 exposes instructions, cycles and branch-misses.
W=${W:-${E2E:-$HOME/e2e}/rapidjson}
N=${N:-200}
R=${R:-5}
BUILDS=${1:-"sse2 nvol"}
FILES=${2:-"citm_catalog twitter text_lines"}
OPS=${3:-"parse parse_len insitu write pretty"}

# one perf run -> "instructions cycles branch-misses"
run() {
    perf stat -x, -e instructions,cycles,branch-misses "$W/jsonbench-$1" count "$2" "$3" "$W/corpus/$4.json" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"], v["branch-misses"]}'
}

# best (minimum) of R runs, per counter
best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} NR == 1 || $3 < c {c = $3} END {print a, b, c}'
}

printf '%-14s %-10s %-10s %14s %14s %12s\n' file op build instr/op cycles/op brmiss/op
for f in $FILES; do
    for op in $OPS; do
        for b in $BUILDS; do
            set -- $(best "$b" "$op" "$N" "$f") $(best "$b" "$op" 0 "$f")
            awk -v f="$f" -v op="$op" -v b="$b" -v n="$N" -v i1="$1" -v c1="$2" -v m1="$3" -v i0="$4" -v c0="$5" -v m0="$6" \
                'BEGIN {printf "%-14s %-10s %-10s %14.0f %14.0f %12.0f\n", f, op, b, (i1 - i0) / n, (c1 - c0) / n, (m1 - m0) / n}'
        done
    done
done
