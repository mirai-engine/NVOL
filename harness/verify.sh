#!/usr/bin/env bash
# Full verification of NVOL: every step must pass before a change is committed.
#
#   bash harness/verify.sh            # all steps
#   SKIP="rust-a64 odin-a64" bash harness/verify.sh
#
# Steps (names usable in SKIP):
#   check     python -m nvol check: kernels (IR) vs oracle, x86_64 and aarch64
#   pytest    the generated C via ctypes, on the forced CPU paths
#   cpp       the generated C++ header compiles (raw layer C++17, wrappers C++20) and
#             harness/cpp/smoke.cpp (the C++20 wrappers) builds and passes, native
#   cpp-a64   the generated C and C++ for aarch64: compiled with clang -Werror, smoke.cpp
#             linked statically and run under QEMU (harness/cpp/run-aarch64.sh; needs
#             harness/cpp/setup-aarch64-sysroot.sh once, in Alpine)
#   rust      harness/rust: cargo test, native
#   rust-a64  harness/rust for aarch64 under QEMU (emulated: correctness only)
#   odin      harness/odin/tests, native (every forced x86 path)
#   odin-a64  harness/odin/tests for aarch64 under QEMU
#   asm       the committed generated/nasm, generated/masm and generated/gas (NVOL's own
#             x86_64 assembly, Win64) assembled with NASM, ml64 and GNU as
#             (harness/asm/assemble.py), each linked with the C glue by gcc and by MSVC
#             (cl + link.exe, found with vswhere: harness/asm/build_msvc.py) and smoke-tested
#             (harness/asm/smoke.py); native Windows only (the pytest step already runs every
#             asm test, all three syntaxes, natively)
#   asm-sysv  the same under System V for NASM and GNU as: harness/asm/run-sysv.sh, natively
#             on Linux or through WSL (needs nasm, binutils, gcc, python3, pytest there),
#             smoke plus every asm test of pytest
#   asm-a64   NVOL's own aarch64 assembly (GNU as syntax, AAPCS64): harness/asm/run-a64.sh in
#             Alpine (WSL or native; needs harness/cpp/setup-aarch64-sysroot.sh once): GNU as
#             and Clang give the same bytes, the committed generated/gas library built and
#             smoke-tested, then test_asm and the native suites under QEMU (emulated:
#             correctness only)
#
# A step passes only on exit status 0 of its own command; nothing is decided by
# reading its output. A step that cannot run (tool missing) is SKIPPED and makes
# the whole run fail unless it was asked for in SKIP.
#
# QEMU: qemu-aarch64 from PATH, else through WSL (WSL_DISTRO, default Alpine);
# override with QEMU="...".
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="$(mktemp -d)"
SKIP=" ${SKIP:-} "
CXX=${CXX:-g++}
PY=${PY:-python}
if [ -z "${QEMU:-}" ]; then
    if command -v qemu-aarch64 >/dev/null 2>&1; then QEMU=qemu-aarch64
    elif command -v wsl >/dev/null 2>&1; then QEMU="wsl -d ${WSL_DISTRO:-Alpine} -- qemu-aarch64"
    fi
fi
export QEMU

failed=()
skipped=()
passed=()

# step NAME REQUIRED_TOOL COMMAND...
step() {
    local name=$1 tool=$2; shift 2
    if [[ "$SKIP" == *" $name "* ]]; then
        echo "=== $name: skipped (SKIP)"
        return
    fi
    if [ -n "$tool" ] && ! command -v "$tool" >/dev/null 2>&1; then
        echo "=== $name: SKIPPED ($tool not found)"
        skipped+=("$name")
        return
    fi
    local t0=$SECONDS
    if "$@" >"$LOG/$name.log" 2>&1; then
        echo "=== $name: ok ($((SECONDS - t0)) s)"
        passed+=("$name")
    else
        echo "=== $name: FAILED (exit $?, $((SECONDS - t0)) s)"
        tail -n 30 "$LOG/$name.log" | sed 's/^/    /'
        failed+=("$name")
    fi
}

cpp_header() {
    local tu='#include <nvol.hpp>
int main() { return 0; }'
    echo "$tu" | "$CXX" -x c++ -std=c++17 -Wall -Wextra -Werror -I"$ROOT/generated/cpp" -fsyntax-only - &&
    echo "$tu" | "$CXX" -x c++ -std=c++20 -Wall -Wextra -Werror -I"$ROOT/generated/cpp" -fsyntax-only - &&
    "$CXX" -std=c++20 -O2 -Wall -Wextra -Werror "$ROOT/harness/cpp/smoke.cpp" -o "$LOG/smoke.exe" &&
    "$LOG/smoke.exe"
}

# a script meant for Linux: directly when QEMU is native, through WSL otherwise
linux_sh() {
    local script=$1
    if [[ "$QEMU" == wsl* ]]; then
        local win path
        win=$(cygpath -w "$script" 2>/dev/null || echo "$script")
        path=$(echo "$win" | sed -E 's#^([A-Za-z]):#/mnt/\L\1#; s#\\#/#g')
        MSYS_NO_PATHCONV=1 wsl -d "${WSL_DISTRO:-Alpine}" -- sh "$path"
    else
        sh "$script"
    fi
}
cpp_a64() { need_qemu && linux_sh "$ROOT/harness/cpp/run-aarch64.sh"; }

rust_native() { (cd "$ROOT/harness/rust" && cargo test --release); }

need_qemu() {
    if [ -z "${QEMU:-}" ]; then echo "no qemu-aarch64 and no WSL (set QEMU)"; return 1; fi
}
rust_a64() { need_qemu && bash "$ROOT/harness/run-aarch64.sh"; }
asm_win() {
    local syntax d ext
    for syntax in nasm masm gas; do
        d="$LOG/asm-$syntax"
        ext=asm; [ "$syntax" = gas ] && ext=s
        mkdir -p "$d"
        "$PY" "$ROOT/harness/asm/assemble.py" "$syntax" \
            "$ROOT/generated/$syntax/nvol_x86_64_win64.$ext" "$d/kernels.obj" win64 &&
        gcc -static -O2 -Wall -Wextra -Werror -std=c99 -shared -DNVOL_BUILD_DLL -DNVOL_TESTING \
            -o "$d/nvol_gcc.dll" "$ROOT/generated/$syntax/nvol.c" "$d/kernels.obj" &&
        "$PY" "$ROOT/harness/asm/smoke.py" "$d/nvol_gcc.dll" &&
        "$PY" "$ROOT/harness/asm/build_msvc.py" "$d/kernels.obj" "$d/nvol_msvc.dll" "$syntax" &&
        "$PY" "$ROOT/harness/asm/smoke.py" "$d/nvol_msvc.dll" || return 1
    done
}
asm_sysv() {
    if [[ "$(uname -s)" == Linux ]]; then sh "$ROOT/harness/asm/run-sysv.sh"
    elif command -v wsl >/dev/null 2>&1; then linux_sh "$ROOT/harness/asm/run-sysv.sh"
    else echo "no Linux and no WSL"; return 1
    fi
}
odin_native() { odin run "$ROOT/harness/odin/tests" -collection:nvol="$ROOT/generated" -o:speed; }
odin_a64() { need_qemu && bash "$ROOT/harness/odin/run-aarch64.sh"; }
asm_a64() { need_qemu && linux_sh "$ROOT/harness/asm/run-a64.sh"; }

step check    "$PY"   "$PY" -m nvol check
step pytest   "$PY"   "$PY" -m pytest -q tests
step cpp      "$CXX"  cpp_header
step cpp-a64  ""     cpp_a64
step rust     cargo   rust_native
step rust-a64 cargo   rust_a64
step odin     odin    odin_native
step odin-a64 odin    odin_a64
step asm      nasm    asm_win
step asm-sysv ""      asm_sysv
step asm-a64  ""      asm_a64

echo
echo "passed:  ${passed[*]:-none}"
[ ${#skipped[@]} -gt 0 ] && echo "skipped: ${skipped[*]} (not requested: counts as a failure)"
[ ${#failed[@]} -gt 0 ] && echo "FAILED:  ${failed[*]}  (logs in $LOG)"
if [ ${#failed[@]} -eq 0 ] && [ ${#skipped[@]} -eq 0 ]; then
    echo "overall: OK"
    rm -rf "$LOG"
    exit 0
fi
echo "overall: FAIL"
exit 1
