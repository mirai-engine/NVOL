#!/usr/bin/env bash
# Build the Rust parity harness for aarch64 (NEON) and run it under QEMU user mode.
#
# Needs: rustup target add aarch64-unknown-linux-musl   (rust-lld + static musl are included)
#        qemu-aarch64 (Linux/WSL: e.g. `apk add qemu-aarch64` or `apt install qemu-user`)
# On Windows + WSL set QEMU="wsl -d <distro> -- qemu-aarch64" and the path is translated.
set -euo pipefail
cd "$(dirname "$0")/rust"
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld
cargo test --release --target aarch64-unknown-linux-musl --lib --no-run >/dev/null 2>&1
exe=$(ls -t target/aarch64-unknown-linux-musl/release/deps/nvol_harness-* | grep -v '\.d$' | head -1)
QEMU=${QEMU:-qemu-aarch64}
if [[ "$QEMU" == wsl* ]]; then
    win=$(cygpath -w "$PWD/$exe" 2>/dev/null || echo "$PWD/$exe")
    path=$(echo "$win" | sed -E 's#^([A-Za-z]):#/mnt/\L\1#; s#\\#/#g')
    MSYS_NO_PATHCONV=1 $QEMU "$path" --test-threads=4
else
    $QEMU "$exe" --test-threads=4
fi
