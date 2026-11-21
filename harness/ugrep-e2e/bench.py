"""ugrep end to end: stock vs NVOL-patched build (see rebuild.sh / README in this folder).

1. Every query must print byte-identical output on both binaries.
2. Wall time, interleaved: each round runs every query on both, alternating which goes first.

Runs inside WSL:  python3 bench.py --stock .../ugrep-stock/src/ugrep --nvol .../ugrep-nvol/src/ugrep \
                      --en $E2E/corpus/en64.txt --code $E2E/corpus/code64.rs --rounds 21
"""
import argparse
import hashlib
import random
import statistics
import subprocess
import sys
import time

QUERIES = [
    # (corpus, name, args, path taken inside ugrep)
    ("en", "literal", ["Sherlock Holmes"], "NVOL memmem"),
    ("en", "literal-absent", ["Moriarty"], "NVOL memmem"),
    ("en", "count-lines", ["-c", "Watson"], "NVOL memmem + memchr"),
    ("en", "line-numbers", ["-n", "Adler Irene"], "NVOL memmem + count"),
    ("en", "two-chars", ["-c", "zq"], "NVOL memmem (chars<2>)"),
    ("en", "three-chars", ["-c", "Wat"], "NVOL memmem (chars<3>)"),
    ("en", "word", ["-cw", "the"], "NVOL memmem"),
    ("code", "literal", ["fn main"], "NVOL memmem"),
    ("code", "line-numbers", ["-n", "unsafe"], "NVOL memmem + count"),
    ("code", "absent", ["zqzqzqzq"], "NVOL memmem"),
    ("en", "class+literal", ["-c", "[HW]ello"], "NVOL pair, 2-byte sets"),
    ("en", "ignore-case", ["-c", "-i", "sherlock holmes"], "NVOL pair, 2-byte sets"),
    ("en", "alternation", ["-c", "Holmes|Watson"], "NVOL pair"),
    ("code", "rare-pair", ["-c", "[QZ][0-9]"], "NVOL pair, 16-byte set"),
    ("code", "rare-3", ["-c", "[QZX][0-9]"], "NVOL pair, 16-byte set"),
    ("code", "braces", ["-c", "[{}~]"], "NVOL pair, 3-byte set"),
    ("code", "alt-class", ["-c", "(Q|Z)[a-z]"], "NVOL pair"),
    ("code", "alternation", ["-c", "unsafe fn|extern \"C\""], "ugrep pattern (control)"),
]


def run(ug, args, path, capture=False):
    cmd = [ug, "--no-config", *args, path] if False else [ug, *args, path]
    if capture:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    t = time.perf_counter()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - t


ONLY = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--nvol", required=True)
    ap.add_argument("--en", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--rounds", type=int, default=21)
    ap.add_argument("--only", default=None, help="comma-separated query names to run")
    ap.add_argument("--extra", action="append", default=[], help="extra query: corpus:name:arg1 arg2 ...")
    a = ap.parse_args()
    corpora = {"en": a.en, "code": a.code}
    global QUERIES
    for x in a.extra:
        c, name, args = x.split(":", 2)
        QUERIES.append((c, name, args.split(), "extra"))
    if a.only:
        keep = set(a.only.split(","))
        QUERIES = [q for q in QUERIES if q[1] in keep]

    ok = True
    for c, name, args, _ in QUERIES:
        s = run(a.stock, args, corpora[c], capture=True)
        l = run(a.nvol, args, corpora[c], capture=True)
        same = s == l
        ok &= same
        print(f"output {c:4} {name:15} {'identical' if same else 'DIFFERENT'} {len(s):>10} bytes  "
              f"sha256 {hashlib.sha256(s).hexdigest()[:12]}")
    if not ok:
        sys.exit("outputs differ")

    times = {(q[0], q[1], b): [] for q in QUERIES for b in ("stock", "nvol")}
    rnd = random.Random(1)
    for _ in range(a.rounds):
        for c, name, args, _ in QUERIES:
            order = [("stock", a.stock), ("nvol", a.nvol)]
            if rnd.random() < 0.5:
                order.reverse()
            for b, ug in order:
                times[(c, name, b)].append(run(ug, args, corpora[c]))

    print(f"\nms, median (min) of {a.rounds} interleaved rounds; < 0 = NVOL faster; paired = median of per-round ratios")
    print(f"{'corpus':6} {'query':15} {'path':26} {'stock':>15} {'nvol':>15} {'median':>8} {'paired':>8}")
    for c, name, _, what in QUERIES:
        s, l = times[(c, name, "stock")], times[(c, name, "nvol")]
        ms, ml = statistics.median(s), statistics.median(l)
        paired = statistics.median(y / x for x, y in zip(s, l))
        print(f"{c:6} {name:15} {what:26} {ms * 1e3:7.1f} ({min(s) * 1e3:5.1f}) {ml * 1e3:7.1f} ({min(l) * 1e3:5.1f})"
              f" {(ml / ms - 1) * 100:+7.1f}% {(paired - 1) * 100:+7.1f}%")


if __name__ == "__main__":
    main()
