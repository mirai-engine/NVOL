#!/usr/bin/env bash
# Build the Odin test harness for linux_arm64 (NEON), link it statically against
# the musl that ships with Rust's aarch64-unknown-linux-musl target (rust-lld),
# and run it under QEMU user mode.
#   needs: odin, rustup target add aarch64-unknown-linux-musl, qemu-aarch64
#   QEMU="wsl -d Alpine -- qemu-aarch64" on Windows + WSL
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ODIN=${ODIN:-odin}
OUT=${OUT:-$(mktemp -d)}
T=$(rustc --print sysroot)/lib/rustlib
SC=$T/aarch64-unknown-linux-musl/lib/self-contained
LLD=$(command ls "$T"/*/bin/rust-lld* | head -1)
CB=$(command ls "$T"/aarch64-unknown-linux-musl/lib/libcompiler_builtins-*.rlib | head -1)
"$ODIN" build "$ROOT/harness/odin/tests" -collection:nvol="$ROOT/generated" -target:linux_arm64 \
    -build-mode:obj -o:speed -out:"$OUT/tests.o"
"$LLD" -flavor gnu -static -o "$OUT/tests" "$SC/crt1.o" "$SC/crti.o" "$SC/crtbegin.o" "$OUT/tests.o" \
    --start-group "$SC/libc.a" "$CB" --end-group "$SC/crtend.o" "$SC/crtn.o"
QEMU=${QEMU:-qemu-aarch64}
if [[ "$QEMU" == wsl* ]]; then
    win=$(cygpath -w "$OUT/tests" 2>/dev/null || echo "$OUT/tests")
    path=$(echo "$win" | sed -E 's#^([A-Za-z]):#/mnt/\L\1#; s#\\#/#g')
    MSYS_NO_PATHCONV=1 $QEMU "$path"
else
    $QEMU "$OUT/tests"
fi
