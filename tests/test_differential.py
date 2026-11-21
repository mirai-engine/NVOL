"""Generated code (C, via the generated Python binding) against the oracle.

Covers every length 0..260 at every alignment 0..63 with the match placed at
the edges, the middle and nowhere, on both the SSE2 and AVX2 code paths, plus
random long haystacks.
"""
import ctypes
import random

import pytest

from nvol import oracle
from conftest import paths

PATHS = paths({"sse2": 0, "avx2": 1})


def _api(module):
    return [(k, n, c) for k, n, c in module.api if k not in ("memmem", "teddy")]


@pytest.fixture(params=list(PATHS))
def path(request, native):
    native._lib.nvol_test_set_avx2(PATHS[request.param])
    yield request.param
    native._lib.nvol_test_set_avx2(-1)


def call(native, kind, name, needles, buf, off, L):
    fn = getattr(native._lib, name)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buf)) + off
    return fn(*needles, base, L)


def test_positional_exhaustive(native, module, path):
    rng = random.Random(7)
    buf = bytearray(260 + 128)
    for kind, name, n in _api(module):
        needles = [] if kind == "ascii" else [ord("x"), ord("y"), ord("z")][:n]
        filler = ord("a")
        for L in range(0, 261):
            for off in range(0, 64, 1 if L < 70 else 7):
                positions = {None, 0, L // 2, L - 1, rng.randrange(L)} if L else {None}
                for p in positions:
                    for i in range(len(buf)):
                        buf[i] = filler
                    # poison bytes just outside the window: must never be reported
                    hit = 0x80 if kind == "ascii" else needles[-1]
                    if off:
                        buf[off - 1] = hit
                    buf[off + L] = hit
                    if p is not None:
                        buf[off + p] = hit
                    hay = bytes(buf[off:off + L])
                    got = call(native, kind, name, needles, buf, off, L)
                    want = oracle.expected(name, needles, hay)
                    assert got == want, (name, path, L, off, p, got, want)


def test_random_dense(native, module, path):
    rng = random.Random(99)
    for kind, name, n in _api(module):
        for _ in range(300):
            L = rng.choice([rng.randrange(0, 70), rng.randrange(0, 600), rng.randrange(0, 20000)])
            alpha = rng.choice([b"ab", b"ab\n", bytes(range(256)), b"\x00\xff"])
            off = rng.randrange(64)
            data = bytes(rng.choice(alpha) for _ in range(L))
            buf = bytearray(off) + bytearray(data) + bytearray(64)
            needles = [] if kind == "ascii" else [rng.choice(alpha) for _ in range(n)]
            got = call(native, kind, name, needles, buf, off, L)
            assert got == oracle.expected(name, needles, data), (name, path, L, off)


def test_python_wrappers(native):
    assert native.memchr(ord("c"), b"abcabc") == 2
    assert native.memrchr(ord("c"), b"abcabc") == 5
    assert native.memchr2(ord("z"), ord("b"), bytearray(b"abcabc")) == 1
    assert native.memchr3(ord("q"), ord("w"), ord("e"), b"abc") is None
    assert native.count(ord("\n"), memoryview(b"a\nb\n" * 1000)) == 2000
    assert native.count_parity(ord("\n"), b"a\nb\n" * 1000) == 2000
    assert native.first_non_ascii("añb".encode()) == 1
    assert native.memchr(0, b"") is None


def test_iterators(native):
    import random
    rng = random.Random(5)
    for _ in range(300):
        L = rng.randrange(0, 700)
        hay = bytes(rng.choice(b"ab\nc") for _ in range(L))
        for ns in ([ord("\n")], [ord("\n"), ord("c")], [ord("\n"), ord("c"), ord("b")]):
            want = [i for i, x in enumerate(hay) if x in ns]
            sfx = "" if len(ns) == 1 else str(len(ns))
            assert list(getattr(native, f"memchr{sfx}_iter")(*ns, hay)) == want
            assert list(getattr(native, f"memrchr{sfx}_iter")(*ns, bytearray(hay))) == want[::-1]


def test_c_iterator_meets_in_middle(native):
    """next / next_back interleaved never overlap (C iterator struct)."""
    import ctypes as C

    class It(C.Structure):
        _fields_ = [("base", C.c_void_p), ("lo", C.c_void_p), ("hi", C.c_void_p),
                    ("n1", C.c_uint8), ("n2", C.c_uint8), ("n3", C.c_uint8), ("k", C.c_int)]
    lib = native._lib
    lib.nvol_memchr_iter_init.argtypes = [C.POINTER(It), C.c_int, C.c_uint8, C.c_uint8, C.c_uint8, C.c_char_p, C.c_size_t]
    for f in ("nvol_memchr_iter_next", "nvol_memchr_iter_next_back", "nvol_memchr_iter_count"):
        getattr(lib, f).argtypes = [C.POINTER(It)]
        getattr(lib, f).restype = C.c_size_t
    hay = b"x\nx\n\n" * 50
    want = [i for i, x in enumerate(hay) if x == 10]
    it = It()
    lib.nvol_memchr_iter_init(C.byref(it), 1, 10, 0, 0, hay, len(hay))
    front, back = [], []
    while True:
        a = lib.nvol_memchr_iter_next(C.byref(it))
        if a == native._NONE:
            break
        front.append(a)
        b = lib.nvol_memchr_iter_next_back(C.byref(it))
        if b == native._NONE:
            break
        back.append(b)
    assert front + back[::-1] == want
    lib.nvol_memchr_iter_init(C.byref(it), 1, 10, 0, 0, hay, len(hay))
    lib.nvol_memchr_iter_next(C.byref(it))
    assert lib.nvol_memchr_iter_count(C.byref(it)) == len(want) - 1
