#!/bin/sh
# Parity of each language backend against NVOL's own assembly (PENDING item 19): the same
# operations, sizes and inputs as harness/asm/bench.c, counted per call with perf stat
# ((count with N calls - count with 0 calls) / N; instructions exact, cycles best of R).
#
#   sh harness/parity/parity.sh [ops] [sizes] [paths]       (Linux; WSL Arch has every tool)
#   defaults: every operation; 16 64 256 4096 65536; paths "1 0" (AVX2, SSE2/SSSE3)
#   env: W (work dir, default /tmp/nvol-parity), R (default 5), BUILDS, MD=1 (markdown table)
#
# Builds: asm (NASM object + the C glue, gcc), gcc and clang (generated/c), gxx and clangxx
# (generated/cpp, through harness/parity/bench_cpp.cpp), rust (harness/rust parbench, LTO) and
# odin (harness/odin/parbench, -o:speed). C++ and Rust have no test hook: they run only on
# path 1, the CPU's own (AVX2). The measure is per call, not per function: Rust's LTO and
# Odin inline the kernels into the caller, so the instructions of one symbol would not compare.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
W=${W:-/tmp/nvol-parity}
R=${R:-5}
BUILDS=${BUILDS:-"asm gcc clang gxx clangxx rust odin"}
OPS=${1:-"memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem memmem2 byteset byteset_not pair teddy"}
SIZES=${2:-"16 64 256 4096 65536"}
PATHS=${3:-"1 0"}
mkdir -p "$W"
B="$ROOT/harness/asm/bench.c"
for b in $BUILDS; do
    case $b in
    asm) nasm -f elf64 "$ROOT/generated/nasm/nvol_x86_64_sysv.asm" -o "$W/kernels.o"
         gcc -O2 -DNVOL_TESTING -I"$ROOT/generated/nasm" -o "$W/bench-asm" "$B" "$ROOT/generated/nasm/nvol.c" "$W/kernels.o" ;;
    gcc|clang) $b -O2 -DNVOL_TESTING -I"$ROOT/generated/c" -o "$W/bench-$b" "$B" "$ROOT/generated/c/nvol.c" ;;
    gxx) g++ -O2 -std=c++17 -I"$ROOT/generated/cpp" -o "$W/bench-gxx" "$ROOT/harness/parity/bench_cpp.cpp" ;;
    clangxx) clang++ -O2 -std=c++17 -I"$ROOT/generated/cpp" -o "$W/bench-clangxx" "$ROOT/harness/parity/bench_cpp.cpp" ;;
    rust) CARGO_TARGET_DIR="$W/cargo" cargo build -q --release --manifest-path "$ROOT/harness/rust/Cargo.toml" --bin parbench
          cp "$W/cargo/release/parbench" "$W/bench-rust" ;;
    odin) odin build "$ROOT/harness/odin/parbench" -collection:nvol="$ROOT/generated" -o:speed -out:"$W/bench-odin" ;;
    *) echo "unknown build $b" >&2; exit 2 ;;
    esac
done

hooked() { case $1 in asm|gcc|clang|odin) return 0 ;; esac; return 1; }
run() {     # build op size n path -> "instructions cycles"
    b=$1; shift
    if [ "$b" = rust ]; then set -- "$1" "$2" "$3"; fi       # parbench OP SIZE N (no hook)
    perf stat -x, -e instructions,cycles "$W/bench-$b" "$@" 2>&1 >/dev/null |
        awk -F, '$3 ~ /instructions/ {i = $1} $3 ~ /cycles/ {c = $1} END {print i, c}'
}
best() {
    i=0
    while [ $i -lt "$R" ]; do run "$@"; i=$((i + 1)); done |
        awk 'NR == 1 || $1 < a {a = $1} NR == 1 || $2 < b {b = $2} END {print a, b}'
}

OUT="$W/parity.txt"
: > "$OUT"
for op in $OPS; do
    for size in $SIZES; do
        n=$((33554432 / (size + 64)))
        for path in $PATHS; do
            for b in $BUILDS; do
                if [ "$path" != 1 ] && ! hooked "$b"; then continue; fi
                set -- $(best "$b" "$op" "$size" "$n" "$path")
                a1=$1; c1=$2
                set -- $(best "$b" "$op" "$size" 0 "$path")
                awk -v op="$op" -v size="$size" -v path="$path" -v b="$b" -v n="$n" \
                    -v a1="$a1" -v c1="$c1" -v a0="$1" -v c0="$2" \
                    'BEGIN {printf "%s %s %s %s %.1f %.1f\n", op, size, path, b, (a1 - a0) / n, (c1 - c0) / n}' >> "$OUT"
            done
        done
    done
done

# the table: each build against the asm of the same row (instructions, cycles, and the deltas)
awk -v md="${MD:-0}" '
{ k = $1 " " $2 " " $3; i[k, $4] = $5; c[k, $4] = $6; if (!(k in seen)) { seen[k] = 1; order[++n] = k }
  if (!($4 in bs)) { bs[$4] = 1; border[++nb] = $4 } }
END {
    if (md) {
        printf "| op | size | path |"; for (j = 1; j <= nb; j++) printf " %s |", border[j]; print ""
        printf "|---|---|---|"; for (j = 1; j <= nb; j++) printf "---|"; print ""
    } else {
        printf "%-16s %6s %4s", "op", "size", "path"; for (j = 1; j <= nb; j++) printf " %22s", border[j]; print ""
    }
    for (r = 1; r <= n; r++) {
        k = order[r]; split(k, f, " ")
        if (md) printf "| %s | %s | %s |", f[1], f[2], f[3]; else printf "%-16s %6s %4s", f[1], f[2], f[3]
        for (j = 1; j <= nb; j++) {
            b = border[j]
            if (!((k, b) in i)) { if (md) printf " — |"; else printf " %22s", "-"; continue }
            if (b == "asm" || !((k, "asm") in i)) cell = sprintf("%.0f / %.1f", i[k, b], c[k, b])
            else cell = sprintf("%+.0f%% / %+.0f%%", (i[k, b] / i[k, "asm"] - 1) * 100, (c[k, b] / c[k, "asm"] - 1) * 100)
            if (md) printf " %s |", cell; else printf " %22s", cell
        }
        print ""
    }
}' "$OUT"
