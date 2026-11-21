#!/bin/sh
# NVOL's own aarch64 assembly (A64, AAPCS64, GNU as syntax), run under QEMU: the aarch64
# Python of the sysroot runs tests/test_asm.py with NVOL_ARCH=aarch64, so every function is
# called through ctypes against the reference interpreter, as on x86-64. Inside the tests the
# native clang cross-compiles (a process under qemu-user runs a native binary directly).
#
#   sh harness/asm/run-a64.sh [pytest arguments]       # inside Alpine (WSL or native)
#
# Needs harness/cpp/setup-aarch64-sysroot.sh once (clang, lld, qemu-aarch64, the sysroot with
# python3 and py3-pytest). QEMU is emulation: this proves correctness, not speed.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
R=${SYSROOT:-/opt/sysroot-aarch64}
export NVOL_ARCH=aarch64
export NVOL_A64_CC="clang --target=aarch64-alpine-linux-musl --sysroot=$R"
cd "$ROOT"
exec qemu-aarch64 -L "$R" "$R/usr/bin/python3" -m pytest -q -p no:cacheprovider tests/test_asm.py "$@"
