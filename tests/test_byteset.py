"""Byte-set search: generated C (via ctypes) against a plain Python oracle, on the
AVX2 path (16-byte kernels below 32 bytes), the SSE4.2 path without AVX2 and the
baseline path (SSE2 chains and ranges, scalar otherwise)."""
import ctypes
import random

import pytest

from nvol.__main__ import byteset_cases
from nvol.kernels.byteset import CLASS_BYTES, PAIR_BYTES
from conftest import mask_positions, paths

PATHS = paths({"no-avx2": 0, "avx2": 1, "sse42": 2})   # 0: no AVX2, no SSE4.2
P = ctypes.c_void_p


@pytest.fixture(params=list(PATHS))
def lib(request, native):
    L = native._lib
    L.nvol_byteset_init.argtypes = [P, ctypes.c_size_t, P]
    L.nvol_byteset_pair_init.argtypes = [P, ctypes.c_size_t, P, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, P]
    for f in (L.nvol_find_byteset_raw, L.nvol_find_not_byteset_raw, L.nvol_find_byteset_pair_raw):
        f.argtypes = [P, P, P]
        f.restype = P
    L.nvol_test_set_avx2(PATHS[request.param])
    yield L
    L.nvol_test_set_avx2(-1)


def _search(L, s1, s2, i1, i2, buf, off, start, end):
    """buf: a ctypes buffer holding the haystack at offset `off`."""
    b1, b2 = ctypes.create_string_buffer(bytes(s1), max(1, len(s1))), ctypes.create_string_buffer(bytes(s2), max(1, len(s2)))
    cls = ctypes.create_string_buffer(CLASS_BYTES)
    pst = ctypes.create_string_buffer(PAIR_BYTES)
    L.nvol_byteset_init(b1, len(s1), cls)
    L.nvol_byteset_pair_init(b1, len(s1), b2, len(s2), i1, i2, pst)
    base = ctypes.addressof(buf) + off
    r1 = L.nvol_find_byteset_raw(cls, base + start, base + end)
    r2 = L.nvol_find_byteset_pair_raw(pst, base + start, base + end)
    return (None if not r1 else r1 - base), (None if not r2 else r2 - base)


def _want(s1, s2, i1, i2, hay, start, end):
    w1 = next((i for i in range(start, end) if hay[i] in s1), None)
    w2 = next((p for p in range(start, end - max(i1, i2)) if hay[p + i1] in s1 and hay[p + i2] in s2), None)
    return w1, w2


def test_random(lib):
    rng = random.Random(77)
    for s1, s2, i1, i2, hay, start, end in byteset_cases(rng, 4000):
        buf = ctypes.create_string_buffer(hay, len(hay) + 1)
        assert _search(lib, s1, s2, i1, i2, buf, 0, start, end) == _want(s1, s2, i1, i2, hay, start, end), \
            (s1, s2, i1, i2, hay, start, end)


def test_random_not(lib):
    """Negated search on the random sets, over haystacks made mostly of members of the set
    (as nvol check does), so runs are long enough to reach every vector kernel."""
    f = lib.nvol_find_not_byteset_raw
    rng = random.Random(78)
    for s1, _s2, _i1, _i2, hay, start, end in byteset_cases(rng, 4000):
        if not s1:
            continue
        n = len(hay) + rng.choice([0, 0, 40, 200])
        end += n - len(hay)
        nhay = bytearray(rng.choice(s1) for _ in range(n))
        for _ in range(rng.choice([0, 0, 1, 3])):
            if n:
                nhay[rng.randrange(n)] = rng.randrange(256)
        buf = ctypes.create_string_buffer(bytes(nhay), n + 1)
        cls = ctypes.create_string_buffer(CLASS_BYTES)
        b1 = ctypes.create_string_buffer(bytes(s1), len(s1))
        lib.nvol_byteset_init(b1, len(s1), cls)
        base = ctypes.addressof(buf)
        r = f(cls, base + start, base + end)
        want = next((i for i in range(start, end) if nhay[i] not in s1), None)
        assert (None if not r else r - base) == want, (s1, bytes(nhay), start, end)


def test_alignment_and_positions(lib):
    """Every haystack offset 0..63, the hit at every position near both ends, and
    poison bytes just outside [start, end) that must never be reported."""
    s1, s2 = bytes([0x00, 0x80, 0xFF, 0x41]), bytes([0x7F, 0x8F, 0x0A])
    i1, i2 = 3, 9
    for hl in (0, 1, 15, 16, 17, 31, 32, 33, 40, 41, 47, 48, 64, 65, 100, 129):
        for off in range(64):
            for at in sorted({0, 1, hl // 2, max(0, hl - 1), max(0, hl - 10), max(0, hl - 11)}):
                buf = bytearray(b"\x01" * (off + hl + 64))
                buf[off - 1 if off else 0] = 0x41           # poison before (when there is room)
                buf[off + hl] = 0x41                        # poison after
                if at < hl:
                    buf[off + at] = 0x80
                if at + i2 < hl:
                    buf[off + at + i1] = 0xFF
                    buf[off + at + i2] = 0x8F
                cb = (ctypes.c_char * len(buf)).from_buffer(buf)
                hay = bytes(buf[off:off + hl])
                assert _search(lib, s1, s2, i1, i2, cb, off, 0, hl) == _want(s1, s2, i1, i2, hay, 0, hl), \
                    (hl, off, at)


def test_full_and_empty_sets(lib):
    hay = bytes(range(256)) * 2
    buf = ctypes.create_string_buffer(hay, len(hay))
    every = bytes(range(256))
    assert _search(lib, every, every, 0, 0, buf, 0, 0, len(hay)) == (0, 0)
    assert _search(lib, b"", b"", 0, 0, buf, 0, 0, len(hay)) == (None, None)
    for b in (0x00, 0x7F, 0x80, 0xFF):
        assert _search(lib, bytes([b]), bytes([b]), 0, 0, buf, 0, 1, len(hay)) == (b or 256, b or 256)


NOT_SETS = [b" \t\n\r", b"0123456789", bytes(range(0x80, 0x90)), b"\x00", b'"\\',
            # 7-16 bytes, no 0, neither unique-nibble nor a low range: the SSE4.2 path
            # takes pcmpistri over the class's member list; 17 is one past its limit
            b"aAbBcCdD", b"+-.eE0123456789", b"!#$%&()*+,-./:;<", b"!#$%&()*+,-./:;<="]


@pytest.mark.parametrize("members", NOT_SETS)
def test_not_byteset(lib, members):
    """First byte outside the set: unique-nibble sets (whitespace, digits, 16 high
    bytes), a one-byte set and a two-byte set (k-compare chains), sets of 8-17 bytes
    (pcmpistri on the SSE4.2 path, the nibble lookup otherwise), at every offset and
    run length around the vector sizes, with a poison byte right after end."""
    cls = ctypes.create_string_buffer(CLASS_BYTES)
    mb = ctypes.create_string_buffer(members, len(members))
    lib.nvol_byteset_init(mb, len(members), cls)
    other = next(b for b in range(256) if b not in members)
    rng = random.Random(len(members))
    for off in range(0, 64, 3):
        for run in (0, 1, 2, 15, 16, 17, 31, 32, 33, 63, 64, 65, 100):
            for tail in (0, 1, 20, 40):
                buf = bytearray(rng.choice(members) for _ in range(off + run + tail + 1))
                if tail:
                    buf[off + run] = other
                buf[off + run + tail] = other                 # poison right after end
                cb = (ctypes.c_char * len(buf)).from_buffer(buf)
                base = ctypes.addressof(cb) + off
                r = lib.nvol_find_not_byteset_raw(cls, base, base + run + tail)
                want = run if tail else None
                assert (None if not r else r - base) == want, (members, off, run, tail)



class _Visitor(ctypes.Structure):
    _fields_ = [("fn", ctypes.CFUNCTYPE(ctypes.c_int, P, P, ctypes.c_uint64)), ("ctx", P)]


def _visit(L, pst, base, start, end, stop=None):
    """Chunks the visitor sees (base offset, mask) and the returned pointer; stop: the
    visitor returns true on that chunk (0-based)."""
    seen = []

    def fn(ctx, at, m):
        seen.append((at - base, m))
        return int(stop is not None and len(seen) == stop + 1)
    v = _Visitor(_Visitor._fields_[0][1](fn), None)
    r = L.nvol_find_byteset_pair_visit_raw(pst, base + start, base + end, ctypes.byref(v))
    return seen, (None if not r else r - base)


def test_pair_visit(lib):
    """Visitor search: random cases and dense haystacks (bytes of both sets), to the end and
    stopping at a random chunk, against every candidate."""
    f = lib.nvol_find_byteset_pair_visit_raw
    f.argtypes = [P, P, P, P]
    f.restype = P
    rng = random.Random(91)
    for k, (s1, s2, i1, i2, hay, start, end) in enumerate(byteset_cases(rng, 1500)):
        if k % 2:
            n = rng.choice([40, 300, 1100, 5000])
            hay = bytes(rng.choice(s1 + s2) if rng.random() < 0.8 else rng.randrange(256) for _ in range(n))
            start, end = rng.randrange(4), n - rng.randrange(4)
        buf = ctypes.create_string_buffer(hay, len(hay) + 1)
        pst = ctypes.create_string_buffer(PAIR_BYTES)
        b1 = ctypes.create_string_buffer(bytes(s1), max(1, len(s1)))
        b2 = ctypes.create_string_buffer(bytes(s2), max(1, len(s2)))
        lib.nvol_byteset_pair_init(b1, len(s1), b2, len(s2), i1, i2, pst)
        base = ctypes.addressof(buf)
        want = [q for q in range(start, end - max(i1, i2)) if hay[q + i1] in s1 and hay[q + i2] in s2]
        chunks, r = _visit(lib, pst, base, start, end)
        got = [o + i for o, m in chunks for i in mask_positions(m)]
        assert r is None and got == want and all(m for _, m in chunks), (s1, s2, i1, i2, hay, start, end)
        if chunks:
            stop = rng.randrange(len(chunks))
            seen, r = _visit(lib, pst, base, start, end, stop)
            assert seen == chunks[:stop + 1] and r == chunks[stop][0]


def test_copy_until(lib):
    """Copy-until: the prefix up to the first member is copied exactly, the return is that
    member (or end), and nothing outside dst[0, end - start) is written (guard bytes on both
    sides of dst); over every alignment of source and destination."""
    f = lib.nvol_copy_until_byteset_raw
    f.argtypes = [P, P, P, P]
    f.restype = P
    rng = random.Random(5)
    cases = list(byteset_cases(rng, 1500))
    for k, (s1, _s2, _i1, _i2, hay, start, end) in enumerate(cases):
        if k % 3 == 0:                       # long runs without members
            n = rng.choice([31, 32, 33, 64, 200, 1000])
            pool = [c for c in range(256) if c not in s1] or [0]
            hay = bytes(rng.choice(pool) for _ in range(n))
            if s1 and rng.random() < 0.7:
                hay = bytearray(hay)
                hay[rng.randrange(n)] = s1[rng.randrange(len(s1))]
                hay = bytes(hay)
            start, end = 0, n
        cls = ctypes.create_string_buffer(CLASS_BYTES)
        b1 = ctypes.create_string_buffer(bytes(s1), max(1, len(s1)))
        lib.nvol_byteset_init(b1, len(s1), cls)
        soff, doff = rng.randrange(32), rng.randrange(32)
        src = ctypes.create_string_buffer(b"\x00" * soff + hay, soff + len(hay) + 1)
        n = end - start
        dst = ctypes.create_string_buffer(b"\xAB" * (doff + n + 64), doff + n + 64)
        base = ctypes.addressof(src) + soff
        r = f(cls, base + start, base + end, ctypes.addressof(dst) + doff)
        stop = start + next((i for i, c in enumerate(hay[start:end]) if c in s1), n)
        out = dst.raw
        assert r - base == stop, (s1, hay, start, end)
        assert out[doff:doff + stop - start] == hay[start:stop]
        assert out[:doff] == b"\xAB" * doff and out[doff + n:] == b"\xAB" * 64


def test_python_byteset(native):
    """The generated Python binding's ByteSet against a naive scan."""
    rng = random.Random(4)
    for s1, _s2, _i1, _i2, hay, start, end in byteset_cases(rng, 600):
        bs = native.ByteSet(bytes(s1))
        start = min(start, len(hay))
        want = next((i for i in range(start, len(hay)) if hay[i] in s1), None)
        want_not = next((i for i in range(start, len(hay)) if hay[i] not in s1), None)
        assert bs.find(hay, start) == want and bs.find_not(hay, start) == want_not, (s1, hay, start)
        assert bs.find(bytearray(hay), start) == want
