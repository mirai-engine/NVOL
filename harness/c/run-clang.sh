#!/bin/sh
# The whole pytest suite with Clang as the C compiler (PENDING 2.5): every C build of the
# suite (the native library of each backend, the asm glue, the primitive and fuzz builds)
# through clang instead of gcc. Linux; from Windows through WSL (harness/verify.sh, step
# c-clang). Needs clang, nasm, binutils, python3 and pytest (Alpine: apk add clang; the rest as
# for harness/asm/run-sysv.sh).
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
clang --version | head -1
N=""      # one worker per CPU with pytest-xdist
if python3 -c "import xdist" 2>/dev/null; then N="-n $(nproc)"; fi
K=""
if [ -n "${PYTEST_K:-}" ]; then K="$PYTEST_K"; fi      # verify.sh's PYTEST_K
# shellcheck disable=SC2086
NVOL_CC=clang PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider $N tests ${K:+-k "$K"}
