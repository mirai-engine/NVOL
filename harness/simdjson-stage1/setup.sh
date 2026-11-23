#!/bin/sh
# simdjson's stage 1 against nvol_json_index_raw, inside Linux (WSL). One binary links
# simdjson's single-header build (runtime dispatch: haswell on AVX2) and NVOL's generated C.
#
#   sh setup.sh <simdjson checkout>     # binary and corpus land in $W (default ${E2E:-$HOME/e2e}/stage1)
#   env: CC (default gcc), CXX (default g++)
set -e
SRC=${1:?usage: setup.sh <simdjson checkout>}
HERE=$(cd "$(dirname "$0")" && pwd)
NVOL=$(cd "$HERE/../.." && pwd)
W=${W:-${E2E:-$HOME/e2e}/stage1}
CC=${CC:-gcc}
CXX=${CXX:-g++}
mkdir -p "$W/corpus"
cp "$SRC"/jsonexamples/twitter.json "$SRC"/jsonexamples/citm_catalog.json "$W/corpus/"
$CC -O2 -c -I"$NVOL/generated/c" -o "$W/nvol.o" "$NVOL/generated/c/nvol.c"
$CXX -O2 -std=c++17 -c -I"$SRC/singleheader" -o "$W/simdjson.o" "$SRC/singleheader/simdjson.cpp"
$CXX -O2 -std=c++17 -I"$SRC/singleheader" -I"$NVOL/generated/c" -o "$W/stage1bench" \
    "$HERE/stage1bench.cpp" "$W/simdjson.o" "$W/nvol.o"
for f in "$W"/corpus/*.json; do "$W/stage1bench" same "$f"; done
