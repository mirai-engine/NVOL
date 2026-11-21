#!/bin/sh
# Correctness: every build must report exactly what the no-SIMD build reports
# (error code and offset, or output length and hash) for every file and operation.
#
#   sh check.sh <rapidjson repo> <simdjson repo>      # after setup.sh; W as in setup.sh
set -e
RJ=${1:?usage: check.sh <rapidjson repo> <simdjson repo>}
SJ=${2:?usage: check.sh <rapidjson repo> <simdjson repo>}
HERE=$(cd "$(dirname "$0")" && pwd)
W=${W:-${E2E:-$HOME/e2e}/rapidjson}
mkdir -p "$W/corpus" "$W/fuzz"
python3 "$HERE/mkcorpus.py" "$W/corpus" "$SJ/jsonexamples/twitter.json" "$SJ/jsonexamples/citm_catalog.json" "${TEXT:-${E2E:-$HOME/e2e}/corpus/en64.txt}"
python3 "$HERE/fuzz.py" "$W/fuzz" 400
find "$RJ/bin" "$SJ/jsonexamples" "$SJ/tests" "$W/corpus" "$W/fuzz" -name '*.json' -type f | sort > "$W/files.txt"
echo "$(wc -l < "$W/files.txt") files"
for v in base sse2 sse42 nvol nvol42; do
  xargs "$W/jsonbench-$v" check < "$W/files.txt" > "$W/check-$v.txt"
done
# a parse from a static initializer that runs before NVOL's classes are built
${CXX:-g++} -O2 -std=gnu++17 -I"$W/src/include" -I"$HERE/../../generated/cpp" -DRAPIDJSON_NVOL -o "$W/prestatic" "$HERE/prestatic.cpp" "$HERE/nvol_impl.cpp"
"$W/prestatic"
${CXX:-g++} -O2 -std=gnu++17 -I"$W/src/include" -I"$HERE/../../generated/cpp" -DRAPIDJSON_NVOL -msse4.2 -o "$W/prestatic42" "$HERE/prestatic.cpp" "$HERE/nvol_impl.cpp"
"$W/prestatic42"
for v in sse2 sse42 nvol nvol42; do
  if cmp -s "$W/check-base.txt" "$W/check-$v.txt"; then
    echo "$v: identical to base ($(wc -l < "$W/check-$v.txt") results)"
  else
    echo "$v: DIFFERS from base"; diff "$W/check-base.txt" "$W/check-$v.txt" | head -20; exit 1
  fi
done
