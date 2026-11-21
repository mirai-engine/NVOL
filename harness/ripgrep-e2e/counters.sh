#!/bin/sh
# ripgrep stock vs NVOL on Linux under hardware counters (perf stat), whole process, best of R,
# the two builds interleaved; then where the cycles go (perf record), so that the share of
# the SIMD kernels is isolated from the rest of rg.
#
#   W=~/rg-work sh counters.sh          # after setup.sh and one bench.py run (the corpora)
#
# W is setup.sh's work directory: target-stock/, target-nvol/ and corpus/.
W=${W:-$(cd "$(dirname "$0")" && pwd)/work}
S=$W/target-stock/release/rg
N=$W/target-nvol/release/rg
EN=$W/corpus/en.txt
CODE=$W/corpus/code.rs
R=${R:-7}

run() {
    perf stat -x, -e instructions,cycles "$@" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}

q() { # corpus, args...
    c=$1; shift
    i=0
    while [ $i -lt "$R" ]; do
        echo "s $(run "$S" --no-config "$@" "$c")"
        echo "n $(run "$N" --no-config "$@" "$c")"
        i=$((i + 1))
    done | Q="$*" awk '                  # ENVIRON: -v would read the backslashes as escapes
        BEGIN {q = ENVIRON["Q"]}
        $1 == "s" && (si == "" || $2 < si) {si = $2} $1 == "s" && (sc == "" || $3 < sc) {sc = $3}
        $1 == "n" && (ni == "" || $2 < ni) {ni = $2} $1 == "n" && (nc == "" || $3 < nc) {nc = $3}
        END {printf "%-40s %14d %14d %14d %14d %+7.2f%% %+7.2f%%\n", q, si, ni, sc, nc,
            (ni / si - 1) * 100, (nc / sc - 1) * 100}'
}

printf '%-40s %14s %14s %14s %14s %8s %8s\n' query stock-instr nvol-instr stock-cyc nvol-cyc d-instr d-cyc
q "$EN" "Sherlock Holmes"
q "$EN" Moriarty
q "$EN" -F Z
q "$EN" "Sherlock|Holmes|Watson|Irene|Adler"
q "$EN" "Moriarty|Lestrade|Mycroft|Hudson"
q "$EN" -i "sherlock holmes"
q "$EN" -c Watson
q "$EN" -n "Adler Irene"
q "$EN" "[A-Z]olmes Street"
q "$CODE" "fn main"
q "$CODE" 'unsafe fn|extern "C"|#\[inline\(always\)\]'
q "$CODE" -w unsafe
q "$CODE" -F zqzqzqzq
q "$CODE" -c e

# the share of each binary's cycles in its SIMD kernels, per query (single-threaded, one file)
echo
printf '%-40s %10s %10s\n' "query (cycles in SIMD kernels)" stock nvol
share() { # binary, pattern of the kernel symbols, args...
    b=$1; pat=$2; shift 2
    perf record -q -o "$W/perf.data" -e cycles "$b" --no-config -j1 "$@" >/dev/null 2>&1
    perf report -i "$W/perf.data" --no-children --sort sym --stdio 2>/dev/null |
        awk -v pat="$pat" '/^ +[0-9.]+%/ {p = $1; sub(/%/, "", p); if ($0 ~ pat) s += p} END {printf "%9.1f%%", s}'
}
K_STOCK='memchr::arch|memchr::memmem|aho_corasick::packed|memchr::memchr|memrchr|count_raw'
K_NVOL='nvol_|memchr::arch|memchr::memmem|aho_corasick::packed'
for a in "$EN|Sherlock Holmes" "$EN|Moriarty" "$EN|-F|Z" "$EN|Moriarty|Lestrade|Mycroft|Hudson" "$EN|-c|Watson" "$CODE|-c|e"; do
    c=${a%%|*}; rest=${a#*|}
    # the alternation pattern itself contains '|': rebuild it for that query
    case $rest in
        Moriarty\|*) set -- "$rest" ;;
        *) IFS='|'; set -- $rest; unset IFS ;;
    esac
    printf '%-40s ' "$*"
    share "$S" "$K_STOCK" "$@" "$c"
    printf ' '
    share "$N" "$K_NVOL" "$@" "$c"
    echo
done
