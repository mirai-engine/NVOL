#!/bin/sh
# ugrep stock vs NVOL under hardware counters (perf stat), whole process: instructions and
# cycles per query, best of R, the two builds interleaved. Wall time on the same queries has
# a noise of a few percent (bench.py); cycles vary < 1 %.
#
#   sh counters.sh [stock ugrep] [nvol ugrep] [en corpus]
#
# Defaults: the builds of setup.sh under $W (${E2E:-$HOME/e2e}) and $W/corpus/en64.txt.
W=${W:-${E2E:-$HOME/e2e}}
STOCK=${1:-$W/ugrep-stock/src/ugrep}
NV=${2:-$W/ugrep-nvol/src/ugrep}
EN=${3:-$W/corpus/en64.txt}
R=${R:-11}

run() {
    perf stat -x, -e instructions,cycles "$@" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}

printf '%-16s %14s %14s %14s %14s %8s %8s\n' query stock-instr nvol-instr stock-cyc nvol-cyc d-instr d-cyc
for q in "-c zq" "-c Wat" "-c Watson" "Sherlock Holmes" "Moriarty"; do
    i=0
    while [ $i -lt "$R" ]; do
        # shellcheck disable=SC2086
        echo "s $(run "$STOCK" $q "$EN")"
        echo "n $(run "$NV" $q "$EN")"
        i=$((i + 1))
    done | awk -v q="$q" '
        $1 == "s" && (si == "" || $2 < si) {si = $2} $1 == "s" && (sc == "" || $3 < sc) {sc = $3}
        $1 == "n" && (ni == "" || $2 < ni) {ni = $2} $1 == "n" && (nc == "" || $3 < nc) {nc = $3}
        END {printf "%-16s %14d %14d %14d %14d %+7.2f%% %+7.2f%%\n", q, si, ni, sc, nc,
            (ni / si - 1) * 100, (nc / sc - 1) * 100}'
done
