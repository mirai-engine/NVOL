#!/bin/sh
# The generated C and C++ for aarch64 (NEON), inside Linux (WSL or native): compile
# nvol.c and the C++17 raw layer with -Werror, then build smoke.cpp (C++20 wrappers)
# statically and run it under QEMU (emulated: correctness, not speed).
#
#   sh harness/cpp/run-aarch64.sh        # needs setup-aarch64-sysroot.sh once
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
R=${SYSROOT:-/opt/sysroot-aarch64}
if [ ! -f "$R/usr/include/stdio.h" ]; then
    echo "no aarch64 sysroot at $R: run harness/cpp/setup-aarch64-sysroot.sh (Alpine) or set SYSROOT" >&2
    exit 1
fi
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
A="--target=aarch64-alpine-linux-musl --sysroot=$R"
W="-O2 -Wall -Wextra -Werror"
clang $A -std=c11 $W -c "$ROOT/generated/c/nvol.c" -o "$T/nvol.o"
echo "c: ok"
printf '#include <nvol.hpp>\nint main() { return 0; }\n' > "$T/raw.cpp"
clang++ $A -std=c++17 $W -fsyntax-only -I"$ROOT/generated/cpp" "$T/raw.cpp"
echo "c++17 raw layer: ok"
clang++ $A -std=c++20 $W -static -fuse-ld=lld "$ROOT/harness/cpp/smoke.cpp" -o "$T/smoke"
${QEMU:-qemu-aarch64} "$T/smoke"
echo "c++20 smoke under QEMU: ok"
