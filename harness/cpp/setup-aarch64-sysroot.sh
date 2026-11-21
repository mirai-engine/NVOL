#!/bin/sh
# One-time setup, inside Alpine Linux (WSL or native): an aarch64 musl sysroot with the
# C and C++ headers and libraries, plus clang, lld and qemu-aarch64, so that
# harness/cpp/run-aarch64.sh can build the generated C/C++ for aarch64 and run it; and for
# harness/asm/run-a64.sh, binutils-aarch64 (GNU as and objdump for aarch64), a native python3,
# and python3 with pytest and pytest-xdist inside the sysroot (pytest runs under QEMU, one
# process per CPU).
#
#   sh harness/cpp/setup-aarch64-sysroot.sh          # SYSROOT defaults to /opt/sysroot-aarch64
set -e
R=${SYSROOT:-/opt/sysroot-aarch64}
apk add -q clang lld qemu-aarch64 binutils-aarch64 python3
if [ ! -f "$R/usr/include/stdio.h" ]; then
    mkdir -p "$R/etc/apk"
    cp /etc/apk/repositories "$R/etc/apk/"
    apk --arch aarch64 --root "$R" --initdb --allow-untrusted --no-scripts -q add musl-dev libstdc++-dev g++ libgcc
fi
if [ ! -x "$R/usr/bin/python3" ]; then
    apk --arch aarch64 --root "$R" --allow-untrusted --no-scripts -q add python3 py3-pytest
fi
if [ ! -d "$(ls -d "$R"/usr/lib/python3*/site-packages/xdist 2>/dev/null | head -1)" ]; then
    apk --arch aarch64 --root "$R" --allow-untrusted --no-scripts -q add py3-pytest-xdist
fi
echo "aarch64 sysroot ready: $R"
