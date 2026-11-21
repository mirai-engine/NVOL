"""Random JSON documents that sweep the lengths the SIMD scans care about.

usage: python3 fuzz.py OUT_DIR COUNT

Strings of 0-300 bytes mixing plain text, escapes, raw control bytes (invalid
JSON: the parse must fail at the same offset) and UTF-8; whitespace runs of 0-80
bytes of all four kinds; documents from a few bytes to ~200 KiB, so string and
whitespace ends fall on every offset of a vector and near page boundaries.
"""
import random
import sys

out, count = sys.argv[1], int(sys.argv[2])
rng = random.Random(20261118)
WS = ' \t\r\n'
PLAIN = 'abcdefghijklmnopqrstuvwxyz ABCDEFGHIJ0123456789.,;:!?-_/'


def ws():
    k = rng.choice([0, 0, 1, 2, rng.randrange(0, 81)])
    return ''.join(rng.choice(WS) for _ in range(k))


def string(bad):
    n = rng.choice([0, 1, rng.randrange(0, 40), rng.randrange(0, 301)])
    parts = []
    for _ in range(n):
        r = rng.random()
        if r < 0.03:
            parts.append(rng.choice(['\\"', '\\\\', '\\n', '\\t', '\\u00e9', '\\/']))
        elif r < 0.05:
            parts.append(rng.choice(['é', '日本', '€']))
        elif bad and r < 0.052:
            parts.append(chr(rng.randrange(0, 0x20)))
        else:
            parts.append(rng.choice(PLAIN))
    return '"' + ''.join(parts) + '"'


def value(depth, bad):
    r = rng.random()
    if depth > 4 or r < 0.4:
        return string(bad)
    if r < 0.5:
        return str(rng.randrange(-10**6, 10**6))
    if r < 0.55:
        return rng.choice(['true', 'false', 'null'])
    if r < 0.8:
        items = [ws() + value(depth + 1, bad) + ws() for _ in range(rng.randrange(0, 12))]
        return '[' + ','.join(items) + ']'
    items = [ws() + string(bad) + ws() + ':' + ws() + value(depth + 1, bad) + ws() for _ in range(rng.randrange(0, 12))]
    return '{' + ','.join(items) + '}'


for i in range(count):
    bad = i % 10 == 9
    doc = ws() + '[' + ','.join(value(0, bad) for _ in range(rng.choice([1, 5, 50, 400]))) + ']' + ws()
    open(f'{out}/f{i:04d}.json', 'w', encoding='utf8', newline='').write(doc)
print(count, 'files in', out)
