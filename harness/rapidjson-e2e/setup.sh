#!/bin/sh
# rapidjson end to end on NVOL, inside Linux (WSL). rapidjson is header-only, so
# the same jsonbench.cpp is built five times against one patched checkout:
#
#   base   no SIMD (rapidjson's default)
#   sse2   -DRAPIDJSON_SSE2               stock SIMD
#   sse42  -DRAPIDJSON_SSE42 -msse4.2     stock SIMD (pcmpistri for whitespace)
#   nvol   -DRAPIDJSON_NVOL               rapidjson-<commit>.patch: NVOL byte-set search
#                                         (+ nvol_impl.cpp: the kernels, in their own unit)
#   nvol42 -DRAPIDJSON_NVOL -msse4.2      the same, built with SSE4.2 like sse42: the whitespace
#                                         skip inlines NVOL's pcmpistri kernel
#
# Without RAPIDJSON_NVOL the patch changes nothing, so base/sse2/sse42 are stock.
#
#   sh setup.sh <rapidjson git repo>      # binaries and corpus land in $W (default ${E2E:-$HOME/e2e}/rapidjson)
set -e
SRC=${1:?usage: setup.sh <rapidjson git repo>}
HERE=$(cd "$(dirname "$0")" && pwd)
NVOL=$(cd "$HERE/../.." && pwd)
W=${W:-${E2E:-$HOME/e2e}/rapidjson}
COMMIT=297637b
mkdir -p "$W"
rm -rf "$W/src"
git -c core.autocrlf=false clone -q "$SRC" "$W/src"
(cd "$W/src" && git checkout -q $COMMIT && git apply "$HERE/rapidjson-$COMMIT.patch")

CXX=${CXX:-g++}
FLAGS="-O2 -std=gnu++17 -I$W/src/include -I$NVOL/generated/cpp"
$CXX $FLAGS                                -o "$W/jsonbench-base"  "$HERE/jsonbench.cpp"
$CXX $FLAGS -DRAPIDJSON_SSE2               -o "$W/jsonbench-sse2"  "$HERE/jsonbench.cpp"
$CXX $FLAGS -DRAPIDJSON_SSE42 -msse4.2     -o "$W/jsonbench-sse42" "$HERE/jsonbench.cpp"
$CXX $FLAGS -DRAPIDJSON_NVOL               -o "$W/jsonbench-nvol"  "$HERE/jsonbench.cpp" "$HERE/nvol_impl.cpp"
$CXX $FLAGS -DRAPIDJSON_NVOL -msse4.2      -o "$W/jsonbench-nvol42" "$HERE/jsonbench.cpp" "$HERE/nvol_impl.cpp"
echo "built $W/jsonbench-{base,sse2,sse42,nvol,nvol42}"
