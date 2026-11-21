#!/bin/sh
# NVOL's own aarch64 assembly (A64, AAPCS64, GNU as syntax), the `asm-a64` step of
# harness/verify.sh. With no arguments:
#   1. one reference: the library assembled by GNU as (binutils-aarch64) and by Clang gives the
#      same bytes, the same unwind rules, and the function sizes NVOL predicts
#      (python3 -m nvol.asm.samebytes aapcs64, natively);
#   2. the committed generated/gas/nvol_aarch64_aapcs64.s and nvol.c, built as a user builds
#      them (cross clang, a shared object), smoke-tested under QEMU (harness/asm/smoke.py);
#   3. under QEMU, the aarch64 Python of the sysroot runs pytest with NVOL_ARCH=aarch64:
#      tests/test_asm.py (every function and NEON primitive against the interpreter, through
#      ctypes) and the native suites (differential, byteset, memmem, Teddy) over the C and over
#      the assembly (conftest backend gas-a64). Inside the tests the native clang
#      cross-compiles (a process under qemu-user runs a native binary directly).
# With arguments, only step 3, over those test files and pytest arguments.
#
#   sh harness/asm/run-a64.sh [test files and pytest arguments]   # inside Alpine (WSL or native)
#
# Needs harness/cpp/setup-aarch64-sysroot.sh once (clang, lld, qemu-aarch64, binutils-aarch64,
# the sysroot with python3 and py3-pytest) and a native python3. QEMU is emulation: this
# proves correctness, not speed.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
R=${SYSROOT:-/opt/sysroot-aarch64}
export NVOL_ARCH=aarch64
export NVOL_A64_CC="clang --target=aarch64-alpine-linux-musl --sysroot=$R"
export PYTHONDONTWRITEBYTECODE=1
cd "$ROOT"
if [ $# -eq 0 ]; then
    NVOL_ARCH= python3 -m nvol.asm.samebytes aapcs64
    W="$(mktemp -d)"
    trap 'rm -rf "$W"' EXIT
    $NVOL_A64_CC -c -o "$W/kernels.o" generated/gas/nvol_aarch64_aapcs64.s
    $NVOL_A64_CC -fuse-ld=lld -O2 -fPIC -Wall -Wextra -Werror -std=c99 -shared -DNVOL_TESTING \
        -o "$W/libnvol_a64.so" generated/gas/nvol.c "$W/kernels.o"
    qemu-aarch64 -L "$R" "$R/usr/bin/python3" harness/asm/smoke.py "$W/libnvol_a64.so"
    set -- tests/test_asm.py tests/test_differential.py tests/test_byteset.py \
        tests/test_memmem.py tests/test_teddy.py
fi
# one QEMU process per CPU when the sysroot has pytest-xdist (QEMU user mode is single-threaded).
# Each worker is started through qemu-aarch64 too: without binfmt_misc (WSL) the aarch64
# python cannot be exec'd directly, and a process under qemu-user runs a native binary
if qemu-aarch64 -L "$R" "$R/usr/bin/python3" -c "import xdist" 2>/dev/null; then
    set -- --dist load --tx "$(nproc)*popen//python=qemu-aarch64 -L $R $R/usr/bin/python3" "$@"
fi
qemu-aarch64 -L "$R" "$R/usr/bin/python3" -m pytest -q -p no:cacheprovider "$@"
