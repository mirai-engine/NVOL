#!/bin/sh
# Restart-heavy queries for the pair-set path, compared byte for byte between the stock and the
# NVOL build: -o and -n print every match, so a candidate lost or invented when a restart resumes
# the search mid-chunk shows up in the output. Each query runs on the file (mmap/read in large
# blocks) and on a pipe (small reads: the buffer shifts and grows between restarts).
#
#   sh resume-check.sh <stock ugrep> <nvol ugrep> <english corpus> <code corpus>
set -e
S=${1:?stock}; L=${2:?nvol}; EN=${3:?en}; CODE=${4:?code}
T=${TMPDIR:-/tmp}/nvol-resume.$$
mkdir -p "$T"
fail=0
q() { # corpus, args...
  f=$1; shift
  "$S" "$@" "$f" > "$T/s" 2>&1 || true
  "$L" "$@" "$f" > "$T/l" 2>&1 || true
  cat "$f" | "$S" "$@" > "$T/sp" 2>&1 || true
  cat "$f" | "$L" "$@" > "$T/lp" 2>&1 || true
  if cmp -s "$T/s" "$T/l" && cmp -s "$T/sp" "$T/lp" && cmp -s "$T/s" "$T/sp"; then r=identical; else r=DIFFERENT; fail=1; fi
  printf '%-9s %-40s %10s bytes\n' "$r" "$*" "$(wc -c < "$T/l")"
}
q "$EN" -on '[HW]ello'
q "$EN" -on -i 'sherlock holmes'
q "$EN" -o -i 'holmes'
q "$EN" -c '[HW]ello'
q "$EN" -on '[st]he [a-z]'
q "$EN" -onw '[Tt]he'
q "$EN" -on 'W[a-z]tson|H[a-z]lmes'
q "$CODE" -on '[QZ][0-9]'
q "$CODE" -on '[QZX][0-9]'
q "$CODE" -on '[{}~]'
q "$CODE" -on '(Q|Z)[a-z]'
q "$CODE" -on '[a-f][0-9][a-f]'
rm -rf "$T"
[ $fail = 0 ] && echo "all identical" || { echo "MISMATCH"; exit 1; }
