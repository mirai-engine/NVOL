#!/usr/bin/env bash
# Build harness/odin/xcount for linux_amd64: the Odin object, the Rust FFI staticlib for
# x86_64-unknown-linux-musl, linked statically with Rust's self-contained musl (rust-lld).
#   needs: odin, rustup target add x86_64-unknown-linux-musl
# Output: $OUT/xcount (default harness/odin/xcount/xcount); measure it with xcounters.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ODIN=${ODIN:-odin}
OUT=${OUT:-$ROOT/harness/odin/xcount}
T=$(rustc --print sysroot)/lib/rustlib
SC=$T/x86_64-unknown-linux-musl/lib/self-contained
LLD=$(command ls "$T"/*/bin/rust-lld* | head -1)
CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
    cargo build -q --release --target x86_64-unknown-linux-musl --manifest-path "$ROOT/harness/rust-ffi/Cargo.toml"
"$ODIN" build "$ROOT/harness/odin/xcount" -collection:nvol="$ROOT/generated" -target:linux_amd64 \
    -build-mode:obj -o:speed ${ODIN_FLAGS:-} -out:"$OUT/xcount.o"
"$LLD" -flavor gnu -static -o "$OUT/xcount" "$SC/crt1.o" "$SC/crti.o" "$SC/crtbegin.o" "$OUT/xcount.o" \
    --start-group "$ROOT/harness/rust-ffi/target/x86_64-unknown-linux-musl/release/libnvol_ffi_bench.a" \
    "$SC/libc.a" "$SC/libunwind.a" --end-group "$SC/crtend.o" "$SC/crtn.o"
rm -f "$OUT/xcount.o" "$OUT/xcount.ll"
