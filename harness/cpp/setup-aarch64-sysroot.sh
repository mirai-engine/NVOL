#!/bin/sh
# One-time setup, inside Alpine Linux (WSL or native): an aarch64 musl sysroot with the
# C and C++ headers and libraries, plus clang, lld and qemu-aarch64, so that
# harness/cpp/run-aarch64.sh can build the generated C/C++ for aarch64 and run it.
#
#   sh harness/cpp/setup-aarch64-sysroot.sh          # SYSROOT defaults to /opt/sysroot-aarch64
set -e
R=${SYSROOT:-/opt/sysroot-aarch64}
apk add -q clang lld qemu-aarch64
if [ ! -f "$R/usr/include/stdio.h" ]; then
    mkdir -p "$R/etc/apk"
    cp /etc/apk/repositories "$R/etc/apk/"
    apk --arch aarch64 --root "$R" --initdb --allow-untrusted --no-scripts -q add musl-dev libstdc++-dev g++ libgcc
fi
echo "aarch64 sysroot ready: $R"
