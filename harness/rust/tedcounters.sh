#!/bin/sh
# Teddy NVOL vs aho-corasick under hardware counters (perf stat), per tedbench case and size:
# (count with N calls - count with 0 calls) / N, best of R repeats. Wall-clock A/B on a shared
# machine has +-1-10 % noise; cycles vary < 1 %.
#
# Build (Windows or Linux host):
#   rustup target add x86_64-unknown-linux-musl
#   CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
#     cargo build --release --target x86_64-unknown-linux-musl --bin tedcount
# Run in Linux (WSL: perf from `apk add perf`):
#   sh tedcounters.sh [cases] [sizes]      defaults: all 8 cases; 4096 65536 1048576
BIN=${BIN:-$(dirname "$0")/target/x86_64-unknown-linux-musl/release/tedcount}
R=${R:-3}
CASES=${1:-"0 1 2 3 4 5 6 7"}
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

printf '%-5s %8s %14s %14s %14s %14s %8s\n' case size nvol-instr aho-instr nvol-cyc aho-cyc delta
for c in $CASES; do
    for s in $SIZES; do
        n=$((268435456 / s))      # ~256 MiB scanned per run
        set -- $(best nvol $c $s $n) $(best nvol $c $s 0) $(best aho $c $s $n) $(best aho $c $s 0)
        awk -v c=$c -v s=$s -v n=$n -v li=$1 -v lc=$2 -v li0=$3 -v lc0=$4 -v ai=$5 -v ac=$6 -v ai0=$7 -v ac0=$8 'BEGIN {
            l = (lc - lc0) / n; a = (ac - ac0) / n
            # delta as in tedbench: positive = NVOL faster
            printf "%-5s %8s %14.0f %14.0f %14.0f %14.0f %+7.1f%%\n", c, s, (li - li0) / n, (ai - ai0) / n, l, a, (a / l - 1) * 100 }'
    done
done
