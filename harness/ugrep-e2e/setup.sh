#!/bin/sh
# ugrep end to end on NVOL, inside Linux (WSL): build ugrep twice from the same
# commit -- stock, and with ugrep-7.8.5.patch, which routes its byte search to
# NVOL's generated C++ header (generated/cpp/nvol.hpp, header-only) -- then
# optionally run ugrep's own test suite on both.
#
#   sh setup.sh <ugrep git repo (v7.8.5)>            # TEST=1 also runs `make test` on both
#   python3 bench.py --stock $W/ugrep-stock/src/ugrep --nvol $W/ugrep-nvol/src/ugrep --en ... --code ...
#
# W (default ${E2E:-$HOME/e2e}) holds the two clones and the logs; NVOL is this repo.
# Both builds use -std=gnu++17 (nvol.hpp's raw layer needs C++17), so the
# comparison differs only in the patch.
set -e
SRC=${1:?usage: setup.sh <ugrep git repo>}
HERE=$(cd "$(dirname "$0")" && pwd)
NVOL=$(cd "$HERE/../.." && pwd)
W=${W:-${E2E:-$HOME/e2e}}
mkdir -p "$W"

# autotools: a fresh clone has arbitrary mtimes, so make wants to rerun aclocal/automake
fix_stamps() {
  touch aclocal.m4 && sleep 1
  touch configure Makefile.in lib/Makefile.in src/Makefile.in lzma/C/Makefile.in config.h.in && sleep 1
  touch config.status && ./config.status > /dev/null
  touch Makefile lib/Makefile src/Makefile config.h
}

build() { # name, extra configure args...
  name=$1; shift
  cd "$W" && rm -rf "ugrep-$name"
  git -c core.autocrlf=false clone -q "$SRC" "ugrep-$name"
  cd "ugrep-$name"
  git checkout -q v7.8.5
  if [ "$name" = nvol ]; then git apply "$HERE/ugrep-7.8.5.patch"; fi
  ./configure --disable-dependency-tracking CXXFLAGS="-O2 -std=gnu++17" "$@" > "$W/configure-$name.log" 2>&1
  fix_stamps
  make -j"$(nproc)" > "$W/make-$name.log" 2>&1
  echo "built $W/ugrep-$name/src/ugrep"
}

build stock
build nvol CPPFLAGS="-I$NVOL/generated/cpp"

if [ "${TEST:-0}" = 1 ]; then
  for name in stock nvol; do
    (cd "$W/ugrep-$name" && make test > "$W/test-$name.log" 2>&1) && echo "test $name: $(grep -c 'ALL TESTS PASSED' "$W/test-$name.log")/2 suites passed"
  done
fi
