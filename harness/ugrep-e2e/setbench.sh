#!/bin/sh
# Build and run setbench.c (NVOL byte sets vs ugrep-style compare chains) in WSL.
set -e
L=$(cd "$(dirname "$0")/../.." && pwd)
gcc -O2 -mavx2 -I"$L/generated/c" "$L/harness/ugrep-e2e/setbench.c" "$L/generated/c/nvol.c" -lm -o /tmp/setbench
/tmp/setbench
