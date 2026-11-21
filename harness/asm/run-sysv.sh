#!/bin/sh
# The assembly backend under System V (Linux; from Windows through WSL, see harness/verify.sh):
# the committed generated/nasm and generated/gas built as a user builds them (nasm -f elf64 or
# as --64, the C glue in asm mode, a shared object) and smoke-tested, then every asm test of
# pytest (tests/test_asm.py, both syntaxes, and the `nasm` and `gas` backends of the `native`
# fixture), which builds its own libraries from the generator.
# Needs nasm, binutils (as), gcc, python3 and pytest
# (Alpine: apk add nasm gcc g++ musl-dev python3 py3-pytest; g++ for the unwind test;
# py3-pytest-xdist to run on every CPU).
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
nasm -f elf64 generated/nasm/nvol_x86_64_sysv.asm -o "$W/nasm.o"
as --64 -o "$W/gas.o" generated/gas/nvol_x86_64_sysv.s
for syntax in nasm gas; do
    gcc -O2 -fPIC -Wall -Wextra -Werror -std=c99 -shared -DNVOL_TESTING -o "$W/libnvol_$syntax.so" \
        "generated/$syntax/nvol.c" "$W/$syntax.o"
    python3 harness/asm/smoke.py "$W/libnvol_$syntax.so"
done
N=""      # one worker per CPU with pytest-xdist (Alpine: apk add py3-pytest-xdist)
if python3 -c "import xdist" 2>/dev/null; then N="-n $(nproc)"; fi
# shellcheck disable=SC2086
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider $N tests -k "test_asm or nasm or gas"
