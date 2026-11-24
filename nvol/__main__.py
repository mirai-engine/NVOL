"""NVOL command line.

    python -m nvol gen   --lang c,cpp,rust,python,odin,nasm,masm,gas --out generated/
    python -m nvol check [--cases N]        # interpreter vs oracle
    python -m nvol list                     # kernels, primitives, ISAs
"""
from __future__ import annotations

import argparse
import os
import random
import sys

from . import oracle
from .backends import c as c_backend
from .backends import python as py_backend
from .backends import rust as rust_backend
from .interp import Interpreter, Memory
from .isa import ARCH_ISAS, ISAS
from .kernels import ALL, build_module
from .primitives import PRIMS

LANGS = ("c", "cpp", "rust", "python", "odin", "nasm", "masm", "gas")


def asm_files(mod, syntax: str) -> dict:
    """The library from NVOL's own assembly backend in one syntax: a file per calling
    convention that syntax's assembler targets (NASM: Win64 and System V; MASM: Win64; GNU as:
    those two and aarch64 AAPCS64) and the C that goes with it (nvol.c in `asm_kernels` mode:
    glue, CPU checks, test hook; nvol.h is the C backend's)."""
    from .asm import SYNTAX_ABIS, SYNTAX_EXT, library
    mods = mod if isinstance(mod, dict) else {getattr(mod, "arch", "x86_64"): mod}
    if "x86_64" not in mods:
        raise SystemExit(f"{syntax}: the assembly backend generates x86_64 only (use --arch x86_64 or all)")
    arches = {"x86_64"} | ({"aarch64"} if "aapcs64" in SYNTAX_ABIS[syntax] and "aarch64" in mods else set())
    files = c_backend.generate(mods, "c", asm_kernels=arches)
    for abi in SYNTAX_ABIS[syntax]:
        arch = "aarch64" if abi == "aapcs64" else "x86_64"
        if arch in arches:
            files[f"nvol_{arch}_{abi}{SYNTAX_EXT[syntax]}"] = library(mods[arch], abi, syntax)
    return files


def generate(mod, langs, out):
    written = []
    for lang in langs:
        if lang == "c":
            files = c_backend.generate(mod, "c")
        elif lang == "cpp":
            files = c_backend.generate(mod, "cpp")
        elif lang == "rust":
            files = rust_backend.generate(mod)
        elif lang == "python":
            files = py_backend.generate(mod)
        elif lang == "odin":
            from .backends import odin as odin_backend
            files = odin_backend.generate(mod)
        elif lang in ("nasm", "masm", "gas"):
            files = asm_files(mod, lang)
        else:
            raise SystemExit(f"unknown language {lang!r}; choose from {LANGS}")
        d = os.path.join(out, lang)
        os.makedirs(d, exist_ok=True)
        for name, text in files.items():
            path = os.path.join(d, name)
            with open(path, "w", newline="\n", encoding="utf-8") as fh:
                fh.write(text)
            written.append(path)
    return written


def memmem_cases(rng, n):
    """(hay, needle) pairs that stress every memmem branch."""
    alphas = [b"ab", b"abc", b"a", bytes(range(256)), b"the quick brown fox\n"]
    for _ in range(n):
        alpha = rng.choice(alphas)
        hl = rng.choice([0, 1, 5, 15, 16, 17, 31, 33, 48, 64, 100, 200, 400])
        hay = bytes(rng.choice(alpha) for _ in range(hl))
        nl = rng.choice([0, 1, 2, 3, 4, 7, 8, 16, 31, 32, 33, 40, 64, 80])
        kind = rng.randrange(4)
        if kind == 0 and hl >= nl:          # needle taken from the haystack
            o = rng.randrange(hl - nl + 1)
            needle = hay[o:o + nl]
        elif kind == 1:                     # periodic needle (small period two-way)
            unit = bytes(rng.choice(alpha) for _ in range(rng.randint(1, 3)))
            needle = (unit * 100)[:nl]
        else:
            needle = bytes(rng.choice(alpha) for _ in range(nl))
        if kind == 3 and hl >= nl and nl:   # plant it near the end
            o = hl - nl - rng.randrange(min(8, hl - nl) + 1)
            hay = hay[:o] + needle + hay[o + nl:]
        yield hay, needle


def memmem_state(mod, mem, nbase, nlen, sbase=0xC0000):
    """Run nvol_memmem_init into a fresh writable region; returns its base."""
    from .kernels.memmem import STATE_WORDS
    mem.add(bytes(8 * STATE_WORDS), sbase)
    Interpreter(mod, mem).call("nvol_memmem_init", [nbase, nbase + nlen, sbase])
    return sbase


def check_memmem(mod, cases: int, seed: int) -> int:
    rng = random.Random(seed)
    bad = total = 0
    for hay, needle in memmem_cases(rng, cases):
        hb = 0x10000 + rng.randrange(64)
        nb = 0x80000 + rng.randrange(64)
        mem = Memory(hay, hb)
        mem.add(needle, nb)
        sb = memmem_state(mod, mem, nb, len(needle))
        for cpu in ({"avx2": True}, {"avx2": False}):
            p = Interpreter(mod, mem, cpu).call("nvol_memmem_find_raw",
                                                [sb, nb, nb + len(needle), hb, hb + len(hay)])
            got = oracle.NONE if p == 0 else p - hb
            want = oracle.memmem(hay, needle)
            total += 1
            if got != want:
                bad += 1
                print(f"MISMATCH memmem hay={hay[:40]!r}..({len(hay)}) needle={needle!r} got={got} want={want}")
    print(f"[{getattr(mod, 'arch', 'x86_64')}] memmem interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


def teddy_blob(patterns) -> bytes:
    """Patterns blob layout read by the Teddy kernels (see kernels/teddy.py)."""
    n = len(patterns)
    head = 8 + 16 * n
    out, offs, pos = [n.to_bytes(8, "little")], [], head
    for p in patterns:
        offs.append((pos, len(p)))
        pos += len(p)
    out += [o.to_bytes(8, "little") + ln.to_bytes(8, "little") for o, ln in offs]
    return b"".join(out) + b"".join(patterns)


def teddy_cases(rng, n):
    alphas = [b"ab", b"abcd", b"the quick brown fox", bytes(range(256)), b"aA\x00\xff"]
    for _ in range(n):
        alpha = rng.choice(alphas)
        npat = rng.choice([1, 2, 3, 5, 8, 9, 16, 17, 32, 33, 40, 64, 65])
        lens = rng.choice([(1, 1), (1, 3), (2, 2), (2, 5), (3, 3), (3, 8), (4, 4), (4, 9), (6, 12)])
        hl = rng.choice([0, 1, 5, 15, 16, 17, 18, 19, 20, 31, 32, 33, 34, 35, 40, 47, 48, 64, 100, 200])
        hay = bytes(rng.choice(alpha) for _ in range(hl))
        pats = []
        for _ in range(npat):
            ln = rng.randint(*lens)
            if hay and rng.random() < 0.3 and ln <= hl:
                o = rng.randrange(hl - ln + 1)
                pats.append(hay[o:o + ln])
            else:
                pats.append(bytes(rng.choice(alpha) for _ in range(ln)))
        if rng.random() < 0.03:
            pats[rng.randrange(len(pats))] = b""
        start = rng.randrange(hl + 1) if rng.random() < 0.3 else 0
        end = rng.randrange(start, hl + 1) if rng.random() < 0.3 else hl
        yield pats, hay, start, end


def check_teddy(mod, cases: int, seed: int) -> int:
    from .kernels.teddy import STATE_BYTES
    rng = random.Random(seed)
    bad = total = 0
    arch = getattr(mod, "arch", "x86_64")
    cpus = ([{"avx2": True, "ssse3": True}, {"avx2": False, "ssse3": True}, {"avx2": False, "ssse3": False}]
            if arch == "x86_64" else [{}])
    for pats, hay, start, end in teddy_cases(rng, cases):
        blob = teddy_blob(pats)
        for cpu in cpus:
            hb, bb, sb = 0x10000 + rng.randrange(64), 0x80000, 0xC0000
            mem = Memory(hay, hb)
            mem.add(blob, bb)
            mem.add(bytes(STATE_BYTES), sb)
            it = Interpreter(mod, mem, cpu)
            it.call("nvol_teddy_init", [bb, sb])
            kind = int.from_bytes(mem.region(sb)[:8], "little")
            ok_build = oracle.teddy_buildable(pats, cpu.get("avx2", False), cpu.get("ssse3", False), arch)
            total += 1
            if (kind != 0) != ok_build:
                bad += 1
                print(f"MISMATCH teddy build kind={kind} want_buildable={ok_build} n={len(pats)} pats={pats[:4]}")
                continue
            if not kind:
                continue
            r = it.call("nvol_teddy_find_raw", [sb, bb, hb + start, hb + end])
            got = None if r == oracle.NONE else (r & 0xFF, start + (r >> 8), start + (r >> 8) + len(pats[r & 0xFF]))
            want = oracle.leftmost_first(pats, hay, start, end)
            total += 1
            if got != want:
                bad += 1
                print(f"MISMATCH teddy cpu={cpu} kind={kind} hay={hay!r}[{start}:{end}] pats={pats} got={got} want={want}")
    print(f"[{arch}] teddy interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


def byteset_cases(rng, n):
    """(set1, set2, i1, i2, hay, start, end): sets of 1..20 bytes (duplicates allowed) or ranges,
    hay bytes drawn mostly from outside the sets so hits land anywhere."""
    pool = [0x00, 0x0A, 0x41, 0x5A, 0x61, 0x7A, 0x7F, 0x80, 0x8F, 0x9A, 0xC3, 0xF0, 0xFF]
    for _ in range(n):
        def one_set():
            if rng.random() < 0.2:      # range set: [0, t] plus 0..4 bytes above it (4: nibble lookup)
                t = rng.choice([1, 5, 8, 31, 31, 64, 200, 255])
                return bytes(range(t + 1)) + bytes(rng.randrange(t + 1, 256) for _ in range(rng.randrange(5)) if t < 255)
            if rng.random() < 0.25:     # unique-nibble set: one member per low nibble at most
                los = rng.sample(range(16), rng.choice([1, 2, 4, 7, 16]))
                return bytes((rng.randrange(16) << 4) | lo for lo in los)
            k = rng.choice([1, 2, 3, 4, 5, 8, 16, 20])
            return bytes(rng.choice(pool + [rng.randrange(256)]) for _ in range(k))
        s1, s2 = one_set(), one_set()
        i1, i2 = rng.choice([0, 1, 2, 5, 17, 33]), rng.choice([0, 1, 3, 7, 31, 40])
        L = rng.choice([0, 1, 5, 15, 16, 17, 31, 32, 33, 47, 48, 63, 64, 65, 100, 200, 300])
        filler = bytes(b for b in range(256) if b not in s1 and b not in s2) or b"x"
        hay = bytearray(rng.choice(filler) for _ in range(L))
        for _ in range(rng.choice([0, 0, 1, 2, 5])):
            if L:
                hay[rng.randrange(L)] = rng.choice(s1 + s2)
        start = rng.choice([0, 0, min(3, L)])
        end = L - rng.choice([0, 0, min(2, L - start)])
        yield s1, s2, i1, i2, bytes(hay), start, end


JSON_SNIPPETS = [b'{"a": 1}', b'[1, 2.5e-3, true, false, null]', b'"x\\"y"', b'"\\\\"', b'{"k\\\\": [ ]}',
                 b'"a\x01b"', b'  \t\r\n', b'"\\u00e9"', b'tru e', b'"\xc3\xa9"']


# code points near every boundary of UTF-8's validity rules
UTF8_POINTS = [0x00, 0x24, 0x7F, 0x80, 0xA9, 0x7FF, 0x800, 0x20AC, 0xD7FF, 0xE000, 0xFFFD, 0xFFFF,
               0x10000, 0x1F600, 0x10FFFF]
# invalid sequences: overlong 2/3/4-byte, surrogates, above U+10FFFF, lone and extra
# continuations, truncated leads, bytes never used
UTF8_BAD = [b"\xc0\x80", b"\xc1\xbf", b"\xe0\x80\x80", b"\xe0\x9f\xbf", b"\xed\xa0\x80",
            b"\xed\xbf\xbf", b"\xf0\x80\x80\x80", b"\xf0\x8f\xbf\xbf", b"\xf4\x90\x80\x80",
            b"\xf5\x80\x80\x80", b"\xf8\x88\x80\x80\x80", b"\x80", b"\xbf", b"\xc3\xa9\xa9",
            b"\xc3", b"\xe2\x82", b"\xf0\x9f\x98", b"\xfe", b"\xff", b"\xe2\x28\xa1"]


def digit_cases(rng, n):
    """8-byte strings: mostly digits, with (half the time) one byte just outside '0'..'9',
    a digit with its high bit or nibble changed, or any byte."""
    near = [0x2F, 0x3A, 0x20, 0x2E, 0x2D, 0x2B, 0x65, 0x45, 0x00, 0xFF]
    for _ in range(n):
        data = bytearray(rng.choice(b"0123456789") for _ in range(8))
        r = rng.random()
        if r < 0.5:
            i = rng.randrange(8)
            data[i] = (rng.choice(near) if r < 0.2 else data[i] | 0x80 if r < 0.3
                       else data[i] ^ (rng.randrange(1, 16) << 4) if r < 0.4 else rng.randrange(256))
        yield bytes(data)


def check_numbers(mod, cases: int, seed: int) -> int:
    rng = random.Random(seed)
    bad = total = 0
    arch = getattr(mod, "arch", "x86_64")
    cpus = [{"sse42": True}, {"sse42": False}] if arch == "x86_64" else [{}]
    for data in digit_cases(rng, cases):
        want = oracle.is_eight_digits(data)
        for cpu in cpus:
            hb = 0x10000 + rng.randrange(64)
            got = Interpreter(mod, Memory(data, hb), cpu).call("nvol_is_eight_digits", [hb])
            total += 1
            if got != want:
                bad += 1
                print(f"MISMATCH is_eight_digits data={data!r} got={got} want={want}")
            if want:
                got = Interpreter(mod, Memory(data, hb), cpu).call("nvol_parse_eight_digits", [hb])
                total += 1
                if got != int(data):
                    bad += 1
                    print(f"MISMATCH parse_eight_digits cpu={cpu} data={data!r} got={got}")
    print(f"[{arch}] numbers interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


def utf8_cases(rng, n):
    """Byte strings around 64-byte boundaries: valid text, then (half the time) one break."""
    for _ in range(n):
        pts = []
        for _ in range(rng.randrange(0, 70)):
            r = rng.random()
            cp = (rng.choice(UTF8_POINTS) if r < 0.3 else rng.randrange(0x80) if r < 0.6
                  else rng.choice([rng.randrange(0x80, 0x800), rng.randrange(0x800, 0xD800),
                                   rng.randrange(0xE000, 0x10000), rng.randrange(0x10000, 0x110000)]))
            pts.append(chr(cp))
        data = bytearray(b"a" * rng.randrange(70) + "".join(pts).encode("utf-8", "surrogatepass"))
        r = rng.random()
        if r < 0.3:
            at = rng.randrange(len(data) + 1)
            data[at:at] = rng.choice(UTF8_BAD)
        elif r < 0.4 and data:
            data[rng.randrange(len(data))] = rng.randrange(256)
        elif r < 0.5 and data:
            del data[rng.randrange(len(data)):]
            data += rng.choice(UTF8_BAD)[:rng.randrange(1, 3)]
        elif r < 0.6:
            # a code point cut short at the end of a block, then whole blocks of ASCII
            # (utf8_checker's prev_incomplete): the cut lands on bytes 61-63
            data = bytearray(b"a" * (64 * rng.randrange(1, 3) - rng.randrange(1, 4)))
            data[-1:] = bytes(rng.choice([[0xC3], [0xE2], [0xE2, 0x82], [0xF0], [0xF0, 0x9F, 0x98]]))
            data += b"b" * rng.randrange(64, 200)
        yield bytes(data)


def check_utf8(mod, cases: int, seed: int) -> int:
    rng = random.Random(seed)
    bad = total = 0
    arch = getattr(mod, "arch", "x86_64")
    cpus = ([{"avx2": True, "ssse3": True}, {"avx2": False, "ssse3": True},
             {"avx2": False, "ssse3": False}] if arch == "x86_64" else [{}])
    for data in utf8_cases(rng, cases):
        want = oracle.utf8_valid(data)
        for cpu in cpus:
            hb = 0x10000 + rng.randrange(64)
            it = Interpreter(mod, Memory(data, hb), cpu)
            got = it.call("nvol_utf8_valid", [hb, len(data)])
            total += 1
            if got != want:
                bad += 1
                print(f"MISMATCH utf8 cpu={cpu} data={data!r} got={got} want={want}")
    print(f"[{arch}] utf8 interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


def json_cases(rng, n):
    """(blocks, splits, state): 1-6 blocks of JSON-like bytes, the block counts of the calls
    that cover them (the state carried between calls), and the starting state."""
    alpha = b'{}[]:,"\\ \t\n\rabc019.-' + bytes([0x01, 0x0C, 0x1A, 0x1F, 0x7F, 0x80, 0xC3, 0xFF])
    heavy = b'\\"\\"\\ab'      # runs of backslashes and quotes, the escape scanner's hard cases
    ascii = bytes(c for c in alpha if c < 0x80)
    for _ in range(n):
        nb = rng.randint(1, 6)
        # half the documents are valid UTF-8 (ASCII bytes and whole code points), so that the
        # UTF-8 state words are tested on text that passes, not only on text that fails
        text = rng.random() < 0.5
        data = bytearray()
        while len(data) < 64 * nb:
            r = rng.random()
            if text and r < 0.25:
                data += chr(rng.choice(UTF8_POINTS[3:])).encode("utf-8")
            elif r < 0.3:
                data += rng.choice(JSON_SNIPPETS)
            elif r < 0.5:
                data += bytes(rng.choice(heavy) for _ in range(rng.randint(1, 12)))
            else:
                data += bytes(rng.choice(ascii if text else alpha) for _ in range(rng.randint(1, 8)))
        data = data[:64 * nb]
        if text and rng.random() < 0.4:
            # a code point at the end of block k, j of its bytes before the boundary:
            # finished in the next block, cut and followed by an ASCII block (an error), or cut
            # at the end of the document (incomplete)
            cp = chr(rng.choice([0xE9, 0x20AC, 0x1F600])).encode("utf-8")
            j = rng.randrange(1, len(cp))
            k = rng.randint(1, nb)
            at = 64 * k - j
            kind = rng.randrange(3)
            if kind == 0 and k < nb:
                data[at:at + len(cp)] = cp
            elif kind == 1 and k < nb:
                data[at:64 * k] = cp[:j]
                data[64 * k:64 * k + 64] = b" " * 64
            else:
                data[64 * nb - j:] = cp[:j]
        data = bytes(data)
        splits, left = [], nb
        while left:
            k = rng.randint(1, left)
            splits.append(k)
            left -= k
        if rng.random() < 0.3:
            state = (rng.randrange(2), rng.choice([0, (1 << 64) - 1]), rng.randrange(2), 0)
        else:
            state = (0, 0, 0, 0)
        yield data, splits, state


def json_run(call, mem, data, splits, state, hb, sb, ob):
    """Run nvol_json_index_raw over the splits with `call(args) -> count`; returns
    (positions, state words)."""
    for k, w in enumerate(state):
        mem[sb + 8 * k:sb + 8 * k + 8] = w.to_bytes(8, "little")
    out, at = [], 0
    for k in splits:
        n = call([sb, hb + 64 * at, k, 64 * at, ob])
        out += [int.from_bytes(bytes(mem[ob + 4 * i:ob + 4 * i + 4]), "little") for i in range(n)]
        at += k
    from .kernels.json import STATE_BYTES, STATE_WORDS
    st = tuple(int.from_bytes(bytes(mem[sb + 8 * k:sb + 8 * k + 8]), "little") for k in range(STATE_WORDS))
    return out, st + (bytes(mem[sb + STATE_BYTES - 3:sb + STATE_BYTES]),)


def json_same(got, want) -> bool:
    """The error words are any non-zero value; the other words are exact. "incomplete" is
    compared only while the bytes are valid (after an error the kernel may keep a stale one),
    and the carried bytes only when the call saw 3 or more."""
    (gp, gs), (wp, ws) = got, want
    return (gp == wp and gs[:3] == ws[:3] and (gs[3] != 0) == (ws[3] != 0)
            and (gs[4] != 0) == (ws[4] != 0) and (ws[4] or (gs[5] != 0) == (ws[5] != 0))
            and (len(ws[6]) < 3 or gs[6] == ws[6]))


def check_json(mod, cases: int, seed: int) -> int:
    from .kernels.json import STATE_BYTES
    rng = random.Random(seed)
    bad = total = 0
    arch = getattr(mod, "arch", "x86_64")
    cpus = ([{"avx2": True, "pclmul": True, "ssse3": True}, {"avx2": False, "ssse3": True},
             {"avx2": False, "ssse3": False}] if arch == "x86_64" else [{}])
    for data, splits, state in json_cases(rng, cases):
        want = oracle.json_index(data, 0, state)
        for cpu in cpus:
            hb, sb, ob = 0x10000 + 64 * rng.randrange(4) + rng.randrange(64), 0x80000, 0x90000
            mem = Memory(data, hb)
            mem.add(bytes(STATE_BYTES), sb)
            mem.add(bytes(4 * len(data)), ob)
            it = Interpreter(mod, mem, cpu)
            view = _MemView(mem)
            got = json_run(lambda a: it.call("nvol_json_index_raw", a), view, data, splits, state, hb, sb, ob)
            total += 1
            if not json_same(got, want):
                bad += 1
                print(f"MISMATCH json cpu={cpu} splits={splits} state={state} data={data!r} "
                      f"got={got} want={want}")
    print(f"[{arch}] json interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


class _MemView:
    """mem[a:b] reads and writes the interpreter's memory by address."""

    def __init__(self, mem):
        self.mem = mem

    def __getitem__(self, s):
        return self.mem.read(s.start, s.stop - s.start)

    def __setitem__(self, s, data):
        self.mem.write(s.start, data)


def check_byteset(mod, cases: int, seed: int) -> int:
    from .kernels.byteset import CLASS_BYTES, PAIR_BYTES
    rng = random.Random(seed)
    bad = total = 0
    arch = getattr(mod, "arch", "x86_64")
    cpus = ([{"avx2": True, "ssse3": True}, {"avx2": False}, {"avx2": False, "sse42": False}]
            if arch == "x86_64" else [{}])
    for s1, s2, i1, i2, hay, start, end in byteset_cases(rng, cases):
        want1 = next((i for i in range(start, end) if hay[i] in s1), None)
        want2 = next((p for p in range(start, end - max(i1, i2)) if hay[p + i1] in s1 and hay[p + i2] in s2), None)
        for cpu in cpus:
            hb, s1b, s2b, cb, pb = 0x10000 + rng.randrange(64), 0x80000, 0x90000, 0xC0000, 0xD0000
            mem = Memory(hay, hb)
            mem.add(s1, s1b)
            mem.add(s2, s2b)
            mem.add(bytes(CLASS_BYTES), cb)
            mem.add(bytes(PAIR_BYTES), pb)
            it = Interpreter(mod, mem, cpu)
            it.call("nvol_byteset_init", [s1b, len(s1), cb])
            it.call("nvol_byteset_pair_init", [s1b, len(s1), s2b, len(s2), i1, i2, pb])
            ob = 0xE0000
            mem.add(bytes(16), ob)
            r1 = it.call("nvol_find_byteset_raw", [cb, hb + start, hb + end])
            r2 = it.call("nvol_find_byteset_pair_raw", [pb, hb + start, hb + end])
            got1 = None if r1 == 0 else r1 - hb
            got2 = None if r2 == 0 else r2 - hb
            # negated search, over a haystack made mostly of members of set 1
            L = len(hay)
            nhay = bytearray(rng.choice(s1) for _ in range(L))
            for _ in range(rng.choice([0, 0, 1, 3])):
                if L:
                    nhay[rng.randrange(L)] = rng.randrange(256)
            nb = 0x40000 + rng.randrange(64)
            mem.add(bytes(nhay), nb)
            rn = it.call("nvol_find_not_byteset_raw", [cb, nb + start, nb + end])
            gotn = None if rn == 0 else rn - nb
            wantn = next((i for i in range(start, end) if nhay[i] not in s1), None)
            total += 1
            if gotn != wantn:
                bad += 1
                print(f"MISMATCH byteset not cpu={cpu} s1={s1!r} hay={bytes(nhay)!r}[{start}:{end}] got={gotn} want={wantn}")
            # masked pair: the first chunk with candidates, every candidate in it
            rm = it.call("nvol_find_byteset_pair_mask_raw", [pb, hb + start, hb + end, ob])
            mask = int.from_bytes(mem.region(ob)[:8], "little")
            span = int.from_bytes(mem.region(ob)[8:16], "little")
            step = 4 if arch == "aarch64" else 1
            cands = [] if rm == 0 else [rm - hb + (bit // step) for bit in range(64) if mask >> bit & 1]
            # exactly the candidates in [first, base + span) -- the caller resumes at base + span
            covered = [] if rm == 0 else [q for q in range(want2 if want2 is not None else 0, rm - hb + span)
                                          if q < end - max(i1, i2) and hay[q + i1] in s1 and hay[q + i2] in s2]
            ok_mask = (cands[0] if cands else None) == want2 and cands == covered and (rm == 0 or span >= 1)
            total += 3
            if not ok_mask:
                bad += 1
                print(f"MISMATCH byteset mask cpu={cpu} s1={s1!r} s2={s2!r} i=({i1},{i2}) hay={hay!r}[{start}:{end}] "
                      f"cands={cands} want_first={want2}")
            # visitor: every chunk with candidates, in order -- on the case's haystack and on a
            # dense one (bytes of both sets, up to 1100); once to the end, once stopping at a chunk
            dense = bytes(rng.choice(s1 + s2) if rng.random() < 0.8 else rng.randrange(256)
                          for _ in range(rng.choice([40, 300, 1100])))
            db = 0x60000 + rng.randrange(64)
            mem.add(dense, db)
            for bh, bb, bs, be in ((hay, hb, start, end), (dense, db, 0, len(dense))):
                allc = [q for q in range(bs, be - max(i1, i2)) if bh[q + i1] in s1 and bh[q + i2] in s2]
                chunks = []
                rv = it.call("nvol_find_byteset_pair_visit_raw", [pb, bb + bs, bb + be,
                                                                   lambda at, m: chunks.append((at, m)) and False])
                got = [at - bb + bit // step for at, m in chunks for bit in range(64) if m >> bit & 1]
                ok_v = rv == 0 and got == allc and all(m for _, m in chunks)
                if chunks:
                    stop = rng.randrange(len(chunks))
                    seen = []

                    def stop_at(at, m, seen=seen, stop=stop):
                        seen.append((at, m))
                        return len(seen) == stop + 1
                    rs = it.call("nvol_find_byteset_pair_visit_raw", [pb, bb + bs, bb + be, stop_at])
                    ok_v &= rs == chunks[stop][0] and seen == chunks[:stop + 1]
                total += 1
                if not ok_v:
                    bad += 1
                    print(f"MISMATCH byteset visit cpu={cpu} s1={s1!r} s2={s2!r} i=({i1},{i2}) hay={bh!r}[{bs}:{be}] "
                          f"got={got} want={allc}")
            # copy-until: dst gets [start, stop) exactly; nothing is written outside dst[0, end - start)
            stop = start + next((i for i, c in enumerate(hay[start:end]) if c in s1), end - start)
            n = end - start
            dstb = 0x70000 + rng.randrange(64)
            mem.add(b"\xEE" * max(n, 1), dstb)
            rc = it.call("nvol_copy_until_byteset_raw", [cb, hb + start, hb + end, dstb])
            dreg = mem.region(dstb)
            total += 1
            if rc - hb != stop or dreg[:stop - start] != hay[start:stop]:
                bad += 1
                print(f"MISMATCH byteset copy cpu={cpu} s1={s1!r} hay={hay!r}[{start}:{end}] got={rc - hb} want={stop}")
            if got1 != want1 or got2 != want2:
                bad += 1
                print(f"MISMATCH byteset cpu={cpu} s1={s1!r} s2={s2!r} i=({i1},{i2}) hay={hay!r}[{start}:{end}] "
                      f"got=({got1},{got2}) want=({want1},{want2})")
    print(f"[{arch}] byteset interpreter vs oracle: {total - bad}/{total} ok")
    return 1 if bad else 0


def check(mod, cases: int, seed: int) -> int:
    rng = random.Random(seed)
    lens = [0, 1, 2, 7, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127, 128, 129, 255, 256, 300, 1100]
    alphabets = [b"ab", bytes(range(256)), b"abc\n", b"\x7f\x80a"]
    bad = total = 0
    for kind, name, n in mod.api:
        if kind in ("memmem", "teddy"):
            continue
        for _ in range(cases):
            L = rng.choice(lens)
            alpha = rng.choice(alphabets)
            hay = bytes(rng.choice(alpha) for _ in range(L))
            needles = [] if kind == "ascii" else [rng.choice(b"abc\n\x00\x80\xff") for _ in range(n)]
            base = 0x10000 + rng.randrange(64)
            for cpu in ({"avx2": True}, {"avx2": False}):
                got = Interpreter(mod, Memory(hay, base), cpu).call(name, needles + [base, L])
                exp = oracle.expected(name, needles, hay)
                total += 1
                if got != exp:
                    bad += 1
                    print(f"MISMATCH {name} len={L} align={base % 64} needles={needles} got={got} want={exp}")
    print(f"[{getattr(mod, 'arch', 'x86_64')}] interpreter vs oracle: {total - bad}/{total} ok")
    rc = check_memmem(mod, cases * 8, seed) if any(k == "memmem" for k, _, _ in mod.api) else 0
    rc |= check_teddy(mod, cases * 4, seed) if any(k == "teddy" for k, _, _ in mod.api) else 0
    rc |= check_byteset(mod, cases * 4, seed) if any(f.name == "nvol_find_byteset_raw" for f in mod.functions) else 0
    rc |= check_json(mod, cases, seed) if any(f.name == "nvol_json_index_raw" for f in mod.functions) else 0
    rc |= check_utf8(mod, cases, seed) if any(f.name == "nvol_utf8_valid" for f in mod.functions) else 0
    rc |= check_numbers(mod, cases, seed) if any(f.name == "nvol_parse_eight_digits" for f in mod.functions) else 0
    return 1 if bad or rc else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nvol")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--lang", default=",".join(LANGS))
    g.add_argument("--isa", default="",
                   help="ISAs of a single --arch for memchr, count, ascii and memmem (default sse2,avx2 "
                        "on x86_64); teddy and byteset always build their own set (SSSE3/AVX2, and "
                        "SSE4.2 for byteset), which their dispatch relies on")
    g.add_argument("--arch", default="all", help="x86_64, aarch64 or all (default)")
    g.add_argument("--kernels", default=",".join(ALL))
    g.add_argument("--out", default="generated")
    ch = sub.add_parser("check")
    ch.add_argument("--cases", type=int, default=30)
    ch.add_argument("--seed", type=int, default=0)
    sub.add_parser("list")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        print("ISAs:      ", ", ".join(ISAS))
        print("kernels:   ", ", ".join(ALL))
        print("primitives:")
        for p in PRIMS.values():
            print(f"  {p.name:13} {p.doc}")
        return 0
    if a.cmd == "check":
        from .kernels import build_modules
        return max(check(m, a.cases, a.seed) for m in build_modules().values())
    from .kernels import build_modules
    if a.arch == "all":
        if a.isa:
            ap.error("--isa needs a single --arch (x86_64 or aarch64)")
        mod = build_modules(a.kernels.split(","))
    else:
        if a.arch not in ARCH_ISAS:
            ap.error(f"--arch: {a.arch} (x86_64, aarch64 or all)")
        bad = [n for n in a.isa.split(",") if a.isa and n not in ARCH_ISAS[a.arch]]
        if bad:
            ap.error(f"--isa: {','.join(bad)} not in {a.arch} ({', '.join(ARCH_ISAS[a.arch])})")
        isa = a.isa.split(",") if a.isa else None
        mod = {a.arch: build_module(isa, a.kernels.split(","), a.arch)}
    for p in generate(mod, a.lang.split(","), a.out):
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
