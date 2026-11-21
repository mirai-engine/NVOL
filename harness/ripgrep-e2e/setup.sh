#!/usr/bin/env bash
# ripgrep end to end on NVOL: copies memchr 2.8.3 and aho-corasick 1.1.5 from the
# local cargo registry into work/, applies the NVOL patches (every SIMD entry point
# that ripgrep reaches calls generated/rust/nvol.rs), and builds ripgrep twice
# without touching the ripgrep checkout: stock and with [patch.crates-io].
#
#   RIPGREP=../ripgrep bash harness/ripgrep-e2e/setup.sh          # build both
#   RIPGREP=../ripgrep TEST=1 bash harness/ripgrep-e2e/setup.sh   # + crate and ripgrep test suites
#   python harness/ripgrep-e2e/bench.py
#
# Needs the crates already in the registry (build ripgrep or harness/rust once);
# everything runs with --offline.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="${W:-$HERE/work}"          # W=... builds elsewhere (a Linux filesystem: faster)
RIPGREP="${RIPGREP:?set RIPGREP to a ripgrep checkout (15.x, Cargo.lock with memchr 2.8.3 / aho-corasick 1.1.5)}"
REG="$(ls -d "$HOME"/.cargo/registry/src/*/memchr-2.8.3 | head -1)"
REG="$(dirname "$REG")"

mkdir -p "$W"
for c in memchr:2.8.3 aho-corasick:1.1.5; do
  name="${c%%:*}"; ver="${c##*:}"
  rm -rf "$W/$name"
  cp -r "$REG/$name-$ver" "$W/$name"
  (cd "$W/$name" && patch -p1 --binary -s < "$HERE/$name-$ver.patch")
  cp "$HERE/../../generated/rust/nvol.rs" "$W/$name/src/nvol.rs"   # the patch's `mod nvol`
done

wpath() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }
PATCH=(--config "patch.crates-io.memchr.path=\"$(wpath "$W/memchr")\""
       --config "patch.crates-io.aho-corasick.path=\"$(wpath "$W/aho-corasick")\"")

if [ "${TEST:-0}" = 1 ]; then
  (cd "$W/memchr" && cargo test --release --offline --lib)
  (cd "$W/aho-corasick" && cargo test --release --offline --lib)
fi

cd "$RIPGREP"
cp Cargo.lock "$W/Cargo.lock.orig"
trap 'cp "$W/Cargo.lock.orig" "$RIPGREP/Cargo.lock"' EXIT   # the patched build rewrites the lock
cargo build --release --offline --target-dir "$W/target-stock"
cargo build --release --offline --target-dir "$W/target-nvol" "${PATCH[@]}"
if [ "${TEST:-0}" = 1 ]; then
  cargo test --release --offline --workspace --target-dir "$W/target-nvol" "${PATCH[@]}"
fi
