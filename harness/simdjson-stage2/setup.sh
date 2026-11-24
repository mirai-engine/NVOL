#!/bin/sh
# Copyright The Mirai Authors. SPDX-License-Identifier: MIT OR Apache-2.0
#
# simdjson's stage-2 routines against NVOL's, inside Linux (WSL): one binary with simdjson's
# amalgamated source and NVOL's generated C++ header, both inlined into the same loops.
#
#   sh setup.sh <simdjson checkout>     # binaries land in $W (default ${E2E:-$HOME/e2e}/stage2)
#   env: CXX (default g++)
set -e
SRC=${1:?usage: setup.sh <simdjson checkout>}
HERE=$(cd "$(dirname "$0")" && pwd)
NVOL=$(cd "$HERE/../.." && pwd)
W=${W:-${E2E:-$HOME/e2e}/stage2}
CXX=${CXX:-g++}
mkdir -p "$W"
# digitsbench.cpp includes simdjson.cpp, where every implementation's code is
$CXX -O2 -std=c++17 -I"$SRC/singleheader" -I"$NVOL/generated/cpp" -o "$W/digitsbench" "$HERE/digitsbench.cpp"
"$W/digitsbench" same
