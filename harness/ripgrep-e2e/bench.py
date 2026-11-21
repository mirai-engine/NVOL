"""End-to-end comparison: ripgrep built on the stock memchr/aho-corasick vs
ripgrep built on NVOL-patched copies of them (see setup.sh).

1. For every query, both binaries must print byte-identical output.
2. Wall time, interleaved: each round runs every query on both binaries,
   alternating which goes first, so drift hits both equally.

    python harness/ripgrep-e2e/bench.py --rounds 15
"""
import argparse
import hashlib
import os
import random
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("W") or os.path.join(HERE, "work")   # as in setup.sh
EXE = ".exe" if os.name == "nt" else ""

WORDS = ["the ", "quick ", "brown ", "fox ", "jumps ", "over ", "lazy ", "dog ", "Sherlock ", "Holmes ",
         "said ", "to ", "Watson ", "and ", "then\n", "of ", "a ", "in ", "was ", "he ", "that ", "it ",
         "his ", "with ", "Adler ", "Irene ", "Baker ", "Street ", "had ", "you ", "not ", "for "]


def make_english(path, size):
    """Synthetic English-like text (xorshift over a word list), ~12 words per line."""
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    x = 0x2545F4914F6CDD1D
    m = (1 << 64) - 1
    out, n = [], 0
    while n < size:
        x ^= (x << 13) & m
        x ^= x >> 7
        x ^= (x << 17) & m
        w = WORDS[x % len(WORDS)]
        out.append(w)
        n += len(w)
    data = "".join(out).encode()[:size]
    with open(path, "wb") as f:
        f.write(data)


def make_code(path, size, src):
    """Concatenated Rust sources from the local cargo registry (sorted, deterministic)."""
    if os.path.exists(path) and os.path.getsize(path) >= size:
        return
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for nm in sorted(names):
            if nm.endswith(".rs"):
                files.append(os.path.join(root, nm))
    with open(path, "wb") as f:
        n = 0
        for p in files:
            with open(p, "rb") as g:
                b = g.read()
            f.write(b)
            n += len(b)
            if n >= size:
                break


QUERIES = [
    # (corpus, name, args, what it exercises)
    ("en", "literal", ["Sherlock Holmes"], "memmem"),
    ("en", "literal-absent", ["Moriarty"], "memmem, whole file"),
    ("en", "byte-absent", ["-F", "Z"], "memchr, whole file"),
    ("en", "alternation", ["Sherlock|Holmes|Watson|Irene|Adler"], "Teddy"),
    ("en", "alternation-absent", ["Moriarty|Lestrade|Mycroft|Hudson"], "Teddy, whole file"),
    ("en", "literal-i", ["-i", "sherlock holmes"], "Teddy (case variants)"),
    ("en", "count-lines", ["-c", "Watson"], "memchr count (lines)"),
    ("en", "line-numbers", ["-n", "Adler Irene"], "memchr count (-n)"),
    ("en", "inner-literal", ["[A-Z]olmes Street"], "memmem (inner literal)"),
    ("code", "literal", ["fn main"], "memmem"),
    ("code", "alternation", ["unsafe fn|extern \"C\"|#\\[inline\\(always\\)\\]"], "Teddy"),
    ("code", "word", ["-w", "unsafe"], "memmem + word boundary"),
    ("code", "absent", ["-F", "zqzqzqzq"], "memmem, whole file"),
    ("code", "count-e", ["-c", "e"], "memchr per line"),
]


def run(rg, args, path, capture=False):
    cmd = [rg, "--no-config", *args, path]
    if capture:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    t = time.perf_counter()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", default=os.path.join(WORK, "target-stock", "release", "rg" + EXE))
    ap.add_argument("--nvol", default=os.path.join(WORK, "target-nvol", "release", "rg" + EXE))
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--size-mb", type=int, default=256)
    ap.add_argument("--registry", default=None, help="cargo registry src dir for the code corpus")
    a = ap.parse_args()

    os.makedirs(os.path.join(WORK, "corpus"), exist_ok=True)
    corpora = {"en": os.path.join(WORK, "corpus", "en.txt"), "code": os.path.join(WORK, "corpus", "code.rs")}
    make_english(corpora["en"], a.size_mb << 20)
    reg = a.registry
    if reg is None:
        base = os.path.join(os.path.expanduser("~"), ".cargo", "registry", "src")
        reg = os.path.join(base, sorted(os.listdir(base))[0])
    make_code(corpora["code"], a.size_mb << 20, reg)
    for k, p in corpora.items():
        print(f"corpus {k}: {os.path.getsize(p) / 2**20:.0f} MiB")

    ok = True
    for c, name, args, _ in QUERIES:
        s = run(a.stock, args, corpora[c], capture=True)
        l = run(a.nvol, args, corpora[c], capture=True)
        same = s == l
        ok &= same
        print(f"output {c:4} {name:20} {'identical' if same else 'DIFFERENT'}  "
              f"{len(s):>11} bytes  sha256 {hashlib.sha256(s).hexdigest()[:12]}")
    if not ok:
        sys.exit("outputs differ")

    times = {(q[0], q[1], b): [] for q in QUERIES for b in ("stock", "nvol")}
    rnd = random.Random(1)
    for r in range(a.rounds):
        for c, name, args, _ in QUERIES:
            order = [("stock", a.stock), ("nvol", a.nvol)]
            if rnd.random() < 0.5:
                order.reverse()
            for b, rg in order:
                times[(c, name, b)].append(run(rg, args, corpora[c]))
        print(f"round {r + 1}/{a.rounds}", file=sys.stderr)

    print(f"\nms, median (min) of {a.rounds} interleaved rounds; nvol vs stock < 0 = NVOL faster;"
          " 'paired' = median of the per-round nvol/stock ratios")
    print(f"{'corpus':6} {'query':20} {'exercises':28} {'stock':>15} {'nvol':>15} {'median':>8} {'min':>8} {'paired':>8}")
    for c, name, _, what in QUERIES:
        s, l = times[(c, name, "stock")], times[(c, name, "nvol")]
        ms, ml = statistics.median(s), statistics.median(l)
        paired = statistics.median(y / x for x, y in zip(s, l))
        print(f"{c:6} {name:20} {what:28} {ms * 1e3:7.1f} ({min(s) * 1e3:5.1f}) {ml * 1e3:7.1f} ({min(l) * 1e3:5.1f})"
              f" {(ml / ms - 1) * 100:+7.1f}% {(min(l) / min(s) - 1) * 100:+7.1f}% {(paired - 1) * 100:+7.1f}%")


if __name__ == "__main__":
    main()
