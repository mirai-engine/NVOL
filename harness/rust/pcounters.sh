#!/bin/sh
# NVOL against its Rust bar under hardware counters (perf stat), for the counter benches that
# take `IMPL ARG N`: (count with N calls - count with 0 calls) / N, best of R repeats.
#
#   sh pcounters.sh linecount "nvol memchr" "8 20 40 64 100 200 1000" 50
#   sh pcounters.sh nacount   "nvol bstr"   "16 64 256 4096 65536"
#
# Without a fourth argument, N gives ~256 MiB scanned per run (ARG is a size in bytes).
# Build first (Windows or Linux host):
#   CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
#     cargo build --release --target x86_64-unknown-linux-musl --bin linecount --bin nacount
BENCH=${1:?usage: pcounters.sh BENCH "IMPL1 IMPL2" "ARGS" [N]}
BIN=${BIN:-$(dirname "$0")/target/x86_64-unknown-linux-musl/release/$BENCH}
IMPLS=${2:?impls}
ARGS=${3:?args}
FIXED=$4
R=${R:-5}

run() {
    perf stat -x, -e instructions,cycles "$BIN" "$@" 2>&1 >/dev/null |
        awk -F, '{v[$3]=$1} END {print v["instructions"], v["cycles"]}'
}
best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}

set -- $IMPLS
A=$1
B=$2
printf '%-8s %12s %12s %12s %12s %8s %8s\n' arg "$A-instr" "$B-instr" "$A-cyc" "$B-cyc" d-instr d-cyc
for x in $ARGS; do
    n=${FIXED:-$((268435456 / x))}
    set -- $(best "$A" "$x" "$n") $(best "$A" "$x" 0) $(best "$B" "$x" "$n") $(best "$B" "$x" 0)
    awk -v x="$x" -v n="$n" -v ai="$1" -v ac="$2" -v ai0="$3" -v ac0="$4" \
        -v bi="$5" -v bc="$6" -v bi0="$7" -v bc0="$8" 'BEGIN {
        a = (ai - ai0) / n; b = (bi - bi0) / n; c = (ac - ac0) / n; d = (bc - bc0) / n
        printf "%-8s %12.1f %12.1f %12.1f %12.1f %+7.1f%% %+7.1f%%\n", x, a, b, c, d,
            (a / b - 1) * 100, (c / d - 1) * 100 }'
done
